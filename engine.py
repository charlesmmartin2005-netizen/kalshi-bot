"""
The bot's main loop, wrapped so it can run either in the foreground
(main.py) or on a background thread behind the dashboard (dashboard.py).

The engine owns one KalshiClient / RiskManager / MarketMaker and keeps a
live snapshot of everything the dashboard shows: books, resting quotes,
risk state, and a rolling log. It adds no trading logic of its own — every
quote/cancel decision is still MarketMaker's, every limit is RiskManager's.
"""
import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import fair_value
from config import CONFIG, BotConfig
from kalshi_client import build_client, normalize_orderbook, top_of_book
from market_maker import MarketMaker
from risk_manager import RiskManager

log = logging.getLogger("engine")

PRICE_HISTORY_POINTS = 180   # per ticker, one point per cycle
LOG_LINES = 300
ROTATE_SECONDS = 900         # auto-rotate re-centres the strikes this often (strategy: rolling ladder)
ROLLED_OFF = "rolled off — working position down"
DEPTH_EVERY = 5              # full orderbooks (dashboard depth ladder) every Nth cycle
FETCH_WORKERS = 12


class _RingLogHandler(logging.Handler):
    def __init__(self, buffer):
        super().__init__(level=logging.INFO)
        self.buffer = buffer

    def emit(self, record):
        self.buffer.append({
            "ts": record.created,
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        })


