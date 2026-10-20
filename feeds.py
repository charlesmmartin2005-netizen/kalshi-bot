"""
Streaming market data, so the bot reprices on every tick instead of polling.

SPOT INDEX (no login needed)
  Websockets to Coinbase, Kraken, Bitstamp and Gemini stream best bid/ask
  for BTC-USD and ETH-USD. The index is the median of the fresh exchange
  mids. Kalshi settles on CF Benchmarks' RTI (a multi-exchange index); in
  research/index_check.py over 124 real settlements, a blend matched it
  ~20% better than Coinbase alone (BTC: 0.58bp vs 0.73bp average error).
  Every update sets HUB.changed, which wakes the engine.

KALSHI BOOK + TRADES
  Kalshi's websocket requires an API key (unauthenticated connections get
  401). With a key, KalshiStream keeps every watched book live from
  orderbook snapshots + deltas and streams the public trade tape (paper
  fills) — the engine then needs no REST polling for market data.
  Without a key the engine falls back to fast REST polling. NOTE: the
  Kalshi stream could not be tested without a key; it parses both the
  cents and the dollar-string message forms and falls back to REST if it
  drops or goes quiet.
"""
import json
import logging
import statistics
import threading
import time
from datetime import datetime, timezone

import websocket

log = logging.getLogger("feeds")

PRODUCTS = ("BTC-USD", "ETH-USD")
STALE_SECONDS = 10          # an exchange quote older than this is left out of the index
RECONNECT_MAX_SECONDS = 30


class SpotHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.quotes: dict[str, dict[str, tuple[float, float]]] = {p: {} for p in PRODUCTS}  # product -> exch -> (mid, ts)
        self.status: dict[str, dict] = {}       # exchange -> {connected, msgs, last, error}
        self.changed = threading.Event()
        self.listeners: list[threading.Event] = []   # also set on every index change (engine wake-up)
        self.last_update = 0.0
        self._feeds = []
        self.started = False

    # ---- updates from feed threads ------------------------------------------
    def update(self, exchange, product, bid, ask):
        if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
            return
        mid, now = (bid + ask) / 2, time.time()
        with self.lock:
            prev = self.quotes[product].get(exchange)
            self.quotes[product][exchange] = (mid, now)
            st = self.status.setdefault(exchange, {})
            st["msgs"] = st.get("msgs", 0) + 1
            st["last"] = now
        if not prev or prev[0] != mid:
            self.last_update = now
            self.changed.set()
            for ev in self.listeners:
                ev.set()

    # ---- reads -----------------------------------------------------------------
    def index(self, product) -> float | None:
        """Median of fresh exchange mids (None if no exchange is fresh)."""
        cutoff = time.time() - STALE_SECONDS
        with self.lock:
            mids = [m for m, ts in self.quotes.get(product, {}).values() if ts >= cutoff]
        return statistics.median(mids) if mids else None

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            ex = {name: {"connected": st.get("connected", False), "msgs": st.get("msgs", 0),
                         "age": round(now - st["last"], 1) if st.get("last") else None,
                         "error": st.get("error")}
                  for name, st in self.status.items()}
            quotes = {p: {e: {"mid": round(m, 2), "age": round(now - ts, 1)} for e, (m, ts) in q.items()}
                      for p, q in self.quotes.items()}
        return {"exchanges": ex, "quotes": quotes,
                "index": {p: (round(i, 2) if (i := self.index(p)) else None) for p in PRODUCTS}}

    # ---- lifecycle -------------------------------------------------------------
    def start(self):
        if self.started:
            return
        self.started = True
        for feed in (CoinbaseFeed, KrakenFeed, BitstampFeed, GeminiFeed):
            for f in feed.make(self):
                self._feeds.append(f)
                f.start()
        log.info("Spot feeds started: %s", ", ".join(f.name for f in self._feeds))


