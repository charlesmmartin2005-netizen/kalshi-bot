"""
The bot's main loop, wrapped so it can run either in the foreground
(main.py) or on a background thread behind the dashboard (dashboard.py).

The engine owns one KalshiClient / RiskManager / MarketMaker and keeps a
live snapshot of everything the dashboard shows: books, resting quotes,
risk state, and a rolling log. It adds no trading logic of its own — every
quote/cancel decision is still MarketMaker's, every limit is RiskManager's.
"""
import collections
import ctypes
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import autopick
import feeds
import ledger
import pnl
from config import CONFIG, BotConfig
from fair_value import CRYPTO_MIN_HOURS_TO_TRADE
from kalshi_client import build_client, normalize_orderbook, top_of_book
from market_maker import MarketMaker
from risk_manager import RiskManager

log = logging.getLogger("engine")

# A halt (kill switch or loss limit) is written here and survives restarts:
# with run-bot.ps1 restarting the dashboard and --autostart, a crash or a
# reboot would otherwise quietly resume trading. Reset on the dashboard.
HALT_FILE = Path(__file__).parent / "halted.txt"
PRICE_HISTORY_POINTS = 180   # per ticker
HISTORY_EVERY_SECONDS = 1.0  # one chart point per second (~3 minutes shown)
LOG_LINES = 300
ROTATE_SECONDS = 900         # auto-rotate re-centres the strikes this often (strategy: rolling ladder)
ROLLED_OFF = "rolled off — working position down"
NOTHING_TO_TRADE = ("Nothing to trade right now: today's BTC markets are in their last 2 hours and "
                    "tomorrow's open at 4pm ET (3pm CT). With Auto-rotate on, the bot picks them up "
                    "automatically within a minute.")
FETCH_WORKERS = 12

