"""
Long price charts, the full ledger and the Scanner's rows: the model's bars survive
a restart, Kalshi's candles merge in, memory stays flat, and the dashboard's
endpoints serve it all.

    python -m unittest test_dashboard_data

Only Kalshi is faked. The dashboard's Engine is a stand-in, so importing
dashboard.py here starts no feeds, no sweeps and no trading.
"""
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import engine
import price_bars

T = "KXBTCD-26NOV0317-T76999.99"
T0 = 1793664000     # a minute boundary


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def candle(minute, bid=(50, 52, 49, 51), ask=(52, 54, 51, 53), volume="7.00"):
    points = ("open", "high", "low", "close")
    return {"end_period_ts": minute + 60, "volume_fp": volume,
            "yes_bid": {f"{p}_dollars": f"{v / 100:.4f}" for p, v in zip(points, bid)},
            "yes_ask": {f"{p}_dollars": f"{v / 100:.4f}" for p, v in zip(points, ask)}}


class FakeKalshi:
    def __init__(self, candles=()):
        self.candles = list(candles)
        self.calls = []
        self.fail = False

    def _request(self, method, path, params=None, authenticated=True):
        self.calls.append((path, params))
        if self.fail:
            raise OSError("no connection")
        return {"candlesticks": [c for c in self.candles
                                 if params["start_ts"] <= c["end_period_ts"] - 60 < params["end_ts"]]}


class BarStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = price_bars.BarStore(self.dir.name)

    def test_a_minute_becomes_open_high_low_close(self):
        for i, v in enumerate((50.0, 53.5, 48.0, 51.0)):
            self.store.add(T, T0 + i, v)
        self.assertEqual(self.store.read(T), [[T0, 50.0, 53.5, 48.0, 51.0]])

    def test_no_model_price_records_nothing(self):
        self.store.add(T, T0, None)
        self.store.flush(T0 + 600)
        self.assertEqual(self.store.read(T), [])

    def test_bars_survive_a_restart_but_the_open_minute_does_not_get_written_early(self):
        self.store.add(T, T0, 50.0)
        self.store.add(T, T0 + 60, 51.0)
        self.store.flush(T0 + 61)
        self.assertEqual(len(Path(self.dir.name, f"{T}.jsonl").read_text().splitlines()), 1)
        self.assertEqual(price_bars.BarStore(self.dir.name).read(T), [[T0, 50.0, 50.0, 50.0, 50.0]])
        self.assertEqual(self.store.read(T), [[T0, 50.0, 50.0, 50.0, 50.0], [T0 + 60, 51.0, 51.0, 51.0, 51.0]])

    def test_a_market_no_longer_recorded_still_gets_its_last_minute_written(self):
        self.store.add(T, T0, 50.0)
        self.store.flush(T0 + 60)
        self.assertEqual(self.store._open, {})
        self.assertEqual(price_bars.BarStore(self.dir.name).read(T), [[T0, 50.0, 50.0, 50.0, 50.0]])

    def test_a_finished_minute_stays_readable_before_it_is_written(self):
        self.store.add(T, T0, 50.0)
        self.store.add(T, T0 + 60, 51.0)
        self.assertEqual([b[0] for b in self.store.read(T)], [T0, T0 + 60])

    def test_since_filters_and_a_crash_cut_line_is_skipped(self):
        self.store.add(T, T0, 50.0)
        self.store.add(T, T0 + 60, 51.0)
        self.store.flush(T0 + 120)
        with open(Path(self.dir.name, f"{T}.jsonl"), "a") as f:
            f.write("[1793664120,5")
        self.assertEqual([b[0] for b in self.store.read(T)], [T0, T0 + 60])
        self.assertEqual([b[0] for b in self.store.read(T, since=T0 + 60)], [T0 + 60])

    def test_tickers_cannot_reach_outside_the_folder(self):
        for bad in ("../secrets", "..", "a/b", "a\\b", "C:evil", "", ".env", "x" * 200):
            self.assertFalse(price_bars.valid_ticker(bad), bad)
            self.assertEqual(self.store.read(bad), [])
        self.assertTrue(price_bars.valid_ticker(T))

    def test_old_files_are_pruned_and_recent_ones_kept(self):
        self.store.add(T, T0, 50.0)
        self.store.flush(T0 + 60)
        path = Path(self.dir.name, f"{T}.jsonl")
        self.store.prune(path.stat().st_mtime + 86400)
        self.assertTrue(path.exists())
        self.store.prune(path.stat().st_mtime + (price_bars.KEEP_DAYS + 1) * 86400)
        self.assertFalse(path.exists())

    def test_a_write_failure_never_stops_the_writer_thread(self):
        self.store.flush = mock.Mock(side_effect=[OSError("disk full"), None])
        stop = threading.Event()
        with mock.patch.object(price_bars, "FLUSH_SECONDS", 0.01):
            self.store.start()
            for _ in range(200):
                if self.store.flush.call_count >= 2:
                    break
                stop.wait(0.01)
        self.store._stop.set()
        self.assertGreaterEqual(self.store.flush.call_count, 2)

    def test_recording_holds_nothing_beyond_the_current_minute(self):
        for m in range(5000):
            self.store.add(T, T0 + m * 60, 50.0)
            if m % 10 == 0:
                self.store.flush(T0 + m * 60)
        self.assertEqual(len(self.store._open), 1)
        self.assertLessEqual(len(self.store._done), 10)


