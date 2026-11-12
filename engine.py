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
import json
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
import price_bars
from config import CONFIG, BotConfig
from fair_value import CRYPTO_MIN_HOURS_TO_TRADE, CRYPTO_SERIES
from kalshi_client import build_client, normalize_orderbook, top_of_book
from market_maker import MarketMaker
from risk_manager import RiskManager

log = logging.getLogger("engine")

# A halt (kill switch or loss limit) is written here and survives restarts:
# with run-bot.ps1 restarting the dashboard and --autostart, a crash or a
# reboot would otherwise quietly resume trading. Reset on the dashboard.
HALT_FILE = Path(__file__).parent / "halted.txt"
# Live: where today's daily P&L started, so the daily loss limit doesn't forget
# the day's losses on a restart. The server restarted four times on 2026-10-28.
RISK_DAY_FILE = Path(__file__).parent / "risk_day.json"
PRICE_HISTORY_POINTS = 180   # per ticker
HISTORY_EVERY_SECONDS = 1.0  # one chart point per second (~3 minutes shown)
LOG_LINES = 300
ROTATE_SECONDS = 900         # auto-rotate re-centres the strikes this often (strategy: rolling ladder)
SETTLE_CHECK_SECONDS = 60.0  # how often positions past their close are checked for settlement
ROLLED_OFF = "rolled off — working position down"
NOTHING_TO_TRADE = (f"Nothing to trade right now: today's BTC markets are in their last "
                    f"{CRYPTO_MIN_HOURS_TO_TRADE:g} hours and tomorrow's open at 4pm ET (3pm CT). With "
                    f"Auto-rotate on, the bot picks them up automatically within a minute.")
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
# Live only:
LIVE_TICK_SECONDS = 1.0        # how often the live housekeeping thread looks for work
BALANCE_SECONDS = 5.0          # Kalshi allows Basic accounts one balance read a second
RECONCILE_SECONDS = 15.0       # compare positions and resting orders with Kalshi's
FILLS_OVERLAP_SECONDS = 300    # ask for fills this far before the last one we know of; repeats are ignored
# Kalshi's order list lags: on 2026-11-04 an order placed a moment earlier wasn't
# on it, and one just cancelled still was, for under 2 seconds. A quote younger
# than this isn't judged by its absence from the list.
ORDER_LIST_LAG_SECONDS = 5.0
FILL_STREAM_DOWN = "Can't see fills from Kalshi, so not trading until the fill stream is back"
STATUS_LAG_SECONDS = 60     # how long an "open" status is doubted after an order was refused for a pause
EXCHANGE_CLOSED = "Kalshi's exchange is closed, so not trading until it reopens"
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
        # Settlement checks are background work: they only take rate-limit
        # slots trading leaves free, and their errors don't count toward the
        # kill switch.
        self.settle_client = build_client(config)
        self.settle_client.low_priority = True
        self._last_settle_check = 0.0
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
        self.bars = price_bars.BarStore()    # the model's price, for long charts
        self.bars.start()
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
        self.user_stream = None
        self._live_thread = None
        self.live_account = None           # latest balance from Kalshi
        self.exchange_open = True          # Kalshi's trading status, as last read
        self.maker.exchange_is_closed = self._exchange_is_closed
        self.maker.trading_paused = self._orders_say_paused
        self._paused_by_order_at = 0.0     # when an order was last refused because trading is paused
        self._last_balance = self._last_reconcile = 0.0
        self._fills_from = time.time() - FILLS_OVERLAP_SECONDS
        self._mismatch_seen = None
        self._reconcile_failures = 0
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
        if config.kalshi.paper_trading:
            restored = self.risk.restore(ledger.read_all_fills(), "paper",
                                         lambda t: pnl.check_settlement(self.client, t)[0])
        else:
            restored = self._restore_live()
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
            if self.mode == "LIVE":
                ok, why = self._prepare_live()
                if not ok:
                    log.critical("Not starting: %s", why)
                    return False, why
            self.running = True
            self.started_at = time.time()
            self._stop.clear()
            # Start from fresh data only — nothing cached from a previous run.
            self._kbooks.clear()
            self._pending_trades.clear()
            self.books.clear()
            self._start_market_stream()
            self._data_thread = threading.Thread(target=self._data_loop, name="engine-data", daemon=True)
            self._data_thread.start()
            self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
            self._thread.start()
            if self.mode == "LIVE":
                self._live_thread = threading.Thread(target=self._live_loop, name="engine-live", daemon=True)
                self._live_thread.start()
        log.info("Engine started in %s mode. Watching: %s", self.mode, self.cfg.watched_tickers)
        if self.mode == "LIVE":
            log.warning("LIVE MODE — real orders will be sent.")
        return True, "started"

    def _start_market_stream(self):
        if self.client.auth is not None and self.kalshi_stream is None:
            # Kalshi's websocket needs an API key; without one the data loop
            # polls REST instead.
            self.kalshi_stream = feeds.KalshiStream(self.client.auth, self.cfg.kalshi.ws_url, self.cfg.watched_tickers,
                                                    keep_trades=self.cfg.kalshi.paper_trading)
            self.kalshi_stream.changed = self._wake
            self.kalshi_stream.start()

    def _cancel_orphaned_orders(self) -> tuple[bool, str]:
        """Cancel orders a previous run left resting on Kalshi. Quotes are
        tracked in memory only, so after a crash or restart nothing would
        reprice or cancel them: on 2026-10-26 quotes left resting while the
        PC slept 4h filled at stale prices for -$115 (paper), and the server
        was restarted four times by the OOM killer on 2026-10-28. Only orders
        on the coin series the bot trades, or on watched markets."""
        try:
            orders = self.client.get_orders(status="resting").get("orders", [])
        except Exception as e:
            log.error("Couldn't list resting orders before starting: %s", e)
            return False, f"couldn't check for orders left by a previous run: {e}"
        failed = ""
        for o in orders:
            ticker = o.get("ticker", "")
            if not self._is_ours(ticker):
                continue
            try:
                self.client.cancel_order(o["order_id"], ticker)
                log.warning("Cancelled order %s on %s left resting by a previous run", o["order_id"], ticker)
            except Exception as e:
                log.error("Failed to cancel order %s on %s: %s", o.get("order_id"), ticker, e)
                failed = f"couldn't cancel order {o.get('order_id')} left by a previous run: {e}"
        return not failed, failed

    def _is_ours(self, ticker: str) -> bool:
        return ticker in self.cfg.watched_tickers or ticker.split("-")[0] in CRYPTO_SERIES

    # ---- live: what's really on Kalshi --------------------------------------

    def _restore_live(self) -> dict:
        """Live positions from the ledger's live rows. Starting checks them
        against Kalshi (_prepare_live) before any order goes out."""
        fills = [f for f in ledger.read_all_fills() if f.get("mode") == "live"]
        restored = self.risk.restore_live(fills, lambda t: pnl.check_settlement(self.client, t))
        self.maker.resume_live(fills)
        if fills:
            self._fills_from = max(f["ts"] for f in fills) - FILLS_OVERLAP_SECONDS
        self._price_open_positions(restored)
        self.risk.resume_day(self._read_risk_day())
        self.risk.on_new_day = self._save_risk_day
        self._save_risk_day()
        return restored

    def _price_open_positions(self, restored):
        """Without a price until the first book arrives, an open position counts
        as worth what it cost, which can read as a loss against the day's start."""
        try:
            for m in self.client.get_markets_batch(list(restored)):
                book = top_of_book(m)
                if book["yes_bid"] is not None or book["yes_ask"] is not None:
                    ask = book["yes_ask"] if book["yes_ask"] is not None else 100
                    self.risk.marks[m["ticker"]] = ((book["yes_bid"] or 0) + ask) / 2
        except Exception as e:
            log.warning("Couldn't price the restored positions: %s", e)

    def _read_risk_day(self):
        try:
            return json.loads(RISK_DAY_FILE.read_text())
        except (OSError, ValueError):
            return None

    def _save_risk_day(self):
        RISK_DAY_FILE.write_text(json.dumps(self.risk.day_state()))

    def _prepare_live(self) -> tuple[bool, str]:
        """Everything that must be true before the first real order: nothing
        left resting by a previous run, every fill booked, positions matching
        Kalshi's, and the balance known. Any doubt means not starting: on
        2026-10-21 a restart that got its positions wrong traded 2x past its
        limits for -$87."""
        ok, why = self._cancel_orphaned_orders()
        if not ok:
            return False, why
        try:
            self._apply_fills(self.client.get_fills(min_ts=self._fills_from))
            positions = self.client.get_positions()
            mine = {t for t, p in self.risk.positions.items() if p.net_contracts}
            ask = sorted(t for t in mine | set(positions) | set(self.cfg.watched_tickers) if self._is_ours(t))
            for m in self.client.get_markets_batch(ask):
                self.market_info[m["ticker"]] = m
        except Exception as e:
            return False, f"couldn't read positions from Kalshi: {e}"
        wrong = self._position_mismatches(positions)
        if wrong:
            return False, ("positions don't match Kalshi's: " + self._describe_mismatches(wrong)
                           + ". Nothing was traded. Sort out the difference on Kalshi, or in fills_log.jsonl, then start again.")
        self._refresh_exchange_status()
        if not self._refresh_balance():
            return False, "couldn't read the balance from Kalshi"
        has_positions = any(p.net_contracts for p in self.risk.positions.values())
        if self.maker.available_cents is not None and self.maker.available_cents <= 0 and not has_positions:
            where = " or ".join(str(s) for s in sorted(self._watched_exchanges()))
            return False, (f"there's no money on exchange {where}, where these markets trade. Kalshi's app moves money "
                           "between its exchanges for you, but API orders don't. Move some at "
                           "https://kalshi.com/account/exchange-indexes, then start again.")
        now = time.time()
        self._last_balance = self._last_reconcile = now
        self._mismatch_seen, self._reconcile_failures = None, 0
        if self.user_stream is None:
            self.user_stream = feeds.UserStream(self.client.auth, self.cfg.kalshi.fills_ws_url or self.cfg.kalshi.ws_url)
            self.user_stream.changed = self._wake
            self.user_stream.start()
        return True, ""

    def _live_loop(self):
        while not self._stop.is_set():
            try:
                self._live_tick()
            except Exception as e:
                log.exception("Live housekeeping failed: %s", e)
            self._stop.wait(LIVE_TICK_SECONDS)

    def _live_tick(self):
        stream = self.user_stream
        if stream and stream.subscribed and not stream.caught_up:
            try:
                self._apply_fills(self.client.get_fills(min_ts=self._fills_from))
                stream.caught_up = True
                log.info("Fill stream up and caught up")
            except Exception as e:
                log.error("Couldn't catch up on fills: %s", e)
        now = time.time()
        if now - self._last_balance >= BALANCE_SECONDS:
            self._last_balance = now
            self._refresh_exchange_status()
            self._refresh_balance()
        if now - self._last_reconcile >= RECONCILE_SECONDS and self.exchange_open:
            self._last_reconcile = now
            self._reconcile()

    def _apply_fills(self, fills):
        fills = [f for f in fills if self._is_ours(f["ticker"])]
        if fills:
            with self.lock:
                self.maker.process_live_fills(fills)
                self._fills_from = max(self._fills_from, max(f["ts"] for f in fills) - FILLS_OVERLAP_SECONDS)

    def _refresh_exchange_status(self):
        """Kalshi closes Thursdays 3-5am ET and can pause trading at any time. Is
        it trading on these markets' exchange? If it can't tell, what it last
        read stands."""
        try:
            status = self.client.get_exchange_status()
        except Exception as e:
            log.warning("Couldn't read Kalshi's trading status: %s", e)
            return
        trading = {r["exchange_index"]: r.get("trading_active") for r in status.get("exchange_index_statuses", [])}
        is_open = bool(status.get("trading_active")) and all(trading.get(x, True) for x in self._watched_exchanges())
        if is_open and time.time() - self._paused_by_order_at < STATUS_LAG_SECONDS:
            return          # an order was just refused for a pause this status doesn't show yet
        if is_open != self.exchange_open:
            log.warning("Kalshi's exchange %s", "reopened" if is_open else "closed: pulling quotes until it reopens")
        self.exchange_open = is_open

    def _orders_say_paused(self):
        """Kalshi refused an order because trading is paused. Its status said so
        4 seconds later on 2026-11-12, by which time 5 refused orders had halted
        the bot, so the order's answer counts as closed until the status catches up."""
        self._paused_by_order_at = time.time()
        if self.exchange_open:
            log.warning("Kalshi's exchange closed (it refused an order): pulling quotes until it reopens")
        self.exchange_open = False

    def _exchange_is_closed(self) -> bool:
        """For when orders keep failing. It may have closed since the last read."""
        self._refresh_exchange_status()
        return not self.exchange_open

    def _watched_exchanges(self) -> set:
        return {self.market_info[t].get("exchange_index") for t in self.cfg.watched_tickers
                if t in self.market_info} - {None}

    def _spendable_cents(self, balance: dict):
        """What the bot can spend. Kalshi checks funds on each market's own
        exchange and API orders don't move money between them, while "balance"
        is the total across all of them: on 2026-11-04 it read $49.24 with $0 on
        the exchange BTC trades on, and every order came back 404. Coin markets
        share one exchange; across several, the smallest amount is the cautious
        number. None = not known yet, so no orders. Keys restricted to a
        subaccount get no breakdown, only the total."""
        rows = balance.get("balance_breakdown")
        if rows is None:
            return balance["balance"]
        exchanges = self._watched_exchanges()
        if not exchanges:
            return None
        on_exchange = {r["exchange_index"]: round(float(r["balance"]) * 100) for r in rows}
        return min(on_exchange.get(x, 0) for x in exchanges)

    def _refresh_balance(self) -> bool:
        try:
            b = self.client.get_balance()
        except Exception as e:
            log.warning("Couldn't read the balance from Kalshi: %s", e)
            return False
        with self.lock:
            spendable = self._spendable_cents(b)
            self.maker.available_cents = spendable
            self.live_account = {"balance_usd": b["balance"] / 100, "portfolio_value_usd": b.get("portfolio_value", 0) / 100,
                                 "spendable_usd": None if spendable is None else spendable / 100, "updated": time.time()}
        return True

    def _position_mismatches(self, positions: dict) -> dict:
        """{ticker: (the bot's net, Kalshi's net)} where they differ. Other kinds
        of market aren't the bot's, and a market past its close is skipped: the
        two don't clear it at the same instant."""
        with self.lock:
            tickers = set(positions) | {t for t, p in self.risk.positions.items() if p.net_contracts}
            wrong = {}
            for t in tickers:
                if not self._is_ours(t) or (t in self.market_info and self._past_close(t)):
                    continue
                ours, theirs = self.risk.net(t), positions.get(t, 0)
                if abs(ours - theirs) > 0.005:
                    wrong[t] = (ours, theirs)
        return wrong

    @staticmethod
    def _describe_mismatches(wrong) -> str:
        return "; ".join(f"{t}: the bot has {ours}, Kalshi has {theirs}" for t, (ours, theirs) in sorted(wrong.items()))

    def _reconcile(self):
        """Compare the bot with Kalshi. A fill the stream missed gets booked
        here. A position that still differs on two checks in a row can't be
        timing, so the bot halts rather than trade on a wrong position. If the
        check can't run at all for a minute or so, the same."""
        listed_at = time.time()
        try:
            orders = self.client.get_orders(status="resting")["orders"]
            self._apply_fills(self.client.get_fills(min_ts=max(self._fills_from, listed_at - 600)))
            positions = self.client.get_positions()
        except Exception as e:
            self._reconcile_failures += 1
            log.error("Couldn't check positions on Kalshi (%d in a row): %s", self._reconcile_failures, e)
            if self._reconcile_failures >= self.cfg.risk.max_consecutive_errors:
                self.risk._halt(f"couldn't check positions on Kalshi {self._reconcile_failures} times in a row: {e}")
            return
        self._reconcile_failures = 0
        wrong = self._position_mismatches(positions)
        if wrong and wrong == self._mismatch_seen:
            self.risk._halt("positions don't match Kalshi's: " + self._describe_mismatches(wrong))
        elif wrong:
            log.warning("Positions differ from Kalshi's, checking again: %s", self._describe_mismatches(wrong))
        self._mismatch_seen = wrong or None
        self._clean_up_orders(orders, listed_at)

    def _clean_up_orders(self, orders, listed_at):
        """Kalshi's resting orders against the bot's: cancel any it isn't tracking
        (e.g. the answer was lost, so nothing would ever replace it), and stop
        tracking any Kalshi no longer has (expired or cancelled by Kalshi), so
        it quotes again. The list lags, so a new quote may not be on it yet, and
        cancelling one it still shows just gets "already gone"."""
        on_kalshi = {o["order_id"] for o in orders}
        with self.lock:
            tracked = {q.order_id for quotes in self.maker.resting_quotes.values() for q in quotes}
            for o in orders:
                if o["order_id"] not in tracked and self._is_ours(o.get("ticker", "")):
                    log.warning("Cancelling order %s on %s: the bot isn't tracking it", o["order_id"], o["ticker"])
                    try:
                        self.client.cancel_order(o["order_id"], o["ticker"])
                    except Exception as e:
                        log.error("Failed to cancel order %s: %s", o["order_id"], e)
            for quotes in self.maker.resting_quotes.values():
                quotes[:] = [q for q in quotes if q.order_id in on_kalshi
                             or q.placed_at >= listed_at - ORDER_LIST_LAG_SECONDS]

    def stop(self, reason: str = "stopped by user") -> tuple[bool, str]:
        if not self.running:
            return False, "not running"
        self._stop.set()
        self._wake.set()
        for th in (self._thread, self._data_thread, self._live_thread):
            if th and th is not threading.current_thread():
                th.join(timeout=15)
        with self.lock:
            self._pull_all_orders(reason)
            self.running = False
        log.info("Engine stopped (%s)", reason)
        return True, "stopped"

    def _pull_all_orders(self, reason):
        """Every quote the bot tracks, and live, anything else of ours on Kalshi."""
        self.maker.cancel_all(reason)
        if self.mode == "LIVE":
            ok, why = self._cancel_orphaned_orders()
            if not ok:
                log.critical("Orders may still be resting on Kalshi: %s", why)

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
                self._pull_all_orders("kill switch")
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
        if self.mode == "LIVE" and not self._is_ours(ticker):
            return False, f"{ticker} isn't a coin market, and live trading is coin markets only"
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

    def _past_close(self, ticker) -> bool:
        close = self.market_info.get(ticker, {}).get("close_time")
        return not close or datetime.fromisoformat(close.replace("Z", "+00:00")) <= datetime.now(timezone.utc)

    def _maybe_clear_settled(self):
        """Every SETTLE_CHECK_SECONDS, close out positions in markets that have
        settled. restore() does this too, but only at startup: on 2026-11-02 a
        bot up 119h still held four Oct 30 markets. They stayed on the watchlist
        reduce-only for good, as $48 of exposure against nothing, with the
        dashboard showing their event instead of the one being traded. A strike
        at the money just before close would also leave a delta of 1.0 per
        contract in the lean and the take cap. Only positions past their close
        are asked about, on the pool so a slow answer can't hold up this loop."""
        if time.time() - self._last_settle_check < SETTLE_CHECK_SECONDS:
            return
        self._last_settle_check = time.time()
        with self.lock:
            closed = [t for t, pos in self.risk.positions.items() if pos.net_contracts and self._past_close(t)]
        if closed:
            self._pool.submit(self._clear_settled, closed)

    def _clear_settled(self, tickers):
        try:
            # One request per market until it's finalized, then none: cached.
            checks = {t: pnl.check_settlement(self.settle_client, t) for t in tickers}
            with self.lock:
                for t, (final, price, result) in checks.items():
                    if final and self.risk.settle(t, price, result):
                        self.remove_ticker(t)
        except Exception as e:
            log.exception("Clearing settled positions failed: %s", e)

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
                        self._pull_all_orders("kill switch")
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
            self._maybe_clear_settled()
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
        live = self.mode == "LIVE"
        if live and not self._ready_to_trade():
            tickers = []
        marks = {}
        for ticker in tickers:
            try:
                book = self._current_book(ticker)
                if book is None or ticker not in self.market_info:
                    continue                       # no data yet
                if live and self._past_close(ticker):
                    # Kalshi would reject every order, and enough of those halt the bot.
                    with self.lock:
                        self.maker._cancel_resting(ticker, reason="market closed")
                    continue
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
                    # Removed since this cycle listed it (auto-pick, or it
                    # settled): evaluating it would bring back its delta and
                    # rest quotes nothing would ever pull.
                    if ticker not in self.cfg.watched_tickers:
                        continue
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

    def _ready_to_trade(self) -> bool:
        """Book what the fill stream has delivered, then pull every quote and
        don't trade if Kalshi is closed, or if the stream isn't healthy: a bot
        that can't see its fills can't enforce a single limit. Trading picks up
        by itself once the problem clears."""
        stream = self.user_stream
        if stream:
            self._apply_fills(stream.drain_fills())
        if not self.exchange_open:
            problem = EXCHANGE_CLOSED
        elif not (stream and stream.healthy):
            problem = FILL_STREAM_DOWN
        else:
            if self.last_error in (FILL_STREAM_DOWN, EXCHANGE_CLOSED):
                self.last_error = None
            return True
        with self.lock:
            self.maker.cancel_all(problem)
        self.last_error = problem
        return False

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
            self.bars.add(ticker, now, fv)

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
                "live": None if self.mode != "LIVE" else {
                    "account": self.live_account,
                    "fill_stream_ok": bool(self.user_stream and self.user_stream.healthy),
                    "exchange_open": self.exchange_open,
                    "orders_failed_in_a_row": self.maker.orders_failed_in_a_row,
                    "positions_differ": self._describe_mismatches(self._mismatch_seen) if self._mismatch_seen else None,
                },
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
                    "max_take_hours": r.max_take_hours,
                    "model_stop_hours": CRYPTO_MIN_HOURS_TO_TRADE,
                    "pick_window_hours": autopick.MAX_HOURS,
                },
                "markets": markets,
                "logs": list(self.logs)[-120:],
            }
