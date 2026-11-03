"""
A market that settles while the bot runs must stop counting as an open
position, and clearing it must not change what the bot thinks it made.

    python -m unittest test_settlement

Only Kalshi and the price feeds are faked. The ledger, P&L, risk manager,
market maker and engine are the real ones.
"""
import logging
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import engine
import ledger
import pnl
from config import BotConfig, KalshiConfig, RiskConfig
from fair_value import FairValue
from risk_manager import RiskManager

OLD = "KXBTCD-26OCT3017-T78499.99"     # the event that has settled
OLD2 = "KXBTCD-26OCT3017-T78999.99"
OLD3 = "KXBTCD-26OCT3017-T77999.99"
OLD4 = "KXBTCD-26OCT3017-T79499.99"
NEW = "KXBTCD-26NOV0317-T76999.99"     # the event being traded


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class RiskManagerSettleTests(unittest.TestCase):
    def short_45_at_60(self, **limits):
        """Sold 45 YES at 60¢ as a taker (76¢ fee), last mid 5¢: +$23.99."""
        risk = RiskManager(RiskConfig(**limits))
        risk.record_fill(OLD, "yes", "sell", 45, 60, maker=False)
        risk.update_marks({OLD: 5})
        return risk

    def long_75_at_60(self, **limits):
        """Bought 75 YES at 60¢ as a maker (no fee), last mid 50¢: −$7.50."""
        risk = RiskManager(RiskConfig(**limits))
        risk.record_fill(OLD, "yes", "buy", 75, 60)
        risk.update_marks({OLD: 50})
        return risk

    def test_position_stops_counting(self):
        risk = self.short_45_at_60()
        risk.reduce_only[OLD] = engine.ROLLED_OFF
        self.assertAlmostEqual(risk.total_exposure_usd(), 18.00)   # 45 x 40¢ of NO
        risk.settle(OLD, 0, "no")
        self.assertEqual(risk.net(OLD), 0)
        self.assertEqual(risk.total_exposure_usd(), 0)
        self.assertNotIn(OLD, risk.reduce_only)

    def test_pnl_moves_only_by_the_gap_from_last_mark_to_payout(self):
        risk = self.short_45_at_60()
        self.assertAlmostEqual(risk.session_pnl_usd, 23.99)
        risk.settle(OLD, 0, "no")
        risk.update_marks({})
        # Pays 45 x 60¢ = $27.00, less the 76¢ fee. Up $2.25 (45 x the last 5¢).
        self.assertAlmostEqual(risk.session_pnl_usd, 26.24)
        self.assertAlmostEqual(risk.daily_pnl_usd, 26.24)

    def test_clearing_a_winner_does_not_read_as_a_loss(self):
        # The winner is already in the day's starting P&L, so dropping its
        # ledger instead would read as −$23.99 and trip this $20 limit.
        risk = self.short_45_at_60(max_daily_loss_usd=20.0)
        risk._day_start_pnl_usd = risk.session_pnl_usd
        risk.settle(OLD, 0, "no")
        risk.update_marks({})
        self.assertAlmostEqual(risk.daily_pnl_usd, 2.25)
        self.assertFalse(risk.halted)

    def test_settling_twice_books_it_once(self):
        risk = self.long_75_at_60()
        risk.settle(OLD, 0, "no")
        risk.settle(OLD, 0, "no")
        self.assertAlmostEqual(risk.market_pnl_usd(OLD), -45.00)

    def test_settlement_loss_counts_toward_the_daily_limit(self):
        risk = self.long_75_at_60(max_daily_loss_usd=40.0)
        self.assertFalse(risk.halted)
        risk.settle(OLD, 0, "no")
        risk.update_marks({})
        self.assertAlmostEqual(risk.daily_pnl_usd, -45.00)
        self.assertTrue(risk.halted)

    def test_settled_loss_does_not_come_back_as_reduce_only(self):
        risk = self.long_75_at_60(max_single_market_loss_usd=30.0)
        risk.settle(OLD, 0, "no")
        risk.update_marks({})
        self.assertNotIn(OLD, risk.reduce_only)