class _WsThread(threading.Thread):
    """A websocket that reconnects forever with backoff."""
    url = ""
    exchange = ""

    def __init__(self, hub, name=None, url=None):
        super().__init__(name=name or self.exchange, daemon=True)
        self.hub = hub
        self.url = url or self.url
        self._backoff = 1

    @classmethod
    def make(cls, hub):
        return [cls(hub)]

    def subscriptions(self):
        return []

    def handle(self, msg):
        raise NotImplementedError

    def _status(self, **kw):
        with self.hub.lock:
            self.hub.status.setdefault(self.exchange, {}).update(kw)

    def run(self):
        while True:
            try:
                ws = websocket.WebSocketApp(
                    self.url, header=self.headers(),
                    on_open=self._on_open, on_message=lambda _ws, m: self._on_message(m),
                    on_error=lambda _ws, e: self._status(error=str(e)[:120]))
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self._status(error=str(e)[:120])
            self._status(connected=False)
            time.sleep(self._backoff)
            self._backoff = min(RECONNECT_MAX_SECONDS, self._backoff * 2)

    def headers(self):
        return None

    def _on_open(self, ws):
        self._backoff = 1
        self._status(connected=True, error=None)
        for s in self.subscriptions():
            ws.send(json.dumps(s))

    def _on_message(self, raw):
        try:
            self.handle(json.loads(raw))
        except Exception as e:      # never let one bad message kill the feed
            log.debug("%s: bad message (%s): %s", self.exchange, e, raw[:200])


class CoinbaseFeed(_WsThread):
    url, exchange = "wss://ws-feed.exchange.coinbase.com", "coinbase"

    def subscriptions(self):
        return [{"type": "subscribe", "product_ids": list(PRODUCTS), "channels": ["ticker"]}]

    def handle(self, m):
        if m.get("type") == "ticker" and m.get("product_id") in PRODUCTS:
            self.hub.update(self.exchange, m["product_id"], float(m["best_bid"]), float(m["best_ask"]))


class KrakenFeed(_WsThread):
    url, exchange = "wss://ws.kraken.com/v2", "kraken"
    SYMBOLS = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}

    def subscriptions(self):
        return [{"method": "subscribe", "params": {"channel": "ticker", "symbol": list(self.SYMBOLS)}}]

    def handle(self, m):
        if m.get("channel") == "ticker":
            for d in m.get("data", []):
                if d.get("symbol") in self.SYMBOLS:
                    self.hub.update(self.exchange, self.SYMBOLS[d["symbol"]], float(d["bid"]), float(d["ask"]))


class BitstampFeed(_WsThread):
    url, exchange = "wss://ws.bitstamp.net", "bitstamp"
    CHANNELS = {"order_book_btcusd": "BTC-USD", "order_book_ethusd": "ETH-USD"}

    def subscriptions(self):
        return [{"event": "bts:subscribe", "data": {"channel": c}} for c in self.CHANNELS]

    def handle(self, m):
        product = self.CHANNELS.get(m.get("channel"))
        d = m.get("data") or {}
        if product and d.get("bids") and d.get("asks"):
            self.hub.update(self.exchange, product, float(d["bids"][0][0]), float(d["asks"][0][0]))


class GeminiFeed(_WsThread):
    """Gemini's v1 market-data socket is one symbol per connection."""
    exchange = "gemini"
    SYMBOLS = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"}

    @classmethod
    def make(cls, hub):
        return [cls(hub, name=f"gemini-{s}", url=f"wss://api.gemini.com/v1/marketdata/{s}"
                                                f"?top_of_book=true&bids=true&offers=true&trades=false")
                for s in cls.SYMBOLS]

    def __init__(self, hub, name=None, url=None):
        super().__init__(hub, name, url)
        self.product = self.SYMBOLS[url.split("/marketdata/")[1].split("?")[0]]
        self.bid = self.ask = None

    def handle(self, m):
        for e in m.get("events", []):
            if e.get("type") != "change" or float(e.get("remaining", 0)) <= 0:
                continue
            if e["side"] == "bid":
                self.bid = float(e["price"])
            elif e["side"] == "ask":
                self.ask = float(e["price"])
        if self.bid and self.ask:
            self.hub.update(self.exchange, self.product, self.bid, self.ask)


HUB = SpotHub()


# ---- Kalshi (needs an API key) -------------------------------------------------------

def _cents(v):
    """Kalshi sends prices as int cents or dollar strings depending on field."""
    if v is None:
        return None
    if isinstance(v, str):
        return round(float(v) * 100)
    return int(v) if v >= 1 or v == 0 else round(v * 100)