class Engine:
    def __init__(self, config: BotConfig = CONFIG):
        self.cfg = config
        self.client = build_client(config)
        self.risk = RiskManager(config.risk)
        self.maker = MarketMaker(self.client, self.risk, config)

        self.lock = threading.RLock()
        self.running = False
        self.started_at = None
        self.cycles = 0
        self.last_cycle_at = None
        self.last_error = None
        self._thread = None
        self._stop = threading.Event()

        self.books: dict[str, dict] = {}
        self.history: dict[str, collections.deque] = {}
        self.market_info: dict[str, dict] = {}
        self._depth: dict[str, tuple] = {}            # ticker -> last (yes levels, no levels)
        self._pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch")
        # On by default: the strategy depends on staying near the money.
        self.auto_rotate = True
        self._last_rotate = 0.0
        self.last_autopick = None
        self.logs = collections.deque(maxlen=LOG_LINES)
        logging.getLogger().addHandler(_RingLogHandler(self.logs))

    # ---- control -----------------------------------------------------------

    @property
    def mode(self) -> str:
        return "PAPER" if self.cfg.kalshi.paper_trading else "LIVE"

    def start(self) -> tuple[bool, str]:
        if not self.cfg.watched_tickers and self.auto_rotate and not self.running:
            try:
                self.autopick()
            except Exception as e:
                log.error("Auto-pick on start failed: %s", e)
        with self.lock:
            if self.running:
                return False, "already running"
            if self.risk.halted:
                return False, f"halted: {self.risk.halt_reason} — reset the kill switch first"
            if not self.cfg.watched_tickers:
                return False, "no watched tickers — add one first"
            self.running = True
            self.started_at = time.time()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
            self._thread.start()
        log.info("Engine started in %s mode. Watching: %s", self.mode, self.cfg.watched_tickers)
        if self.mode == "LIVE":
            log.warning("LIVE MODE — real orders will be sent.")
        return True, "started"

    def stop(self, reason: str = "stopped by user") -> tuple[bool, str]:
        if not self.running:
            return False, "not running"
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)
        with self.lock:
            self.maker.cancel_all(reason)
            self.running = False
        log.info("Engine stopped (%s)", reason)
        return True, "stopped"

    def kill(self) -> tuple[bool, str]:
        """Manual kill switch: halt the risk manager and pull every quote."""
        self.risk._halt("manual kill switch")
        if self.running:
            self.stop("kill switch")
        else:
            with self.lock:
                self.maker.cancel_all("kill switch")
        return True, "halted"

    def reset_halt(self) -> tuple[bool, str]:
        with self.lock:
            self.risk.halted = False
            self.risk.halt_reason = ""
        log.info("Kill switch reset")
        return True, "reset"

    def add_ticker(self, ticker: str) -> tuple[bool, str]:
        ticker = ticker.strip().upper()
        if not ticker:
            return False, "empty ticker"
        try:
            market = self.client.get_market(ticker).get("market", {})
        except Exception as e:
            return False, f"couldn't find {ticker}: {e}"
        with self.lock:
            self.market_info[ticker] = market
            if ticker not in self.cfg.watched_tickers:
                self.cfg.watched_tickers.append(ticker)
        log.info("Now watching %s", ticker)
        return True, "added"

    def autopick(self) -> tuple[bool, str]:
        """Replace the watchlist with autopick's choices. Markets the bot
        still holds a position in are kept, so it can finish exiting."""
        import autopick
        res = autopick.pick(self.client)
        picks = [p["ticker"] for p in res["picks"]]
        if not picks:
            return False, "no suitable markets found: " + "; ".join(res["notes"])
        with self.lock:
            current = list(self.cfg.watched_tickers)
        kept = [t for t in current if t not in picks and self.risk.net(t) != 0]
        removed = [t for t in current if t not in picks and t not in kept]
        for t in removed:
            self.remove_ticker(t)
        with self.lock:
            # Rolled-off markets with a position only work it down; a market
            # that gets picked again trades both ways again.
            for t in kept:
                self.risk.reduce_only.setdefault(t, ROLLED_OFF)
            for t in picks:
                if self.risk.reduce_only.get(t) == ROLLED_OFF:
                    del self.risk.reduce_only[t]
        added = []
        for t in picks:
            if t not in current:
                ok, _ = self.add_ticker(t)
                if ok:
                    added.append(t)
        self.last_autopick = {**res, "added": added, "removed": removed, "kept": kept}
        msg = f"watching {len(picks)} picks (+{len(added)} / −{len(removed)})"
        if kept:
            msg += f", kept {len(kept)} with open positions"
        if res["notes"]:
            msg += " · " + "; ".join(res["notes"])
        log.info("Auto-pick: %s", msg)
        return True, msg

    def set_auto_rotate(self, on: bool) -> tuple[bool, str]:
        self.auto_rotate = bool(on)
        self._last_rotate = 0.0          # rotate on the next cycle
        log.info("Auto-rotate %s", "on" if on else "off")
        return True, "on" if on else "off"

    def _maybe_rotate(self):
        """Every ROTATE_SECONDS, or as soon as a watched market is within 2h
        of close (where the model stops trading), re-run autopick."""
        if not self.auto_rotate:
            return
        closing = any(
            (info := self.market_info.get(t, {})).get("close_time")
            and (datetime.fromisoformat(info["close_time"].replace("Z", "+00:00"))
                 - datetime.now(timezone.utc)).total_seconds() < 2 * 3600
            for t in list(self.cfg.watched_tickers))
        if closing or time.time() - self._last_rotate > ROTATE_SECONDS:
            self._last_rotate = time.time()
            try:
                self.autopick()
            except Exception as e:
                log.error("Auto-rotate failed: %s", e)

    def remove_ticker(self, ticker: str) -> tuple[bool, str]:
        with self.lock:
            if ticker not in self.cfg.watched_tickers:
                return False, "not watched"
            self.maker._cancel_resting(ticker, reason="removed from watchlist")
            self.cfg.watched_tickers.remove(ticker)
            self.books.pop(ticker, None)
            self.maker.deltas.pop(ticker, None)
        log.info("Stopped watching %s", ticker)
        return True, "removed"

    # ---- loop --------------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            if self.risk.halted:
                log.critical("Bot is halted (%s). Cancelling all resting orders.",
                             self.risk.halt_reason)
                with self.lock:
                    self.maker.cancel_all("kill switch")
                    self.running = False
                return
            self._maybe_rotate()
            self.run_cycle()
            self._stop.wait(self.cfg.loop_interval_seconds)

    def _prefetch(self, tickers):
        """Everything one cycle needs, fetched in parallel (the client's
        shared throttle still caps the request rate):
          - best bid/ask for every market in ONE request (also refreshes
            market info),
          - each market's trade tape (paper fills),
          - full depth for the dashboard ladder, only every DEPTH_EVERY cycles,
          - the coins' spot prices, so the model never waits on them."""
        paper = self.cfg.kalshi.paper_trading
        depth_due = self.cycles % DEPTH_EVERY == 0
        products = {fair_value.CRYPTO_SERIES[s] for s in (t.split("-")[0] for t in tickers)
                    if s in fair_value.CRYPTO_SERIES}
        tops = self._pool.submit(self.client.get_markets_batch, tickers)
        trades = {t: self._pool.submit(self.client.get_trades, t, min_ts=self.started_at - 5)
                  for t in tickers} if paper else {}
        depth = {t: self._pool.submit(self.client.get_orderbook, t) for t in tickers} if depth_due else {}
        spots = [self._pool.submit(fair_value.crypto_spot, p) for p in products]
        rows = {m["ticker"]: m for m in tops.result()}
        for f in spots:
            try:
                f.result()
            except Exception as e:     # fair_value retries and reports it per market
                log.warning("Spot price fetch failed: %s", e)
        return rows, trades, depth

    def run_cycle(self):
        with self.lock:
            tickers = list(self.cfg.watched_tickers)
        marks = {}
        try:
            rows, trades, depth = self._prefetch(tickers)
        except Exception as e:
            self.last_error = f"fetch: {e}"
            log.exception("Cycle fetch failed: %s", e)
            self.risk.record_api_error(self.client.consecutive_errors)
            return
        for ticker in tickers:
            try:
                if ticker in rows:
                    self.market_info[ticker] = rows[ticker]
                elif ticker not in self.market_info:
                    self.market_info[ticker] = self.client.get_market(ticker).get("market", {})
                if ticker in depth:
                    book = normalize_orderbook(depth[ticker].result())
                    self._depth[ticker] = (book["raw_yes_levels"], book["raw_no_levels"])
                elif ticker in rows:
                    book = top_of_book(rows[ticker])
                    book["raw_yes_levels"], book["raw_no_levels"] = self._depth.get(ticker, ([], []))
                else:
                    book = None                       # evaluate_market fetches it
                if ticker in trades:
                    # Fill first, against the quotes that were resting while
                    # these trades happened; then reprice with the new position.
                    # Only trades since the engine started can fill our quotes.
                    t_rows = trades[ticker].result()
                    with self.lock:
                        self.maker.process_trades(ticker, t_rows)
                with self.lock:
                    book = self.maker.evaluate_market(ticker, self.market_info[ticker], book)
                self._record_book(ticker, book)
                if book["yes_bid"] is not None and book["yes_ask"] is not None:
                    marks[ticker] = (book["yes_bid"] + book["yes_ask"]) / 2
                self.last_error = None
            except Exception as e:
                self.last_error = f"{ticker}: {e}"
                log.exception("Error evaluating %s: %s", ticker, e)
                self.risk.record_api_error(self.client.consecutive_errors)
        with self.lock:
            self.risk.update_marks(marks)
        self.cycles += 1
        self.last_cycle_at = time.time()

    def _record_book(self, ticker, book):
        bid, ask = book["yes_bid"], book["yes_ask"]
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        # Top of book for the depth ladder: YES bids as-is, NO bids flipped
        # into YES asks (a NO bid at P is a YES ask at 100 - P).
        bids = sorted(book["raw_yes_levels"], key=lambda l: -l[0])[:6]
        asks = sorted(((100 - p, s) for p, s in book["raw_no_levels"]), key=lambda l: l[0])[:6]
        with self.lock:
            self.books[ticker] = {
                "yes_bid": bid, "yes_ask": ask, "spread": book["spread_cents"], "mid": mid,
                "bids": bids, "asks": asks, "updated": time.time(),
            }
            hist = self.history.setdefault(ticker, collections.deque(maxlen=PRICE_HISTORY_POINTS))
            fv = self.maker.last_plan.get(ticker, {}).get("fv")
            hist.append({"ts": time.time(), "bid": bid, "ask": ask, "mid": mid, "fv": fv})

    def run_forever(self):
        """Foreground mode for main.py — blocks until Ctrl+C or a halt."""
        ok, msg = self.start()
        if not ok:
            log.error("Can't start: %s", msg)
            return
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("Shutting down — cancelling all resting orders.")
            self.stop("shutdown")

    # ---- snapshot for the dashboard ---------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            markets = []
            for t in self.cfg.watched_tickers:
                info = self.market_info.get(t, {})
                pos = self.risk.positions.get(t)
                markets.append({
                    "ticker": t,
                    "title": info.get("title", ""),
                    "subtitle": info.get("yes_sub_title", ""),
                    "close_time": info.get("close_time"),
                    "book": self.books.get(t),
                    "history": list(self.history.get(t, [])),
                    "quotes": [
                        {"action": q.action, "side": q.side, "price": q.price_cents,
                         "count": q.count, "age": round(time.time() - q.placed_at, 1)}
                        for q in self.maker.resting_quotes.get(t, [])
                    ],
                    "position": pos.net_contracts if pos else 0,
                    "market_pnl_usd": round(self.risk.market_pnl_usd(t), 2),
                    "plan": self.maker.last_plan.get(t, {}),
                    "reduce_only": self.risk.reduce_only.get(t),
                })
            r = self.cfg.risk
            return {
                "mode": self.mode,
                "running": self.running,
                "authenticated": self.client.auth is not None,
                "started_at": self.started_at,
                "cycles": self.cycles,
                "last_cycle_at": self.last_cycle_at,
                "last_error": self.last_error,
                "min_spread": self.maker.min_required_spread(),
                "auto_rotate": self.auto_rotate,
                "last_autopick": ({k: self.last_autopick[k] for k in ("ts", "notes", "added", "removed", "kept")}
                                  if self.last_autopick else None),
                "risk": {
                    "halted": self.risk.halted,
                    "halt_reason": self.risk.halt_reason,
                    "exposure_usd": self.risk.total_exposure_usd(),
                    "max_exposure_usd": r.max_total_exposure_usd,
                    "daily_pnl_usd": self.risk.daily_pnl_usd,
                    "session_pnl_usd": self.risk.session_pnl_usd,
                    "max_market_loss_usd": r.max_single_market_loss_usd,
                    "max_daily_loss_usd": r.max_daily_loss_usd,
                    "max_position": r.max_position_per_market,
                    "api_errors": self.client.consecutive_errors,
                    "max_api_errors": r.max_consecutive_errors,
                },
                "params": {
                    "min_edge_cents": r.min_edge_cents, "quote_inset_cents": r.quote_inset_cents,
                    "quote_size": r.quote_size, "max_quote_age_seconds": r.max_quote_age_seconds,
                    "inventory_skew_cents": r.inventory_skew_cents,
                    "delta_lean_cents": r.delta_lean_cents, "delta_lean_ref_usd": r.delta_lean_ref_usd,
                    "take_size": r.take_size,
                    "enable_taking": r.enable_taking, "take_edge_cents": r.take_edge_cents,
                    "loop_interval_seconds": self.cfg.loop_interval_seconds,
                },
                "markets": markets,
                "logs": list(self.logs)[-120:],
            }