class FakeKalshi:
    """GET /markets/<ticker> at the network boundary, counting requests."""
    auth = None
    low_priority = False
    consecutive_errors = 0

    def __init__(self, statuses):
        self.statuses = statuses        # ticker -> (status, result)
        self.gets = []
        self.down = False
        self.during_get = None

    def get_market(self, ticker):
        self.gets.append(ticker)
        if self.during_get:
            self.during_get()
        if self.down:
            raise ConnectionError("Kalshi is down")
        status, result = self.statuses[ticker]
        return {"market": {"ticker": ticker, "status": status, "result": result}}


class InlinePool:
    """Runs background work on the caller's thread, so tests can read the result."""
    def submit(self, fn, *args):
        fn(*args)


def iso(hours_from_now):
    t = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return t.isoformat().replace("+00:00", "Z")


class EngineCase(unittest.TestCase):
    def setUp(self):
        pnl._settlement_cache.clear()
        self.addCleanup(pnl._settlement_cache.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger_path = os.path.join(tmp.name, "fills_log.jsonl")
        self.statuses, self.clients = {}, []
        hub = mock.MagicMock(last_update=None)
        hub.snapshot.return_value = {}

        def build_client(_config):
            self.clients.append(FakeKalshi(self.statuses))
            return self.clients[-1]
        for target, value in (("ledger.LEDGER_PATH", self.ledger_path),
                              ("engine.feeds.HUB", hub),
                              ("engine.HALT_FILE", Path(tmp.name) / "halted.txt"),
                              ("engine.build_client", build_client)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", list(root.handlers))

    def make_engine(self, inline=True):
        e = engine.Engine(BotConfig(kalshi=KalshiConfig(paper_trading=True), risk=RiskConfig(),
                                    watched_tickers=[]))
        if inline:
            e._pool = InlinePool()
        for c in self.clients:
            c.gets.clear()              # what restore() asked at startup
        return e

    def hold(self, e, ticker, net, price, hours_to_close, status=("active", "")):
        e.cfg.watched_tickers.append(ticker)
        e.market_info[ticker] = {"ticker": ticker, "close_time": iso(hours_to_close)}
        e.risk.record_fill(ticker, "yes", "buy" if net > 0 else "sell", abs(net), price)
        self.statuses[ticker] = status

    def gets(self):
        return [t for c in self.clients for t in c.gets]

    def sweep(self, e):
        e._last_settle_check = 0.0
        e._maybe_clear_settled()


class EngineSweepTests(EngineCase):
    def test_data_loop_runs_the_sweep(self):
        e = self.make_engine()
        e.auto_rotate = False
        self.hold(e, OLD, -45, 60, -1, ("finalized", "no"))
        e._poll_books = lambda *args, **kwargs: e._stop.set()   # one pass is enough
        e._data_loop()
        self.assertEqual(e.risk.net(OLD), 0)

    def test_finalized_market_comes_off_every_list(self):
        e = self.make_engine()
        self.hold(e, OLD, -45, 60, -20, ("finalized", "no"))
        self.hold(e, NEW, 15, 50, 20)
        e.risk.reduce_only[OLD] = engine.ROLLED_OFF
        e.maker.deltas[OLD] = ("BTC-USD", 1.0)
        e.maker.deltas[NEW] = ("BTC-USD", 0.13)
        e.maker._place(OLD, "buy", 1, 15, "exit", False)
        self.sweep(e)
        self.assertEqual(e.risk.net(OLD), 0)
        self.assertEqual(e.cfg.watched_tickers, [NEW])
        self.assertNotIn(OLD, e.risk.reduce_only)
        self.assertNotIn(OLD, e.maker.deltas)
        self.assertEqual(e.maker.resting_quotes[OLD], [])
        # The market still trading is left alone, and it is all the dashboard sees.
        self.assertEqual(e.risk.net(NEW), 15)
        self.assertIn(NEW, e.maker.deltas)
        snap = e.snapshot()
        self.assertEqual([m["ticker"] for m in snap["markets"]], [NEW])
        self.assertAlmostEqual(snap["risk"]["exposure_usd"], 7.50)   # 15 x 50¢

    def test_open_or_flat_markets_cost_no_requests(self):
        e = self.make_engine()
        self.hold(e, NEW, 15, 50, 20)                       # hasn't closed yet
        self.hold(e, OLD, 15, 50, -20)                      # closed, but flat
        e.risk.record_fill(OLD, "yes", "sell", 15, 50)
        self.sweep(e)
        self.assertEqual(self.gets(), [])

    def test_closed_market_stays_until_finalized_and_is_asked_once_a_minute(self):
        e = self.make_engine()
        self.hold(e, OLD, -45, 60, -1, ("determined", "no"))
        e._maybe_clear_settled()
        e._maybe_clear_settled()
        self.assertEqual(self.gets(), [OLD])
        self.assertEqual(e.risk.net(OLD), -45)
        self.assertEqual(e.cfg.watched_tickers, [OLD])

        self.statuses[OLD] = ("finalized", "no")
        e._last_settle_check -= engine.SETTLE_CHECK_SECONDS + 1
        e._maybe_clear_settled()
        self.assertEqual(self.gets(), [OLD, OLD])
        self.assertEqual(e.risk.net(OLD), 0)

        e._last_settle_check -= engine.SETTLE_CHECK_SECONDS + 1
        e._maybe_clear_settled()
        self.assertEqual(self.gets(), [OLD, OLD])            # nothing left to ask about

    def test_checks_use_a_low_priority_client_never_the_trading_one(self):
        e = self.make_engine()
        self.hold(e, OLD, -45, 60, -1, ("determined", ""))
        self.sweep(e)
        self.assertEqual(e.client.gets, [])
        self.assertFalse(e.client.low_priority)
        asked = [c for c in self.clients if c.gets]
        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0].low_priority)

    def test_failed_check_keeps_the_position_until_kalshi_answers(self):
        e = self.make_engine()
        self.hold(e, OLD, -45, 60, -1, ("finalized", "no"))
        for c in self.clients:
            c.down = True
        self.sweep(e)
        self.assertEqual(e.risk.net(OLD), -45)
        for c in self.clients:
            c.down = False
        self.sweep(e)
        self.assertEqual(e.risk.net(OLD), 0)

    def test_position_with_unknown_close_is_still_checked(self):
        # e.g. removed from the watchlist by hand: nothing else would clear it.
        e = self.make_engine()
        e.risk.record_fill(OLD, "yes", "sell", 45, 60)
        self.statuses[OLD] = ("finalized", "no")
        self.sweep(e)
        self.assertEqual(e.risk.total_exposure_usd(), 0)

    def test_slow_answer_holds_up_neither_the_data_loop_nor_the_lock(self):
        e = self.make_engine(inline=False)
        self.addCleanup(e._pool.shutdown)
        self.hold(e, OLD, -45, 60, -1, ("finalized", "no"))
        asking, answer = threading.Event(), threading.Event()
        self.addCleanup(answer.set)

        def slow():
            asking.set()
            answer.wait(10)
        for c in self.clients:
            c.during_get = slow
        self.sweep(e)                                       # returned with the request out
        self.assertTrue(asking.wait(10))
        self.assertFalse(answer.is_set())

        free = []

        def trading_loop():
            free.append(e.lock.acquire(timeout=2))
            if free[0]:
                e.lock.release()
        t = threading.Thread(target=trading_loop)
        t.start()
        t.join()
        self.assertEqual(free, [True])
        self.assertEqual(e.risk.net(OLD), -45)
        answer.set()
        e._pool.shutdown(wait=True)
        self.assertEqual(e.risk.net(OLD), 0)

    def test_market_removed_mid_cycle_is_not_evaluated_again(self):
        # The sweep runs on another thread, so run_cycle can be holding a
        # ticker list from just before the removal.
        e = self.make_engine()
        self.hold(e, OLD, 1, 50, 20)
        book = {"yes_bid": 40, "yes_ask": 60, "spread_cents": 20, "yes_bid_size": 10, "yes_ask_size": 10,
                "raw_yes_levels": [], "raw_no_levels": [], "ts": time.time()}

        def removed_while_reading_book(ticker):
            e.remove_ticker(ticker)
            return book
        e._current_book = removed_while_reading_book
        fv = FairValue(50.0, "crypto", "", tradeable=True, underlying="BTC-USD", delta=1.0, hours_left=20)
        with mock.patch("fair_value.supported", return_value=True), \
                mock.patch("fair_value.fair_value", return_value=fv):
            e.run_cycle()
        self.assertIsNone(e.last_error)
        self.assertNotIn(OLD, e.maker.deltas)
        self.assertFalse(e.maker.resting_quotes.get(OLD))
        self.assertNotIn(OLD, e.books)

    def test_settled_market_stops_skewing_the_lean_and_take_cap(self):
        # A strike at the money in the last seconds before close freezes at
        # delta 1.0, so 75 contracts read as $75 per 1% for as long as they stay.
        e = self.make_engine()
        self.hold(e, OLD, 75, 60, -20, ("finalized", "no"))
        self.hold(e, NEW, 75, 50, 20)
        e.maker.deltas[OLD] = ("BTC-USD", 1.0)
        e.maker.deltas[NEW] = ("BTC-USD", 0.13)
        live = FairValue(50.0, "crypto", "", tradeable=True, underlying="BTC-USD", delta=0.13, hours_left=20)
        take = [("buy", 50, 15)]
        self.assertEqual(e.maker._cap_take_delta(take, live), [])

        self.sweep(e)
        lean, port = e.maker._delta_lean(NEW, live)
        self.assertAlmostEqual(port, 9.75)                  # 75 x 0.13
        self.assertAlmostEqual(lean, -1.30)                 # 4¢ x 9.75 / 30, this is the top strike
        self.assertEqual(e.maker._cap_take_delta(take, live), take)