class KalshiStream(_WsThread):
    """Live order books + trade tape for the watched markets."""
    exchange = "kalshi"
    PATH = "/trade-api/ws/v2"

    def __init__(self, auth, url, tickers):
        super().__init__(hub=HUB, name="kalshi-ws", url=url)
        self.auth = auth
        self.tickers = list(tickers)
        self.books: dict[str, dict] = {}        # ticker -> {"yes": {px: qty}, "no": {px: qty}}
        self.trades: dict[str, list] = {}       # ticker -> REST-shaped trade rows (drained by engine)
        self.changed = threading.Event()
        self.lock = threading.Lock()
        self.last_msg = 0.0
        self._ws = None
        self._seq = {}

    def headers(self):
        h = self.auth.sign_request("GET", self.PATH)
        return [f"{k}: {v}" for k, v in h.items()]

    def subscriptions(self):
        if not self.tickers:
            return []
        return [{"id": 1, "cmd": "subscribe",
                 "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": self.tickers}}]

    def set_tickers(self, tickers):
        """Resubscribe when the watchlist changes (simplest robust way: reconnect)."""
        if sorted(tickers) == sorted(self.tickers):
            return
        self.tickers = list(tickers)
        with self.lock:
            self.books = {t: b for t, b in self.books.items() if t in self.tickers}
        if self._ws_app:
            self._ws_app.close()

    _ws_app = None

    def run(self):
        while True:
            try:
                self._ws_app = websocket.WebSocketApp(
                    self.url, header=self.headers(), on_open=self._on_open,
                    on_message=lambda _ws, m: self._on_message(m),
                    on_error=lambda _ws, e: self._status(error=str(e)[:120]))
                self._ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self._status(error=str(e)[:120])
            self._status(connected=False)
            with self.lock:
                self.books.clear()          # stale after a disconnect; engine falls back to REST
            time.sleep(self._backoff)
            self._backoff = min(RECONNECT_MAX_SECONDS, self._backoff * 2)

    @property
    def healthy(self) -> bool:
        return bool(self.hub.status.get("kalshi", {}).get("connected")) and time.time() - self.last_msg < 30

    def handle(self, m):
        typ, msg = m.get("type"), m.get("msg") or {}
        self.last_msg = time.time()
        with self.hub.lock:
            st = self.hub.status.setdefault("kalshi", {})
            st["msgs"] = st.get("msgs", 0) + 1
            st["last"] = self.last_msg
        t = msg.get("market_ticker")
        if typ == "orderbook_snapshot" and t:
            book = {"yes": {}, "no": {}}
            for side in ("yes", "no"):
                levels = msg.get(f"{side}_dollars_fp") or msg.get(f"{side}_dollars") or msg.get(side) or []
                for px, qty in levels:
                    book[side][_cents(px)] = float(qty)
            with self.lock:
                self.books[t] = book
            self._seq[m.get("sid")] = m.get("seq")
            self.changed.set()
        elif typ == "orderbook_delta" and t:
            sid, seq = m.get("sid"), m.get("seq")
            if sid in self._seq and seq is not None and self._seq[sid] is not None and seq != self._seq[sid] + 1:
                log.warning("Kalshi book sequence gap (%s→%s) — resyncing", self._seq[sid], seq)
                if self._ws_app:
                    self._ws_app.close()
                return
            self._seq[sid] = seq
            px = _cents(msg.get("price_dollars") or msg.get("price"))
            delta = float(msg.get("delta_fp") or msg.get("delta") or 0)
            side = msg.get("side")
            with self.lock:
                levels = self.books.setdefault(t, {"yes": {}, "no": {}})[side]
                levels[px] = levels.get(px, 0) + delta
                if levels[px] <= 0:
                    del levels[px]
            self.changed.set()
        elif typ == "trade" and t:
            ts = msg.get("ts")
            row = {"trade_id": msg.get("trade_id") or f"{t}-{ts}-{msg.get('yes_price')}-{msg.get('count')}",
                   "created_time": datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(),
                   "yes_price_dollars": msg.get("yes_price_dollars") or f"{_cents(msg.get('yes_price')) / 100:.4f}",
                   "count_fp": msg.get("count_fp") or str(msg.get("count", 0)),
                   "taker_side": msg.get("taker_side")}
            with self.lock:
                self.trades.setdefault(t, []).append(row)
        elif typ == "error":
            self._status(error=str(msg)[:120])

    def book(self, ticker) -> dict | None:
        """normalize_orderbook-shaped book, or None if we don't have one."""
        with self.lock:
            b = self.books.get(ticker)
            if not b:
                return None
            yes = sorted(b["yes"].items(), reverse=True)
            no = sorted(b["no"].items(), reverse=True)
        bid = yes[0][0] if yes else None
        ask = 100 - no[0][0] if no else None
        return {"yes_bid": bid, "yes_ask": ask,
                "spread_cents": ask - bid if bid is not None and ask is not None else None,
                "raw_yes_levels": yes, "raw_no_levels": no}

    def drain_trades(self, ticker) -> list:
        with self.lock:
            return self.trades.pop(ticker, [])
