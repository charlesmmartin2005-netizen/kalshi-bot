"""
Thin REST client over Kalshi's trade-api/v2.

Deliberately un-clever: every method maps to one endpoint, returns parsed
JSON, and raises on HTTP errors. Retry/backoff and rate-limit tracking live
here centrally so nothing upstream has to think about it.
"""
import os
import time
import logging
import threading
import requests

from auth import KalshiAuth

log = logging.getLogger("kalshi_client")


class KalshiRateLimitError(Exception):
    pass


class KalshiAuthMissing(Exception):
    pass


class KalshiClient:
    def __init__(self, base_url: str, auth: KalshiAuth | None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self._session = requests.Session()
        self._consecutive_errors = 0

    # ---- low-level request handling -------------------------------------

    # Shared by every thread using this client (trading loop + dashboard
    # sweeps). Kalshi's token-bucket limiter has no Retry-After header, so
    # pace requests ourselves and retry 429s with backoff — otherwise a
    # 66-page catalog sweep starves the trading loop and its 429s count
    # toward the consecutive-error kill switch.
    # Unauthenticated reads got 429s at ~12/s combined (2026-10-20), so stay
    # at 10. The engine's REST polling uses ~8/s; the dashboard's background
    # sweeps share the rest.
    MAX_REQUESTS_PER_SECOND = float(os.getenv("KALSHI_MAX_RPS", "10"))
    RATE_LIMIT_RETRIES = 3
    _throttle_lock = threading.Lock()
    _next_slot = 0.0

    # Background work (the dashboard's market sweeps) runs on a low-priority
    # client capped at this rate, so the trading loop always has headroom —
    # a full sweep used to starve it for 10-25s.
    LOW_PRIORITY_RPS = 3.0
    low_priority = False
    _low_lock = threading.Lock()
    _next_low_slot = 0.0

    def _wait_for_slot(self):
        if self.low_priority:
            with KalshiClient._low_lock:
                now = time.monotonic()
                wait = KalshiClient._next_low_slot - now
                KalshiClient._next_low_slot = max(now, KalshiClient._next_low_slot) + 1 / self.LOW_PRIORITY_RPS
            if wait > 0:
                time.sleep(wait)
        with KalshiClient._throttle_lock:
            now = time.monotonic()
            wait = KalshiClient._next_slot - now
            KalshiClient._next_slot = max(now, KalshiClient._next_slot) + 1 / self.MAX_REQUESTS_PER_SECOND
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None,
                  authenticated: bool = True):
        url = f"{self.base_url}{path}"
        if authenticated and self.auth is None:
            raise KalshiAuthMissing(
                f"{method} {path} needs an API key — set KALSHI_API_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH in .env")

        for attempt in range(self.RATE_LIMIT_RETRIES + 1):
            self._wait_for_slot()
            headers = {"Content-Type": "application/json"}
            if authenticated:   # sign after waiting, so the timestamp is fresh
                headers.update(self.auth.sign_request(method, f"/trade-api/v2{path}"))
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as e:
                self._consecutive_errors += 1
                log.error("Request failed: %s %s -> %s", method, path, e)
                raise
            if resp.status_code != 429:
                break
            if attempt < self.RATE_LIMIT_RETRIES:
                time.sleep(0.5 * 2 ** attempt)

        if resp.status_code == 429:
            self._consecutive_errors += 1
            log.warning("Rate limited on %s %s after %d retries", method, path, self.RATE_LIMIT_RETRIES)
            raise KalshiRateLimitError(f"429 on {method} {path}")

        if resp.status_code >= 400:
            self._consecutive_errors += 1
            log.error("HTTP %s on %s %s: %s", resp.status_code, method, path, resp.text[:500])
            resp.raise_for_status()

        self._consecutive_errors = 0
        return resp.json()

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    # ---- market data (public, no auth required) --------------------------

    def get_markets(self, status: str = "open", limit: int = 100, cursor: str = None,
                     series_ticker: str = None) -> dict:
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self._request("GET", "/markets", params=params, authenticated=False)

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}", authenticated=False)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, authenticated=False
        )

    def get_markets_batch(self, tickers: list[str]) -> list:
        """Several markets (with best bid/ask) in one request — matched the
        per-market orderbook's top of book exactly when tested 2026-10-20."""
        if not tickers:
            return []
        return self._request("GET", "/markets", params={"tickers": ",".join(tickers), "limit": 1000},
                             authenticated=False).get("markets", [])

    def get_trades(self, ticker: str, min_ts: int = None, limit: int = 200) -> list:
        """Public trade tape for one market, newest first."""
        params = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        return self._request("GET", "/markets/trades", params=params,
                             authenticated=False).get("trades", [])

    def get_events(self, status: str = "open", cursor: str = None, limit: int = 200,
                   with_nested_markets: bool = True) -> dict:
        params = {"status": status, "limit": limit,
                  "with_nested_markets": str(with_nested_markets).lower()}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/events", params=params, authenticated=False)

    # ---- trading (authenticated) ------------------------------------------

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def get_orders(self, ticker: str = None, status: str = None) -> dict:
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params)

    def create_order(self, ticker: str, side: str, action: str, count: int, price_cents: int,
                      order_type: str = "limit", client_order_id: str = None) -> dict:
        """
        !!! VERIFY BEFORE FIRST USE !!!
        Kalshi is migrating order placement to a V2 endpoint
        (/portfolio/events/orders) with a different schema than the legacy
        /portfolio/orders endpoint this method previously assumed. Per
        Kalshi's docs, the legacy endpoint is being deprecated. The
        implementation below reflects the V2 shape found in Kalshi's
        current docs as of this writing:

            POST /trade-api/v2/portfolio/events/orders
            {
              "ticker": "...",
              "client_order_id": "...",
              "side": "bid" | "ask",       # NOT "yes"/"no" — single book side
              "count": "10.00",             # string, contract count
              "price": "0.5600",            # string, DOLLARS not cents
              "time_in_force": "good_till_canceled",
              ...
            }

        Given the read-side field names were already wrong once in this
        project, do NOT trust this blindly — confirm the exact current
        schema at https://docs.kalshi.com/api-reference/orders/create-order-v2
        before sending a real order, ideally by first hitting it from a
        script with a tiny throwaway order on a cheap/illiquid market and
        inspecting the response, the same way we debugged the orderbook.

        side: "yes" or "no" (translated to bid/ask below)
        action: "buy" or "sell"
        price_cents: limit price in whole cents (1-99) — converted to a
                     dollar string internally
        """
        # Kalshi's V2 side is "bid"/"ask" per the single order book, not
        # yes/no directly. A yes-buy or no-sell is a "bid" on the relevant
        # side; translate carefully — VERIFY this mapping against the docs
        # before live use, this is a best-effort translation, not confirmed
        # against a real response.
        book_side = "bid" if action == "buy" else "ask"
        price_dollars = f"{price_cents / 100:.4f}"

        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(__import__("uuid").uuid4()),
            "side": book_side,
            "count": f"{count:.2f}",
            "price": price_dollars,
            "time_in_force": "good_till_canceled",
        }
        return self._request("POST", "/portfolio/events/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def cancel_all_orders(self, ticker: str = None) -> list:
        """Not a native single endpoint — fetches resting orders and cancels
        each. Used by the kill switch to flatten quoting quickly."""
        open_orders = self.get_orders(ticker=ticker, status="resting").get("orders", [])
        results = []
        for order in open_orders:
            try:
                results.append(self.cancel_order(order["order_id"]))
            except Exception as e:
                log.error("Failed to cancel order %s: %s", order.get("order_id"), e)
        return results


def build_client(config) -> KalshiClient:
    """Client with auth if credentials are configured. Market data endpoints
    are public, so paper trading works without a key; live mode does not."""
    auth = None
    try:
        auth = KalshiAuth(config.kalshi.api_key_id, config.kalshi.private_key_path)
    except (ValueError, OSError) as e:
        if not config.kalshi.paper_trading:
            raise
        log.info("No usable API key (%s) — running on public market data only", e)
    return KalshiClient(config.kalshi.base_url, auth)


def dollars_to_cents(value) -> int | None:
    """Kalshi returns prices as dollar strings ("0.4500"). None-safe."""
    if value in (None, ""):
        return None
    return round(float(value) * 100)


def normalize_orderbook(raw_orderbook: dict) -> dict:
    """
    IMPORTANT: Kalshi's actual orderbook response (confirmed against a live
    call) looks like this:

        {
          "orderbook_fp": {
            "yes_dollars": [["0.4500", "120.00"], ...],
            "no_dollars":  [["0.5600", "80.00"], ...]
          }
        }

    Prices are DOLLAR STRINGS (e.g. "0.0100" = 1 cent), not integer cents,
    and sizes are also strings. There is no separate ask array — same
    yes/no duality as before: a no-bid at price P implies a yes-ask at
    (1.00 - P).

    This function converts everything to integer cents internally so the
    rest of the bot can work in whole-cent arithmetic, which is easier to
    reason about for quoting logic.
    """
    book_fp = raw_orderbook.get("orderbook_fp", {}) or {}
    yes_levels_raw = book_fp.get("yes_dollars", []) or []
    no_levels_raw = book_fp.get("no_dollars", []) or []

    def to_cents(dollar_str: str) -> int:
        return round(float(dollar_str) * 100)

    yes_levels = [(to_cents(price), float(size)) for price, size in yes_levels_raw]
    no_levels = [(to_cents(price), float(size)) for price, size in no_levels_raw]

    best_yes_bid = max((p for p, _ in yes_levels), default=None)
    best_no_bid = max((p for p, _ in no_levels), default=None)
    # A no-bid at price P (cents) implies a yes-ask at 100 - P.
    best_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None

    return {
        "yes_bid": best_yes_bid,
        "yes_ask": best_yes_ask,
        # Contracts shown at the touch (takes never exceed them).
        "yes_bid_size": dict(yes_levels).get(best_yes_bid),
        "yes_ask_size": dict(no_levels).get(best_no_bid),
        "spread_cents": (best_yes_ask - best_yes_bid)
        if (best_yes_bid is not None and best_yes_ask is not None)
        else None,
        "raw_yes_levels": yes_levels,
        "raw_no_levels": no_levels,
    }


def top_of_book(market: dict) -> dict:
    """Best bid/ask from a GET /markets row, in normalize_orderbook's shape
    (without depth levels)."""
    bid = dollars_to_cents(market.get("yes_bid_dollars")) or None
    ask = dollars_to_cents(market.get("yes_ask_dollars"))
    ask = ask if ask and ask < 100 else None

    def size(field, px):
        return float(market[field]) if px is not None and market.get(field) else None

    return {"yes_bid": bid, "yes_ask": ask,
            "yes_bid_size": size("yes_bid_size_fp", bid), "yes_ask_size": size("yes_ask_size_fp", ask),
            "spread_cents": ask - bid if bid is not None and ask is not None else None,
            "raw_yes_levels": [], "raw_no_levels": []}