# Repricing: woken by every coin tick / book change, at most every
# REPRICE_MIN_INTERVAL, and at least every REPRICE_HEARTBEAT even if quiet.
REPRICE_MIN_INTERVAL_SECONDS = 0.05
REPRICE_HEARTBEAT_SECONDS = 1.0
# REST polling when there's no Kalshi websocket (no API key) — ~8 req/s total:
BOOK_POLL_SECONDS = 0.4      # 1 batched request: every market's best bid/ask
TRADES_POLL_SECONDS = 2.0    # 1 request per market (paper fills; timing-exact via quote history)
DEPTH_POLL_SECONDS = 10.0    # 1 request per market, only for the dashboard's depth ladder
STREAM_CHECK_SECONDS = 0.5   # with the websocket: how often to drain its trade buffer / check health
# A market whose Kalshi book is older than this is not quoted or traded
# (quotes are pulled) — e.g. while REST polling is failing or after sleep.
MAX_BOOK_AGE_SECONDS = 3.0
# Windows put the PC to sleep while the bot traded (twice on 2026-10-25/26).
# Trading paused, and on wake the paper quotes left from before the sleep
# were filled against trades made while it slept. Live orders would really
# sit on Kalshi at stale prices. So the engine thread asks Windows to keep
# the PC awake while trading runs. The screen can still turn off. Closing
# the lid or picking Sleep still sleeps the PC.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _keep_awake(on: bool):
    """Hold (or release) a keep-awake request for the calling thread."""
    if sys.platform != "win32":
        return
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
        log.warning("Couldn't ask Windows to %s", "keep the PC awake" if on else "allow sleep again")


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
        self._kbooks: dict[str, dict] = {}            # ticker -> latest REST-polled book
        self._pending_trades: dict[str, list] = {}    # ticker -> trades not yet matched to quotes
        self._take_version: dict[str, object] = {}    # ticker -> book version last taken against
        self._pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch")
        self._data_thread = None
        self._wake = threading.Event()
        self.reprice_latency_ms = None
        self.reprice_ms = None
        self.kalshi_stream = None
        # The streaming index runs even while the bot is stopped, so the
        # dashboard always shows live prices.
        feeds.HUB.listeners.append(self._wake)
        feeds.HUB.start()
        # On by default: the strategy depends on staying near the money.
        self.auto_rotate = True
        self._last_rotate = 0.0
        self.last_autopick = None
        self.logs = collections.deque(maxlen=LOG_LINES)
        logging.getLogger().addHandler(_RingLogHandler(self.logs))
        # Positions live in the ledger, not in memory. On 2026-10-21 a restart
        # started the bot from flat: it traded 2x past its limits, its exit
        # quotes worked the wrong positions, and a 330-contract book it no
        # longer knew about rode to settlement for −$87. Restored markets are
        # watched so they can be worked down; the first auto-pick keeps them
        # reduce-only if they're off the ladder.
        restored = self.risk.restore(ledger.read_all_fills(), "paper" if config.kalshi.paper_trading else "live",
                                     lambda t: pnl.check_settlement(self.client, t)[0])
        for t in restored:
            if t not in self.cfg.watched_tickers:
                self.cfg.watched_tickers.append(t)
        if restored:
            log.info("Restored open positions from the ledger: %s", restored)
        if HALT_FILE.exists():
            self.risk.halted = True
            self.risk.halt_reason = f"{HALT_FILE.read_text().strip()} (still halted after a restart)"
            log.warning("Bot is halted: %s — reset the kill switch to trade", self.risk.halt_reason)

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
            if not self.cfg.watched_tickers and not self.auto_rotate:
                return False, "no watched tickers — add one first"
            self.running = True
            self.started_at = time.time()
            self._stop.clear()
            # Start from fresh data only — nothing cached from a previous run.
            self._kbooks.clear()
            self._pending_trades.clear()
            self.books.clear()
            if self.client.auth is not None and self.kalshi_stream is None:
                # Kalshi's websocket needs an API key; without one the data
                # loop polls REST instead.
                self.kalshi_stream = feeds.KalshiStream(self.client.auth, self.cfg.kalshi.ws_url,
                                                        self.cfg.watched_tickers)
                self.kalshi_stream.changed = self._wake
                self.kalshi_stream.start()
            self._data_thread = threading.Thread(target=self._data_loop, name="engine-data", daemon=True)
            self._data_thread.start()
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
        self._wake.set()
        for th in (self._thread, self._data_thread):
            if th and th is not threading.current_thread():
                th.join(timeout=15)
        with self.lock:
            self.maker.cancel_all(reason)
            self.running = False
        log.info("Engine stopped (%s)", reason)
        return True, "stopped"

    def _save_halt(self):
        HALT_FILE.write_text(f"{self.risk.halt_reason} ({datetime.now():%Y-%m-%d %H:%M})\n")

    def kill(self) -> tuple[bool, str]:
        """Manual kill switch: halt the risk manager and pull every quote."""
        self.risk._halt("manual kill switch")
        self._save_halt()
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
            HALT_FILE.unlink(missing_ok=True)
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
        res = autopick.pick(self.client)
        picks = [p["ticker"] for p in res["picks"]]
        with self.lock:
            current = list(self.cfg.watched_tickers)
        gap = res["notes"] and all("no event closing" in n for n in res["notes"])
        idle_msg = NOTHING_TO_TRADE if gap else "No suitable markets right now: " + "; ".join(res["notes"])
        if not picks and not current:
            return False, idle_msg
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
        msg = f"watching {len(picks)} picks (+{len(added)} / −{len(removed)})" if picks else idle_msg
        if kept:
            msg += f", kept {len(kept)} with open positions"
        if picks and res["notes"]:
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
        of close (where the model stops trading), re-run autopick. Markets
        it kept only to work a position down don't count: they stay that
        close until flat, and re-picking for them on every loop (twice a
        second) flooded the log and the API on 2026-10-27."""
        if not self.auto_rotate:
            return
        kept = set((self.last_autopick or {}).get("kept", []))
        picked = [t for t in list(self.cfg.watched_tickers) if t not in kept]
        closing = any(
            (info := self.market_info.get(t, {})).get("close_time")
            and (datetime.fromisoformat(info["close_time"].replace("Z", "+00:00"))
                 - datetime.now(timezone.utc)).total_seconds() < 2 * 3600
            for t in picked)
        idle = not picked          # e.g. waiting for the next daily to open
        if closing or time.time() - self._last_rotate > (60 if idle else ROTATE_SECONDS):
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

    # Two threads while running:
    #   _data_loop   keeps Kalshi books + trade tapes current — from the
    #                Kalshi websocket when an API key is set, otherwise by
    #                REST polling (one batched request for every market's
    #                best bid/ask, trade tapes in parallel, depth now and then)
    #   _loop        sleeps until something changes — a coin tick from the
    #                streaming index (feeds.HUB) or a Kalshi book update —
    #                then fills, reprices and re-quotes every market at once.
    # Repricing is local work (milliseconds), so quotes follow the coin as
    # fast as the exchanges stream it.

    def _loop(self):
        # The request belongs to this thread, so it lasts exactly as long as
        # trading does: stop, kill switch and halts all end here.
        _keep_awake(True)
        try:
            while not self._stop.is_set():
                if self.risk.halted:
                    log.critical("Bot is halted (%s). Cancelling all resting orders.",
                                 self.risk.halt_reason)
                    with self.lock:
                        self.maker.cancel_all("kill switch")
                        self.running = False
                        if not HALT_FILE.exists():
                            self._save_halt()
                    return
                self._wake.wait(REPRICE_HEARTBEAT_SECONDS)
                self._wake.clear()
                self.run_cycle()
                self._stop.wait(REPRICE_MIN_INTERVAL_SECONDS)
        finally:
            _keep_awake(False)

    def _data_loop(self):
        last_trades = last_depth = 0.0
        while not self._stop.is_set():
            t0 = time.time()
            self._maybe_rotate()
            with self.lock:
                tickers = list(self.cfg.watched_tickers)
            stream = self.kalshi_stream
            if stream:
                stream.set_tickers(tickers)
            streaming = bool(stream and stream.healthy)
            try:
                if not streaming or any(t not in self.market_info for t in tickers):
                    self._poll_books(tickers, depth=time.time() - last_depth >= DEPTH_POLL_SECONDS)
                    if time.time() - last_depth >= DEPTH_POLL_SECONDS:
                        last_depth = time.time()
                if self.cfg.kalshi.paper_trading and time.time() - last_trades >= TRADES_POLL_SECONDS:
                    if streaming:
                        for t in tickers:
                            self._queue_trades(t, stream.drain_trades(t))
                    else:
                        futs = {t: self._pool.submit(self.client.get_trades, t, min_ts=self.started_at - 5)
                                for t in tickers}
                        for t, f in futs.items():
                            try:
                                self._queue_trades(t, f.result())
                            except Exception as e:
                                log.warning("Trade tape for %s failed: %s", t, e)
                    last_trades = time.time()
            except Exception as e:
                self.last_error = f"data: {e}"
                log.exception("Market data poll failed: %s", e)
                self.risk.record_api_error(self.client.consecutive_errors)
            self._stop.wait(max(0.0, (STREAM_CHECK_SECONDS if streaming else BOOK_POLL_SECONDS)
                                - (time.time() - t0)))

    def _poll_books(self, tickers, depth: bool):
        """One request for every market's best bid/ask (+ market info);
        full orderbooks for the depth ladder when `depth`."""
        rows = {m["ticker"]: m for m in self.client.get_markets_batch(tickers)}
        fetched_at = time.time()
        futs = {t: self._pool.submit(self.client.get_orderbook, t) for t in tickers} if depth else {}
        changed = False
        with self.lock:
            for t, m in rows.items():
                self.market_info[t] = m
                book = top_of_book(m)
                book["ts"] = fetched_at
                book["raw_yes_levels"], book["raw_no_levels"] = self._depth.get(t, ([], []))
                old = self._kbooks.get(t)
                if not old or (old["yes_bid"], old["yes_ask"]) != (book["yes_bid"], book["yes_ask"]):
                    changed = True
                self._kbooks[t] = book
        if changed:
            self._wake.set()
        # Depth is only for the dashboard's ladder, so the books above are
        # published first and the lock isn't held while it downloads: it
        # used to block repricing for ~0.6s every DEPTH_POLL_SECONDS.
        for t, f in futs.items():
            try:
                full = normalize_orderbook(f.result())
            except Exception as e:
                log.warning("Depth for %s failed: %s", t, e)
                continue
            with self.lock:
                self._depth[t] = (full["raw_yes_levels"], full["raw_no_levels"])
                if (book := self._kbooks.get(t)) is not None:
                    book["raw_yes_levels"], book["raw_no_levels"] = self._depth[t]

    def _queue_trades(self, ticker, rows):
        if rows:
            with self.lock:
                self._pending_trades.setdefault(ticker, []).extend(rows)
            self._wake.set()

    def _current_book(self, ticker):
        stream = self.kalshi_stream
        if stream and stream.healthy and (b := stream.book(ticker)):
            return b
        return self._kbooks.get(ticker)

    def run_cycle(self):
        """Fill, then reprice every watched market from the latest data. No
        network calls except the model's (cached, refreshed in background)."""
        started = time.time()
        tick_ts = feeds.HUB.last_update
        with self.lock:
            tickers = list(self.cfg.watched_tickers)
            pending, self._pending_trades = self._pending_trades, {}
        marks = {}
        for ticker in tickers:
            try:
                book = self._current_book(ticker)
                if book is None or ticker not in self.market_info:
                    continue                       # no data yet
                age = started - book.get("ts", 0)
                if age > MAX_BOOK_AGE_SECONDS:
                    # Never trade a stale book against a live coin price: on
                    # 2026-10-21 a restart priced 16h-old books with the live
                    # index and "bought" 540 contracts at prices long gone.
                    with self.lock:
                        if self.maker.resting_quotes.get(ticker):
                            log.warning("Book for %s is %.0fs old — pulling quotes until fresh data", ticker, age)
                            self.maker._cancel_resting(ticker, reason="stale book")
                        self.maker.last_plan[ticker] = {**self.maker.last_plan.get(ticker, {}),
                                                        "mode": "idle", "reason": f"book {age:.0f}s old — waiting for fresh data"}
                    continue
                if pending.get(ticker):
                    # Fill first, against the quotes that were resting while
                    # these trades happened; then reprice with the new position.
                    with self.lock:
                        self.maker.process_trades(ticker, pending[ticker])
                # One take per market per book update: the bot reprices many
                # times a second but a REST book refreshes every 0.4s, and a
                # live take only shows up in the next one. (Paper takes never
                # show up; MarketMaker takes them off the touch size itself.)
                version = book.get("version", book.get("ts"))
                fresh_for_take = self._take_version.get(ticker) != version
                with self.lock:
                    self.maker.evaluate_market(ticker, self.market_info[ticker], book, allow_takes=fresh_for_take)
                    if self.maker.last_plan.get(ticker, {}).get("takes"):
                        self._take_version[ticker] = version
                self._record_book(ticker, book)
                if book["yes_bid"] is not None and book["yes_ask"] is not None:
                    marks[ticker] = (book["yes_bid"] + book["yes_ask"]) / 2
                self.last_error = None
            except Exception as e:
                self.last_error = f"{ticker}: {e}"
                log.exception("Error evaluating %s: %s", ticker, e)
        with self.lock:
            self.risk.update_marks(marks)
        now = time.time()
        work_ms = (now - started) * 1000
        self.reprice_ms = work_ms if self.reprice_ms is None else 0.9 * self.reprice_ms + 0.1 * work_ms
        # Tick -> requote, only for cycles a new tick actually triggered.
        if tick_ts and tick_ts > (self.last_cycle_at or 0):
            ms = (now - tick_ts) * 1000
            self.reprice_latency_ms = ms if self.reprice_latency_ms is None else 0.9 * self.reprice_latency_ms + 0.1 * ms
        self.cycles += 1
        self.last_cycle_at = now

    def _record_book(self, ticker, book):
        bid, ask = book["yes_bid"], book["yes_ask"]
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        # Top of book for the depth ladder: YES bids as-is, NO bids flipped
        # into YES asks (a NO bid at P is a YES ask at 100 - P).
        bids = sorted(book["raw_yes_levels"], key=lambda l: -l[0])[:6]
        asks = sorted(((100 - p, s) for p, s in book["raw_no_levels"]), key=lambda l: l[0])[:6]
        now = time.time()
        with self.lock:
            self.books[ticker] = {
                "yes_bid": bid, "yes_ask": ask, "spread": book["spread_cents"], "mid": mid,
                "bids": bids, "asks": asks, "updated": now,
            }
            hist = self.history.setdefault(ticker, collections.deque(maxlen=PRICE_HISTORY_POINTS))
            if hist and now - hist[-1]["ts"] < HISTORY_EVERY_SECONDS:
                return                    # reprices run many times a second; chart one point/s
            fv = self.maker.last_plan.get(ticker, {}).get("fv")
            hist.append({"ts": now, "bid": bid, "ask": ask, "mid": mid, "fv": fv})

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
                    "event": info.get("event_ticker"),
                    "strike": info.get("floor_strike"),
                    "strike_type": info.get("strike_type"),
                    "open_time": info.get("open_time"),
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
                "feeds": {**feeds.HUB.snapshot(),
                          "kalshi_mode": ("websocket" if self.kalshi_stream and self.kalshi_stream.healthy
                                          else "REST polling" + ("" if self.client.auth else " (websocket needs an API key)")),
                          "reprice_latency_ms": round(self.reprice_latency_ms, 1) if self.reprice_latency_ms else None,
                          "reprice_work_ms": round(self.reprice_ms, 1) if self.reprice_ms else None,
                          "fill_stats": self.maker.fill_stats},
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
                    "min_reduce_edge_cents": r.min_reduce_edge_cents,
                    "max_take_book_delta_usd": r.max_take_book_delta_usd,
                    "model_stop_hours": CRYPTO_MIN_HOURS_TO_TRADE,
                    "pick_window_hours": autopick.MAX_HOURS,
                },
                "markets": markets,
                "logs": list(self.logs)[-120:],
            }