class CandleTest(unittest.TestCase):
    def test_mid_is_halfway_between_bid_and_ask_at_each_point(self):
        got = price_bars.fetch_candles(FakeKalshi([candle(T0)]), T, T0, T0 + 60)
        self.assertEqual(got, {T0: (51.0, 53.0, 50.0, 52.0, 7.0)})

    def test_a_candle_missing_a_side_is_left_out(self):
        c = candle(T0)
        c["yes_ask"] = {}
        self.assertEqual(price_bars.fetch_candles(FakeKalshi([c]), T, T0, T0 + 60), {})

    def test_the_empty_book_a_market_closes_with_is_not_a_price(self):
        # Seen on KXBTCD-26OCT3017-T78499.99: 0.5c all day, then a jump to 50c in its last minute.
        closing = candle(T0, bid=(0, 0, 0, 0), ask=(1, 100, 1, 100))
        self.assertEqual(price_bars.fetch_candles(FakeKalshi([closing]), T, T0, T0 + 60), {})

    def test_a_one_sided_book_is_still_a_price(self):
        got = price_bars.fetch_candles(FakeKalshi([candle(T0, bid=(0, 0, 0, 0), ask=(3, 3, 3, 3))]), T, T0, T0 + 60)
        self.assertEqual(got[T0][:4], (1.5, 1.5, 1.5, 1.5))

    def test_a_long_window_is_split_into_requests_kalshi_accepts(self):
        k = FakeKalshi()
        price_bars.fetch_candles(k, T, T0, T0 + 7200 * 60)
        self.assertEqual(len(k.calls), 2)
        for _, p in k.calls:
            self.assertLessEqual(p["end_ts"] - p["start_ts"], 5000 * 60)

    def test_a_refresh_tops_up_instead_of_downloading_everything(self):
        k, cache = FakeKalshi([candle(T0)]), price_bars.CandleCache()
        cache.get(k, T, now=T0 + 120)
        calls = len(k.calls)
        cache.get(k, T, now=T0 + 130)
        self.assertEqual(len(k.calls), calls, "within the TTL: no request")
        k.candles.append(candle(T0 + 120))
        got = cache.get(k, T, now=T0 + 180)
        self.assertEqual(len(k.calls), calls + 1)
        self.assertEqual(k.calls[-1][1]["start_ts"], T0 + 120 - price_bars.REFETCH_SECONDS)
        self.assertEqual(sorted(got), [T0, T0 + 120])

    def test_only_the_last_markets_charted_stay_in_memory(self):
        k, cache = FakeKalshi(), price_bars.CandleCache()
        for t in ("A-1", "B-1", "C-1"):
            cache.get(k, t, now=T0)
        self.assertEqual(list(cache._markets), ["B-1", "C-1"])

    def test_kalshi_down_still_returns_what_it_has(self):
        k, cache = FakeKalshi([candle(T0)]), price_bars.CandleCache()
        cache.get(k, T, now=T0 + 60)
        k.fail = True
        self.assertEqual(list(cache.get(k, T, now=T0 + 600)), [T0])
        self.assertEqual(price_bars.CandleCache().get(k, T, now=T0), {})

    def test_rows_merge_market_and_model_by_minute(self):
        got = price_bars.rows({T0: (51.0, 53.0, 50.0, 52.0, 7.0)},
                              [[T0, 60.0, 61.0, 59.0, 60.5], [T0 + 60, 61.0, 61.0, 61.0, 61.0]])
        self.assertEqual(got, [[T0, 51.0, 53.0, 50.0, 52.0, 7.0, 60.0, 61.0, 59.0, 60.5],
                               [T0 + 60, None, None, None, None, None, 61.0, 61.0, 61.0, 61.0]])
        self.assertEqual([r[0] for r in price_bars.rows({T0: (1, 1, 1, 1, 0)}, [], since=T0 + 1)], [])


