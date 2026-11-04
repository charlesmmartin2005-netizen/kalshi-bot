"""
A stand-in for Kalshi's REST API at the HTTP boundary, for tests only. It sits
under the real KalshiClient in place of requests.Session and follows Kalshi's
docs (checked 2026-11-04): V2 order fields, YES-side bid/ask, fixed-point
strings, and a 400 for anything the docs call invalid. So a wrong path, field
or format fails a test here instead of on a real order.

Where the docs don't settle something, this copies what the real API did on
2026-11-04: the refusals for a post-only order that would cross and for too
little money, and an order list that lags behind orders and cancels. Whether
resting orders hold funds is still a guess until live_check.py reports it.
"""
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

BASE_URL = "https://fake-kalshi/trade-api/v2"
ORDER_FIELDS = {"ticker", "client_order_id", "side", "count", "price", "expiration_time", "time_in_force", "post_only",
                "self_trade_prevention_type", "cancel_order_on_pause", "reduce_only", "subaccount", "order_group_id",
                "exchange_index"}
REQUIRED = {"ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type"}


class FakeAuth:
    def sign_request(self, method, path):
        return {"KALSHI-ACCESS-KEY": "test-key", "KALSHI-ACCESS-SIGNATURE": "sig", "KALSHI-ACCESS-TIMESTAMP": "1"}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}: {self.text}", response=self)


def _error(status, code, message="", details=None):
    return FakeResponse(status, {"error": {"code": code, "message": message, **({"details": details} if details else {})}})


def _fp(n):
    return f"{n:.2f}"


def _dollars(cents):
    return f"{cents / 100:.4f}"


def taker_fee_cents(price_cents, count):
    p = price_cents / 100
    return math.ceil(round(0.07 * count * p * (1 - p) * 100, 9))