class LedgerReplayTests(EngineCase):
    """The shape of 2026-10-30: four positions restored from the ledger ride
    to settlement while the bot keeps running."""

    def test_clearing_changes_no_pnl_figure(self):
        ledger.append_fill(OLD3, "yes", "sell", 16, 65)                       # no: +$10.40
        ledger.append_fill(OLD, "yes", "sell", 30, 47, liquidity="taker")     # no: +$14.10, 53¢ fee
        ledger.append_fill(OLD2, "yes", "sell", 50, 30)
        ledger.append_fill(OLD2, "yes", "buy", 5, 20)                         # +50¢, then yes: −$31.50
        ledger.append_fill(OLD4, "yes", "sell", 8, 47)                        # no: +$3.76
        tickers = [OLD3, OLD, OLD2, OLD4]
        self.statuses.update({t: ("determined", "") for t in tickers})
        e = self.make_engine()
        self.assertEqual({t: e.risk.net(t) for t in tickers}, {OLD3: -16, OLD: -30, OLD2: -45, OLD4: -8})
        self.assertAlmostEqual(e.risk.total_exposure_usd(), 57.24)            # 5.60 + 15.90 + 31.50 + 4.24
        for t in tickers:
            e.market_info[t] = {"ticker": t, "close_time": iso(-1)}
        self.statuses.update({OLD3: ("finalized", "no"), OLD: ("finalized", "no"),
                              OLD2: ("finalized", "yes"), OLD4: ("finalized", "no")})
        before = pnl.compute(client=e.client)
        with open(self.ledger_path, "rb") as f:
            ledger_before = f.read()

        e.risk.update_marks({OLD3: 2, OLD: 3, OLD2: 96, OLD4: 1})
        self.sweep(e)
        e.risk.update_marks({})
        after = pnl.compute(client=e.client)

        # Nothing open any more, for the bot's limits or for the watchlist.
        self.assertEqual(e.risk.total_exposure_usd(), 0)
        self.assertEqual(e.cfg.watched_tickers, [])
        # The ledger replay: same totals as before, and settlement wrote no fill.
        for key in ("realized_usd", "fees_usd", "net_usd", "unrealized_usd", "equity_usd", "fill_count"):
            self.assertEqual(after[key], before[key], key)
        self.assertAlmostEqual(after["net_usd"], -3.27)                       # −$2.74 realized, 53¢ fees
        with open(self.ledger_path, "rb") as f:
            self.assertEqual(f.read(), ledger_before)
        # The risk manager's own books now agree with the replay, market by market.
        for ml in after["markets"]:
            self.assertAlmostEqual(e.risk.market_pnl_usd(ml.ticker),
                                   (ml.realized_pnl_cents - ml.total_fees_cents) / 100, msg=ml.ticker)
        self.assertAlmostEqual(e.risk.session_pnl_usd, -3.27)
        # And a restart has nothing to restore.
        self.assertEqual(self.make_engine().risk.positions, {})


if __name__ == "__main__":
    unittest.main()
