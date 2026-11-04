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
import uuid
from datetime import datetime

import requests

from auth import KalshiAuth

log = logging.getLogger("kalshi_client")

# The engine holds its lock while an order is out, so a hung order must not
# freeze it for a read's full timeout.
ORDER_TIMEOUT_SECONDS = 3.0


class KalshiRateLimitError(Exception):
    pass


class WriteBudget:
    """Stays inside Kalshi's limit on orders and cancels. A Basic account gets 100
    tokens a second, refilling continuously, and can't bank more than one
    second's worth; an order costs 10 and a cancel 2 (docs read 2026-11-04). This
    runs at 80, so a misjudged cost doesn't turn into 429s on a take."""
    ORDER = 10
    CANCEL = 2

    def __init__(self, rate: float = 80.0, clock=time.monotonic):
        self.rate = rate
        self.clock = clock
        self.tokens = rate
        self._last = clock()

    def _refill(self):
        now = self.clock()
        self.tokens = min(self.rate, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def can_spend(self, cost: float, keep: float = 0) -> bool:
        self._refill()
        return self.tokens - cost >= keep

    def spend(self, cost: float, keep: float = 0) -> bool:
        """Take `cost` tokens if that leaves at least `keep`."""
        if not self.can_spend(cost, keep):
            return False
        self.tokens -= cost
        return True

    def owe(self, cost: float):
        """Spend regardless: a cancel always goes out."""
        self._refill()
        self.tokens -= cost


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

    # Background work (the dashboard's markouts refresh; sweep.py in its own
    # process) runs on a low-priority client: at most this rate and, within
    # a process, only in slots the trading loop leaves free. A full sweep
    # used to starve the loop for 10-25s, and later still queued ahead of it.
    # While sweep.py runs, the dashboard lowers its own rate by this much so
    # the two processes together stay at MAX_REQUESTS_PER_SECOND.
    LOW_PRIORITY_RPS = 2.0
    low_priority = False
    _next_low_slot = 0.0

    def _wait_for_slot(self):
        if self.low_priority:
            # Take a slot only once it's free now, i.e. no trading request
            # has reserved it, so a sweep delays trading by at most one slot.
            while True:
                with KalshiClient._throttle_lock:
                    now = time.monotonic()
                    free_at = max(KalshiClient._next_slot, KalshiClient._next_low_slot)
                    if now >= free_at:
                        KalshiClient._next_slot = now + 1 / self.MAX_REQUESTS_PER_SECOND
                        KalshiClient._next_low_slot = now + 1 / self.LOW_PRIORITY_RPS
                        return
                time.sleep(max(free_at - now, 0.01))
        with KalshiClient._throttle_lock:
            now = time.monotonic()
            wait = KalshiClient._next_slot - now
            KalshiClient._next_slot = max(now, KalshiClient._next_slot) + 1 / self.MAX_REQUESTS_PER_SECOND
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None,
                  authenticated: bool = True, write: bool = False, missing_ok: bool = False):
        """write: an order or cancel. Kalshi meters writes in their own bucket,
        so they don't wait behind market-data requests, and a 429 isn't retried:
        an order sent again seconds later is at a price from seconds ago.
        missing_ok: a 404 is an answer ({}), not an error."""
        url = f"{self.base_url}{path}"
        if authenticated and self.auth is None:
            raise KalshiAuthMissing(
                f"{method} {path} needs an API key — set KALSHI_API_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH in .env")

        for attempt in range(1 if write else self.RATE_LIMIT_RETRIES + 1):
            if not write:
                self._wait_for_slot()
            headers = {"Content-Type": "application/json"}
            if authenticated:   # sign after waiting, so the timestamp is fresh
                headers.update(self.auth.sign_request(method, f"/trade-api/v2{path}"))
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body, headers=headers,
                    timeout=ORDER_TIMEOUT_SECONDS if write else self.timeout
                )
            except requests.RequestException as e:
                self._consecutive_errors += 1
                log.error("Request failed: %s %s -> %s", method, path, e)
                raise
            if resp.status_code != 429:
                break
            if not write and attempt < self.RATE_LIMIT_RETRIES:
                time.sleep(0.5 * 2 ** attempt)

        if resp.status_code == 429:
            self._consecutive_errors += 1
            log.warning("Rate limited on %s %s after %d retries", method, path, self.RATE_LIMIT_RETRIES)
            raise KalshiRateLimitError(f"429 on {method} {path}")

        if missing_ok and resp.status_code == 404:
            return {}
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

    def _all_pages(self, path: str, key: str, params: dict) -> list:
        rows, cursor = [], None
        while True:
            page = self._request("GET", path, params={**params, **({"cursor": cursor} if cursor else {})})
            rows.extend(page.get(key, []))
            cursor = page.get("cursor")
            if not cursor:
                return rows

    def get_endpoint_costs(self) -> dict:
        """{default_cost, endpoint_costs: [{method, path, cost}]}, in rate-limit tokens."""
        return self._request("GET", "/account/endpoint_costs")

    def get_balance(self) -> dict:
        """{balance (available, cents), portfolio_value (cents), ...}."""
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict[str, float]:
        """Every open position on Kalshi: {ticker: net contracts}, positive
        for YES and negative for NO. Markets not yet settled only."""
        rows = self._all_pages("/portfolio/positions", "market_positions", {"count_filter": "position", "limit": 200})
        return {r["ticker"]: float(r["position_fp"]) for r in rows if float(r["position_fp"])}

    def get_orders(self, ticker: str = None, status: str = None) -> dict:
        params = {"limit": 200}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return {"orders": self._all_pages("/portfolio/orders", "orders", params)}

    def get_fills(self, min_ts: float = None) -> list[dict]:
        """Our fills (normalize_fill's shape), from min_ts on."""
        params = {"limit": 200, **({"min_ts": int(min_ts)} if min_ts else {})}
        return [normalize_fill(r) for r in self._all_pages("/portfolio/fills", "fills", params)]

    def create_order(self, ticker: str, action: str, count: float, price_cents: int,
                     time_in_force: str = "good_till_canceled", post_only: bool = False,
                     expires_at: int = None, client_order_id: str = None) -> dict:
        """POST /portfolio/events/orders (Create Order V2, docs read 2026-11-04;
        live_check.py tries it for real). The book is YES-only: "bid" buys YES,
        "ask" sells YES, which is the same as buying NO at 1 - price. Counts and
        prices are strings, prices in dollars.

        Answers {order_id, client_order_id, fill_count, remaining_count, ts_ms},
        plus average_fill_price and average_fee_paid (per contract) if anything
        filled.

        self_trade_prevention_type "maker": if a take meets our own resting
        quote, that quote is cancelled and the take carries on."""
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "bid" if action == "buy" else "ask",
            "count": f"{count:.2f}",
            "price": f"{price_cents / 100:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "maker",
        }
        if post_only:
            body["post_only"] = True
        if time_in_force == "good_till_canceled":
            body["cancel_order_on_pause"] = True
        if expires_at is not None:
            body["expiration_time"] = int(expires_at)
        return self._request("POST", "/portfolio/events/orders", json_body=body, write=True)

    def cancel_order(self, order_id: str, ticker: str) -> dict:
        """DELETE /portfolio/events/orders/{id}. Kalshi can't find the shard
        from the order ID alone, so the ticker goes with it. {} if the order is
        already gone (it filled, expired or was cancelled)."""
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}",
                             params={"market_ticker": ticker}, write=True, missing_ok=True)


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


def whole(x: float):
    return int(x) if x == int(x) else x


def normalize_fill(row: dict) -> dict:
    """One of our fills from GET /portfolio/fills or the `fill` websocket
    channel, in the ledger's terms. A book_side of "bid" bought YES and "ask"
    sold it, and the price is always the YES price. Raises KeyError or
    ValueError rather than guess at a fill it can't read."""
    if row.get("ts_ms"):
        ts = row["ts_ms"] / 1000
    else:
        ts = row["ts"] if row.get("ts") else datetime.fromisoformat(row["created_time"].replace("Z", "+00:00")).timestamp()
    return {
        "trade_id": row["trade_id"],
        "order_id": row["order_id"],
        "client_order_id": row.get("client_order_id"),
        "ticker": row.get("market_ticker") or row["ticker"],
        "action": {"bid": "buy", "ask": "sell"}[row["book_side"]],
        "count": whole(round(float(row["count_fp"]), 2)),
        "price_cents": whole(round(float(row["yes_price_dollars"]) * 100, 2)),
        "maker": not row["is_taker"],
        "fee_cents": whole(round(float(row["fee_cost"]) * 100, 4)),
        "ts": ts,
    }


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