class FakeExchange:
    def __init__(self, balance_cents=5000):
        # Kalshi checks funds per exchange ("shard"), and coin markets are on 2.
        self.shards = {0: 0, 1: 0, 2: balance_cents, 3: 0}
        self.hide_breakdown = False      # the docs omit it for subaccount-restricted keys
        self.portfolio_value = 0
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self.positions: dict[str, float] = {}
        self.books: dict[str, dict] = {}
        self.markets: dict[str, dict] = {}
        self.requests: list[tuple] = []
        self.order_failures: list = []   # next POSTs to fail: an HTTP status, or an exception to raise
        self.lose_next_response = False  # the order goes through, but the answer never comes back
        self.read_failures: dict[str, Exception] = {}   # path -> raised on every GET of it
        self.page_size = 100
        self.post_only_cross_answers_empty = False
        self.order_list_lag_seconds = 0.0    # GET /portfolio/orders shows the book this long ago
        self.resting_holds_funds = True
        # As GET /account/endpoint_costs answered on 2026-11-04 (orders aren't
        # listed because they cost the default).
        self.endpoint_costs = [{"method": "DELETE", "path": "/trade-api/v2/portfolio/events/orders", "cost": 2},
                               {"method": "DELETE", "path": "/trade-api/v2/portfolio/events/orders/:order_id", "cost": 2},
                               {"method": "GET", "path": "/trade-api/v2/portfolio/orders/:order_id", "cost": 2}]
        self.on_fill = None              # called with each new fill, as the fill stream would be
        self._ids = 0

    # ---- test controls ----------------------------------------------------

    @property
    def balance(self):
        """What's on the exchange the coin markets trade on."""
        return self.shards[2]

    @balance.setter
    def balance(self, cents):
        self.shards[2] = cents

    @staticmethod
    def shard_of(ticker):
        return 2 if ticker.startswith("KX") else 0

    def set_book(self, ticker, bid=None, ask=None, bid_size=100, ask_size=100):
        self.books[ticker] = {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}

    def resting(self, ticker=None):
        return [o for o in self.orders.values() if o["status"] == "resting" and (not ticker or o["ticker"] == ticker)]

    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]

    def hit(self, order_id, count=None, price_cents=None, fee_cents=0):
        """Someone trades against a resting order. Returns the fill's REST row."""
        o = self.orders[order_id]
        count = o["remaining"] if count is None else count
        price = o["price"] if price_cents is None else price_cents
        o["remaining"] = round(o["remaining"] - count, 2)
        o["filled"] = round(o["filled"] + count, 2)
        if o["remaining"] <= 0:
            o["status"] = "executed"
        o["held"] = max(0, o["held"] - round(self._cost(o["side"], price) * count))
        return self._fill(o, count, price, is_taker=False, fee_cents=fee_cents)

    def ws_message(self, fill, sid=13):
        """The `fill` websocket message for one of this exchange's fills."""
        position = self.positions.get(fill["ticker"], 0)
        side = "yes" if fill["book_side"] == "bid" else "no"
        return {"type": "fill", "sending_ts_ms": fill["ts"] * 1000, "sid": sid, "msg": {
            "trade_id": fill["trade_id"], "order_id": fill["order_id"],
            "client_order_id": self.orders[fill["order_id"]]["client_order_id"],
            "market_ticker": fill["ticker"], "exchange_index": 0, "is_taker": fill["is_taker"],
            "side": side, "yes_price_dollars": fill["yes_price_dollars"], "count_fp": fill["count_fp"],
            "fee_cost": fill["fee_cost"], "action": "buy" if side == "yes" else "sell", "ts": fill["ts"],
            "ts_ms": fill["ts"] * 1000, "post_position_fp": _fp(position), "purchased_side": side,
            "outcome_side": side, "book_side": fill["book_side"]}}

    # ---- the session interface KalshiClient uses ---------------------------

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = urlparse(url).path.removeprefix("/trade-api/v2")
        self.requests.append((method, path, dict(params or {}), json, timeout))
        if path.startswith("/portfolio") and "KALSHI-ACCESS-KEY" not in (headers or {}):
            return _error(401, "authentication_error")
        if method == "POST" and path == "/portfolio/events/orders":
            return self._post_order(json or {})
        if method == "DELETE" and path.startswith("/portfolio/events/orders/"):
            return self._cancel(path.rsplit("/", 1)[1], params or {})
        if method == "GET":
            if path in self.read_failures:
                raise self.read_failures[path]
            if path == "/account/endpoint_costs":
                return FakeResponse(200, {"default_cost": 10, "endpoint_costs": self.endpoint_costs})
            if path == "/portfolio/balance":
                total = sum(self.shards.values())
                answer = {"balance": total, "balance_dollars": _dollars(total), "portfolio_value": self.portfolio_value,
                          "updated_ts": 1793800000}
                if not self.hide_breakdown:
                    answer["balance_breakdown"] = [{"balance": _dollars(c), "exchange_index": i} for i, c in self.shards.items()]
                return FakeResponse(200, answer)
            if path == "/portfolio/orders":
                return self._page(params or {}, self._orders_rows(params or {}), "orders")
            if path == "/portfolio/positions":
                return self._positions(params or {})
            if path == "/portfolio/fills":
                return self._fills_page(params or {})
            if path.startswith("/markets/"):
                ticker = path.rsplit("/", 1)[1]
                return FakeResponse(200, {"market": {"ticker": ticker, "status": "active", "result": "",
                                                     "exchange_index": self.shard_of(ticker),
                                                     **self.markets.get(ticker, {})}})
            if path == "/markets":
                series = (params or {}).get("series_ticker")
                tickers = ([t for t in self.books if t.split("-")[0] == series] if series
                           else (params or {}).get("tickers", "").split(","))
                return FakeResponse(200, {"markets": [self._market_row(t) for t in tickers if t], "cursor": ""})
        return _error(404, "not_found", f"{method} {path}")

    # ---- orders ------------------------------------------------------------

    def _post_order(self, body):
        if self.order_failures:
            failure = self.order_failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return _error(failure, "forced_failure")
        problem = self._invalid(body)
        if problem:
            return _error(400, "invalid_order", problem)
        side, price, count = body["side"], round(float(body["price"]) * 100), float(body["count"])
        book = self.books.get(body["ticker"], {})
        touch = book.get("ask") if side == "bid" else book.get("bid")
        crosses = touch is not None and (price >= touch if side == "bid" else price <= touch)
        if body.get("post_only") and crosses:
            if self.post_only_cross_answers_empty:
                return FakeResponse(201, {"order_id": "crossed", "client_order_id": body.get("client_order_id", ""),
                                          "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1793800000000})
            return _error(400, "invalid_order", "invalid order", "post only cross")
        held_cost = self._order_cost(body["ticker"], side, price, count)
        funds = self.shards[self.shard_of(body["ticker"])]
        if held_cost > funds and not funds:
            # Kalshi's real answer on 2026-11-04, for an account with no money on this exchange.
            return _error(404, "insufficient_shard_balance", "insufficient shard balance",
                          "Exchange user not found. For Predictions: reference documentation Exchange Sharding documentation.")
        if held_cost > funds:
            return _error(400, "insufficient_balance", "insufficient balance")

        self._ids += 1
        order_id = f"kalshi-order-{self._ids}"
        order = {"order_id": order_id, "client_order_id": body.get("client_order_id", ""), "ticker": body["ticker"],
                 "side": side, "price": price, "count": count, "remaining": count, "filled": 0.0,
                 "status": "resting", "tif": body["time_in_force"], "post_only": bool(body.get("post_only")),
                 "expiration_time": body.get("expiration_time"), "held": 0, "created_at": time.time(), "ended_at": None,
                 "cancel_on_pause": body.get("cancel_order_on_pause"),
                 "stp": body["self_trade_prevention_type"]}
        self.orders[order_id] = order
        fill_count, fill_cost, fee = 0.0, 0, 0
        if crosses:
            size_key = "ask_size" if side == "bid" else "bid_size"
            fill_count = min(count, book[size_key])
            if fill_count:
                fee = taker_fee_cents(touch, fill_count)
                fill_cost = touch * fill_count
                self._fill(order, fill_count, touch, is_taker=True, fee_cents=fee)
                order["remaining"] = round(count - fill_count, 2)
                order["filled"] = fill_count
                book[size_key] -= fill_count
        if order["remaining"] <= 0:
            order["status"] = "executed"
        elif body["time_in_force"] == "immediate_or_cancel":
            order["status"] = "canceled"
            order["remaining"] = 0.0
        else:
            if self.resting_holds_funds:
                order["held"] = round(self._cost(side, price) * order["remaining"])
            self.shards[self.shard_of(body["ticker"])] -= order["held"]
        if self.lose_next_response:
            self.lose_next_response = False
            raise requests.Timeout("read timed out")
        answer = {"order_id": order_id, "client_order_id": order["client_order_id"], "fill_count": _fp(fill_count),
                  "remaining_count": _fp(order["remaining"]), "ts_ms": 1793800000000}
        if fill_count:
            answer["average_fill_price"] = _dollars(fill_cost / fill_count)
            answer["average_fee_paid"] = f"{fee / fill_count / 100:.6f}"
        return FakeResponse(201, answer)

    def _invalid(self, body):
        missing = REQUIRED - set(body)
        unknown = set(body) - ORDER_FIELDS
        if missing or unknown:
            return f"missing {sorted(missing)}, unknown {sorted(unknown)}"
        if body["side"] not in ("bid", "ask"):
            return "side must be bid or ask"
        if not isinstance(body["count"], str) or not re.fullmatch(r"\d+(\.\d{1,2})?", body["count"]) \
                or float(body["count"]) <= 0:
            return "count must be a fixed-point string"
        if not isinstance(body["price"], str) or not re.fullmatch(r"0\.\d{2,4}", body["price"]):
            return "price must be a fixed-point dollar string"
        cents = float(body["price"]) * 100
        if abs(cents - round(cents)) > 1e-9 or not 1 <= round(cents) <= 99:
            return "off-grid price"              # KXBTCD is linear_cent
        if body["time_in_force"] not in ("fill_or_kill", "good_till_canceled", "immediate_or_cancel"):
            return "bad time_in_force"
        if body["self_trade_prevention_type"] not in ("taker_at_cross", "maker"):
            return "bad self_trade_prevention_type"
        if body["time_in_force"] == "immediate_or_cancel" and "expiration_time" in body:
            return "immediate_or_cancel cannot take expiration_time"
        if body.get("reduce_only") and body["time_in_force"] != "immediate_or_cancel":
            return "reduce_only needs immediate_or_cancel"
        return None

    @staticmethod
    def _cost(side, price):
        return price if side == "bid" else 100 - price

    def _order_cost(self, ticker, side, price, count):
        """Cents needed up front; closing part of a position needs none."""
        pos = self.positions.get(ticker, 0)
        reducing = min(count, abs(pos)) if (pos > 0) == (side == "ask") and pos else 0
        return self._cost(side, price) * (count - reducing)

    def _cancel(self, order_id, params):
        if "market_ticker" not in params:
            return _error(400, "missing_market_ticker", "an order_id alone cannot identify the exchange shard")
        o = self.orders.get(order_id)
        if not o or o["status"] != "resting" or o["ticker"] != params["market_ticker"]:
            return _error(404, "not_found")
        self.shards[self.shard_of(o["ticker"])] += o["held"]
        o["held"] = 0
        reduced = o["remaining"]
        o["remaining"] = 0.0
        o["status"] = "canceled"
        o["ended_at"] = time.time()
        return FakeResponse(200, {"order_id": order_id, "client_order_id": o["client_order_id"],
                                  "reduced_by": _fp(reduced), "ts_ms": 1793800000000})

    def _fill(self, order, count, price, is_taker, fee_cents):
        signed = count if order["side"] == "bid" else -count
        before = self.positions.get(order["ticker"], 0)
        after = round(before + signed, 2)
        self.positions[order["ticker"]] = after
        if after == 0:
            del self.positions[order["ticker"]]
        shard = self.shard_of(order["ticker"])
        if is_taker:
            self.shards[shard] -= round(self._cost(order["side"], price) * count) + fee_cents
        else:
            self.shards[shard] -= fee_cents
        self._ids += 1
        now = datetime.now(timezone.utc)
        side = "yes" if order["side"] == "bid" else "no"
        row = {"fill_id": f"trade-{self._ids}", "trade_id": f"trade-{self._ids}", "exchange_index": 0,
               "order_id": order["order_id"], "ticker": order["ticker"], "market_ticker": order["ticker"],
               "outcome_side": side, "book_side": order["side"], "side": side,
               "action": "buy" if side == "yes" else "sell", "count_fp": _fp(count),
               "yes_price_dollars": _dollars(price), "no_price_dollars": _dollars(100 - price),
               "is_taker": is_taker, "fee_cost": f"{fee_cents / 100:.6f}",
               "created_time": now.isoformat().replace("+00:00", "Z"), "ts": int(now.timestamp())}
        self.fills.append(row)
        if self.on_fill:
            self.on_fill(row)
        return row

    # ---- reads -------------------------------------------------------------

    def _page(self, params, rows, key, extra=None):
        limit = min(int(params.get("limit", 100)), self.page_size)
        start = int(params.get("cursor") or 0)
        page = rows[start:start + limit]
        cursor = str(start + limit) if start + limit < len(rows) else ""
        return FakeResponse(200, {key: page, "cursor": cursor, **(extra or {})})

    def _orders_rows(self, params):
        rows = []
        seen_at = time.time() - self.order_list_lag_seconds
        for o in self.orders.values():
            if o["created_at"] > seen_at:
                continue                     # too new to be on the list yet
            cancelled_since = o["ended_at"] is not None and o["ended_at"] > seen_at
            status = "resting" if cancelled_since else o["status"]
            if params.get("status") and status != params["status"]:
                continue
            if params.get("ticker") and o["ticker"] != params["ticker"]:
                continue
            rows.append({"order_id": o["order_id"], "user_id": "u1", "client_order_id": o["client_order_id"],
                         "ticker": o["ticker"], "outcome_side": "yes" if o["side"] == "bid" else "no",
                         "book_side": o["side"], "type": "limit", "status": o["status"],
                         "yes_price_dollars": _dollars(o["price"]), "no_price_dollars": _dollars(100 - o["price"]),
                         "fill_count_fp": _fp(o["filled"]), "remaining_count_fp": _fp(o["remaining"]),
                         "initial_count_fp": _fp(o["count"]), "taker_fees_dollars": "0.0000",
                         "maker_fees_dollars": "0.0000", "taker_fill_cost_dollars": "0.0000",
                         "maker_fill_cost_dollars": "0.0000"})
        return rows

    def _positions(self, params):
        rows = [{"ticker": t, "exchange_index": 0, "total_traded_dollars": "1.0000", "position_fp": _fp(n),
                 "market_exposure_dollars": "1.0000", "realized_pnl_dollars": "0.0000",
                 "fees_paid_dollars": "0.0000", "last_updated_ts": "2026-11-04T12:00:00Z",
                 "netting_enabled": False}
                for t, n in self.positions.items() if not params.get("ticker") or t == params["ticker"]]
        return self._page(params, rows, "market_positions", {"event_positions": []})

    def _fills_page(self, params):
        rows = list(self.fills)
        if params.get("min_ts"):
            rows = [f for f in rows if f["ts"] >= int(params["min_ts"])]
        if params.get("order_id"):
            rows = [f for f in rows if f["order_id"] == params["order_id"]]
        return self._page(params, rows[::-1], "fills")      # newest first

    def _market_row(self, ticker):
        book = self.books.get(ticker, {})
        row = {"ticker": ticker, "status": "active", "result": "", "exchange_index": self.shard_of(ticker),
               **self.markets.get(ticker, {})}
        if book.get("bid") is not None:
            row.update(yes_bid_dollars=_dollars(book["bid"]), yes_bid_size_fp=_fp(book["bid_size"]))
        if book.get("ask") is not None:
            row.update(yes_ask_dollars=_dollars(book["ask"]), yes_ask_size_fp=_fp(book["ask_size"]))
        return row