class SweepRowsTest(unittest.TestCase):
    """The Scanner's search and export can only reach the rows a sweep sends."""

    def test_every_suitable_market_is_sent_up_to_the_cap(self):
        import sweep
        def row(strategy, tradeable=True):
            return {"strategy": strategy, "tradeable": tradeable}
        rows = [row("fair-value")] * 132 + [row("book")] * (sweep.MAX_ROWS_PER_STRATEGY + 5) + [row(None, False)] * 30
        with mock.patch.object(sweep.scanner, "scan", return_value=rows), mock.patch.object(sweep.arb, "scan", return_value={}):
            scan = sweep.run(None)["scan"]
        got = [r["strategy"] for r in scan["rows"]]
        self.assertEqual(got.count("fair-value"), 132)
        self.assertEqual(got.count("book"), sweep.MAX_ROWS_PER_STRATEGY)
        self.assertEqual(got.count(None), 20)
        self.assertEqual(scan["counts"], {"fair-value": 132, "book": sweep.MAX_ROWS_PER_STRATEGY + 5})


class EngineHookTest(unittest.TestCase):
    def test_the_engine_records_the_model_price_once_a_second(self):
        with tempfile.TemporaryDirectory() as d:
            e = engine.Engine.__new__(engine.Engine)
            e.lock, e.books, e.history = threading.RLock(), {}, {}
            e.maker = mock.Mock(last_plan={T: {"fv": 61.5}})
            e.bars = price_bars.BarStore(d)
            book = {"yes_bid": 60, "yes_ask": 62, "spread_cents": 2, "raw_yes_levels": [], "raw_no_levels": []}
            for now in (T0, T0 + 0.2, T0 + 1.1):
                with mock.patch.object(engine.time, "time", return_value=now):
                    e._record_book(T, book)
            self.assertEqual(len(e.history[T]), 2)
            self.assertEqual(e.bars.read(T), [[T0, 61.5, 61.5, 61.5, 61.5]])


class EndpointTest(unittest.TestCase):
    """The real Flask routes, with a stand-in for the trading engine."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        fake_engine = mock.Mock(bars=price_bars.BarStore(cls.dir.name))
        argv, handlers = sys.argv, list(logging.getLogger().handlers)
        sys.modules.pop("dashboard", None)
        with mock.patch("engine.Engine", return_value=fake_engine), mock.patch.object(sys, "argv", ["test"]):
            import dashboard
        logging.getLogger().handlers[:] = handlers
        sys.argv = argv
        cls.dashboard = dashboard
        cls.client = dashboard.app.test_client()
        cls.engine = fake_engine
        cls.fills = [{"ts": T0 + 30, "ticker": T, "side": "yes", "action": "sell", "count": 15, "price_cents": 55},
                     {"ts": T0 + 90, "ticker": "OTHER-1", "side": "yes", "action": "buy", "count": 5, "price_cents": 20},
                     {"ts": T0 + 95, "ticker": T, "side": "yes", "action": "buy", "count": 15, "price_cents": 50}]

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()
        sys.modules.pop("dashboard", None)

    def setUp(self):
        self.dashboard._candles = price_bars.CandleCache()
        self.ledger = mock.patch.object(self.dashboard.ledger, "read_all_fills", return_value=self.fills)
        self.ledger.start()
        self.addCleanup(self.ledger.stop)

    def test_all_fills_come_back_newest_first(self):
        r = self.client.get("/api/fills")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([f["ts"] for f in r.get_json()["fills"]], [T0 + 95, T0 + 90, T0 + 30])

    def test_history_merges_candles_model_and_this_markets_fills(self):
        self.engine.bars.add(T, T0, 60.0)
        k = FakeKalshi([candle(T0)])
        with mock.patch.object(self.dashboard, "SWEEP_CLIENT", k), mock.patch.object(price_bars.time, "time", return_value=T0 + 60):
            j = self.client.get(f"/api/history/{T}").get_json()
        self.assertEqual(j["rows"], [[T0, 51.0, 53.0, 50.0, 52.0, 7.0, 60.0, 60.0, 60.0, 60.0]])
        self.assertEqual([f["ts"] for f in j["fills"]], [T0 + 30, T0 + 95])
        json.dumps(j)

    def test_history_since_returns_only_newer_rows(self):
        k = FakeKalshi([candle(T0), candle(T0 + 60)])
        with mock.patch.object(self.dashboard, "SWEEP_CLIENT", k), mock.patch.object(price_bars.time, "time", return_value=T0 + 120):
            j = self.client.get(f"/api/history/{T}?since={T0 + 60}").get_json()
        self.assertEqual([r[0] for r in j["rows"]], [T0 + 60])

    def test_a_ticker_that_could_name_another_file_is_refused(self):
        self.assertEqual(self.client.get("/api/history/..%2F..%2Fsecrets").status_code, 404)
        self.assertEqual(self.client.get("/api/history/.env").status_code, 400)


if __name__ == "__main__":
    unittest.main()
