"""
Live trading: what reaches Kalshi, and what the bot believes afterwards.

    python -m unittest test_live

Only Kalshi and the price feeds are faked (fake_exchange.py, at the HTTP
boundary). The client, market maker, risk manager, ledger and engine are real.
These prove the bot follows Kalshi's docs, not that the docs are complete:
live_check.py is what tries the real API.
"""
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import engine
import feeds
import ledger
import live_check
import pnl
import show_pnl
from config import BotConfig, KalshiConfig, RiskConfig, live_risk
from fair_value import FairValue
from fake_exchange import BASE_URL, FakeAuth, FakeExchange, FakeResponse
from kalshi_client import KalshiClient, KalshiRateLimitError, WriteBudget
from market_maker import MarketMaker
from risk_manager import RiskManager

T = "KXBTCD-26NOV0517-T80000.00"
T2 = "KXBTCD-26NOV0517-T80500.00"


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def small_limits(**over):
    limits = dict(quote_size=2, take_size=2, max_position_per_market=10, max_total_exposure_usd=40.0,
                  max_daily_loss_usd=20.0, max_single_market_loss_usd=8.0, delta_lean_cents=0.0,
                  max_take_book_delta_usd=None, max_take_hours=None)
    return RiskConfig(**{**limits, **over})


def model(cents=50.0, tradeable=True):
    return FairValue(cents, "crypto", "", tradeable=tradeable, underlying="BTC-USD", delta=0.1, hours_left=6)


def book(bid=40, ask=60, bid_size=100, ask_size=100):
    return {"yes_bid": bid, "yes_ask": ask, "yes_bid_size": bid_size, "yes_ask_size": ask_size,
            "spread_cents": ask - bid, "raw_yes_levels": [], "raw_no_levels": [], "ts": time.time()}


def real_client(exchange):
    client = KalshiClient(BASE_URL, FakeAuth())
    client._session = exchange
    return client


class LiveCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger_path = os.path.join(tmp.name, "fills_log.jsonl")
        for patcher in (mock.patch("ledger.LEDGER_PATH", self.ledger_path),
                        mock.patch.object(KalshiClient, "MAX_REQUESTS_PER_SECOND", 1_000_000)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("fair_value.supported", True), ("fair_value.fair_value", model())):
            p = mock.patch(target, return_value=value)
            setattr(self, target.split(".")[1], p.start())
            self.addCleanup(p.stop)
        self.ex = FakeExchange()
        self.ex.set_book(T, 40, 60)

    def make_maker(self, **limits):
        cfg = small_limits(**limits)
        self.risk = RiskManager(cfg)
        maker = MarketMaker(real_client(self.ex), self.risk,
                            BotConfig(kalshi=KalshiConfig(paper_trading=False), risk=cfg, watched_tickers=[]))
        maker.available_cents = self.ex.balance
        maker.budget = WriteBudget(rate=1_000_000)   # WriteBudgetTests has the real one
        return maker

    def evaluate(self, maker, ticker=T, **book_args):
        maker.evaluate_market(ticker, {"ticker": ticker}, book(**book_args))

    def price_model(self, cents):
        self.fair_value.return_value = model(cents)


class QuoteOrderTests(LiveCase):
    def test_quotes_rest_on_kalshi_in_the_v2_format(self):
        # Model 50¢, book 40/60: 8¢ edge wants 42/58, the touch caps them at 41/59.
        self.evaluate(self.make_maker())
        resting = sorted(self.ex.resting(T), key=lambda o: o["side"])
        self.assertEqual([(o["side"], o["price"], o["count"]) for o in resting], [("ask", 59, 2.0), ("bid", 41, 2.0)])

    def test_quote_cannot_cross_and_take(self):
        self.evaluate(self.make_maker())
        self.assertTrue(all(o["post_only"] for o in self.ex.resting(T)))

    def test_quote_expires_on_its_own_if_the_bot_dies(self):
        # 2026-10-26: quotes left resting while the PC slept filled at stale prices for -$115.
        self.evaluate(self.make_maker())
        for o in self.ex.resting(T):
            self.assertGreater(o["expiration_time"], time.time() + 20)
            self.assertLess(o["expiration_time"], time.time() + 120)

    def test_bot_tracks_the_id_kalshi_gave_the_order(self):
        maker = self.make_maker()
        self.evaluate(maker)
        tracked = {q.order_id for q in maker.resting_quotes[T]}
        self.assertEqual(tracked, {o["order_id"] for o in self.ex.resting(T)})

    def test_replacing_a_quote_really_removes_the_old_one_from_kalshi(self):
        maker = self.make_maker()
        self.evaluate(maker)
        self.price_model(44.0)                 # target bid falls from 41 to 36, ask to 52 → capped at 59
        self.evaluate(maker)
        bids = [o["price"] for o in self.ex.resting(T) if o["side"] == "bid"]
        self.assertEqual(bids, [36])

    def test_cancelling_an_order_already_gone_is_not_an_error(self):
        maker = self.make_maker()
        self.evaluate(maker)
        for order in self.ex.resting(T):
            self.ex.hit(order["order_id"])
        maker._cancel_resting(T, "test")
        self.assertEqual(maker.client.consecutive_errors, 0)
        self.assertEqual(self.ex.resting(T), [])


class TakeOrderTests(LiveCase):
    def test_a_take_that_only_partly_fills_leaves_nothing_on_the_book(self):
        # A take resting at an old price is the -$115 failure again, and nothing tracks it.
        self.price_model(70.0)                 # 60¢ ask is 10¢ cheap, 8.3¢ after the fee
        self.ex.set_book(T, 40, 60, ask_size=1)   # only 1 left by the time the order for 2 arrives
        self.evaluate(self.make_maker())
        self.assertEqual(self.ex.positions[T], 1.0)
        self.assertEqual([o for o in self.ex.resting(T) if o["side"] == "bid"], [])

    def test_buy_take_lifts_the_ask(self):
        self.price_model(70.0)
        self.evaluate(self.make_maker())
        self.assertEqual(self.ex.positions[T], 2.0)
        self.assertEqual(self.ex.fills[0]["yes_price_dollars"], "0.6000")

    def test_sell_take_hits_the_bid(self):
        self.price_model(30.0)
        self.evaluate(self.make_maker())
        self.assertEqual(self.ex.positions[T], -2.0)
        self.assertEqual(self.ex.fills[0]["yes_price_dollars"], "0.4000")

    def test_a_rate_limited_order_is_not_sent_again(self):
        # Waiting and resending a take means trading at a price from seconds ago.
        client = real_client(self.ex)
        self.ex.order_failures.append(429)
        with mock.patch("kalshi_client.time.sleep") as sleep, self.assertRaises(KalshiRateLimitError):
            client.create_order(T, "buy", 2, 60, "immediate_or_cancel")
        sleep.assert_not_called()
        self.assertEqual(len(self.ex.writes()), 1)

    def test_an_order_does_not_queue_behind_market_data_requests(self):
        client = real_client(self.ex)
        with mock.patch.object(KalshiClient, "_wait_for_slot") as slot:
            client.create_order(T, "buy", 2, 41, post_only=True)
            client.cancel_order(self.ex.resting(T)[0]["order_id"], T)
        slot.assert_not_called()

    def test_a_hung_order_gives_up_sooner_than_a_read(self):
        # The engine holds its lock during an order, so 10s here freezes everything.
        client = real_client(self.ex)
        client.create_order(T, "buy", 2, 41, post_only=True)
        order_timeout = self.ex.writes()[-1][4]
        self.assertLessEqual(order_timeout, 3.0)


class ReadingKalshiTests(LiveCase):
    def test_fills_come_back_across_every_page(self):
        client = real_client(self.ex)
        for _ in range(3):
            client.create_order(T, "buy", 1, 60, "immediate_or_cancel")
        self.ex.page_size = 2
        self.assertEqual(len(client.get_fills()), 3)

    def test_a_fill_reads_the_same_from_rest_and_the_stream(self):
        client = real_client(self.ex)
        client.create_order(T, "sell", 2, 40, "immediate_or_cancel")
        rest = client.get_fills()[0]
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        stream.handle(self.ex.ws_message(self.ex.fills[0]))
        pushed = stream.drain_fills()[0]
        for key in ("trade_id", "order_id", "ticker", "action", "count", "price_cents", "maker", "fee_cents", "ts"):
            self.assertEqual(rest[key], pushed[key], key)
        # 2 contracts sold at 40¢ as the taker. Fee: 7% x 2 x .40 x .60 = 3.36¢, rounded up.
        self.assertEqual((rest["action"], rest["count"], rest["price_cents"], rest["maker"], rest["fee_cents"]),
                         ("sell", 2, 40, False, 4))

    def test_positions_come_back_signed_across_every_page(self):
        client = real_client(self.ex)
        self.ex.set_book(T2, 40, 60)
        client.create_order(T, "sell", 2, 40, "immediate_or_cancel")
        client.create_order(T2, "buy", 3, 60, "immediate_or_cancel")
        self.ex.page_size = 1
        self.assertEqual(client.get_positions(), {T: -2.0, T2: 3.0})

    def test_resting_orders_come_back_across_every_page(self):
        client = real_client(self.ex)
        for price in (30, 31, 32):
            client.create_order(T, "buy", 1, price, post_only=True)
        self.ex.page_size = 2
        self.assertEqual(len(client.get_orders(status="resting")["orders"]), 3)

    def test_a_malformed_fill_is_not_booked_as_something_else(self):
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        stream.handle({"type": "fill", "sid": 1, "msg": {"market_ticker": T, "count_fp": "2.00"}})
        self.assertEqual(stream.drain_fills(), [])

    def test_a_fill_with_no_direction_is_not_guessed_at(self):
        real_client(self.ex).create_order(T, "sell", 2, 40, "immediate_or_cancel")
        message = self.ex.ws_message(self.ex.fills[0])
        del message["msg"]["book_side"]
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        stream.handle(message)
        self.assertEqual(stream.drain_fills(), [])


class MarketStreamTests(unittest.TestCase):
    TRADE = {"type": "trade", "msg": {"market_ticker": T, "ts": 1, "yes_price_dollars": "0.5000",
                                      "count_fp": "1.00", "taker_side": "yes", "trade_id": "x"}}

    def test_it_can_skip_the_trade_tape(self):
        stream = feeds.KalshiStream(FakeAuth(), "wss://fake", [T], keep_trades=False)
        stream.handle(self.TRADE)
        self.assertEqual(stream.drain_trades(T), [])

    def test_paper_still_gets_the_trade_tape(self):
        stream = feeds.KalshiStream(FakeAuth(), "wss://fake", [T])
        stream.handle(self.TRADE)
        self.assertEqual(len(stream.drain_trades(T)), 1)


class StreamHealthTests(unittest.TestCase):
    def test_not_healthy_until_subscribed_and_caught_up(self):
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        self.assertFalse(stream.healthy)
        stream.handle({"id": 1, "type": "subscribed", "msg": {"channel": "fill", "sid": 13}})
        self.assertFalse(stream.healthy)                # fills may have happened before this
        stream.caught_up = True
        self.assertTrue(stream.healthy)

    def test_a_dropped_connection_needs_catching_up_again(self):
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        stream.handle({"id": 1, "type": "subscribed", "msg": {"channel": "fill", "sid": 13}})
        stream.caught_up = True
        stream.disconnected()
        self.assertFalse(stream.healthy)
        stream.handle({"id": 1, "type": "subscribed", "msg": {"channel": "fill", "sid": 14}})
        self.assertFalse(stream.healthy)


class TakeBookingTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.price_model(70.0)             # the 60¢ ask is 8.3¢ cheap after the fee
        self.maker = self.make_maker()

    def test_filled_take_counts_toward_the_position_at_once(self):
        self.evaluate(self.maker)
        self.assertEqual(self.risk.net(T), 2)
        self.assertAlmostEqual(self.risk.total_exposure_usd(), 1.20)   # 2 x 60¢

    def test_take_is_recorded_as_live_with_what_kalshi_charged(self):
        self.evaluate(self.maker)
        row = ledger.read_all_fills()[0]
        # 7% x 2 x .60 x .40 = 3.36¢, rounded up to 4¢.
        self.assertEqual((row["mode"], row["action"], row["count"], row["price_cents"], row["liquidity"], row["fee_cents"]),
                         ("live", "buy", 2, 60, "taker", 4))
        self.assertEqual(row["strategy"], "take")
        self.assertEqual(row["model_cents"], 70.0)

    def test_the_streams_copy_of_a_booked_take_does_not_count_twice(self):
        self.evaluate(self.maker)
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        stream.handle(self.ex.ws_message(self.ex.fills[0]))
        self.maker.process_live_fills(stream.drain_fills())
        self.assertEqual(self.risk.net(T), 2)
        self.assertEqual(len(ledger.read_all_fills()), 1)

    def test_a_take_whose_answer_was_lost_is_booked_from_the_stream(self):
        self.ex.lose_next_response = True
        self.evaluate(self.maker)
        self.assertEqual(self.risk.net(T), 0)      # nothing known yet
        self.maker.process_live_fills(self.maker.client.get_fills())
        self.assertEqual(self.risk.net(T), 2)
        self.assertEqual(ledger.read_all_fills()[0]["liquidity"], "taker")

    def test_a_take_that_misses_books_nothing(self):
        # The ask is gone by the time the order arrives.
        self.ex.set_book(T, 40, 75)
        self.evaluate(self.maker)
        self.assertEqual(self.risk.net(T), 0)
        self.assertEqual(ledger.read_all_fills(), [])

    def test_position_limit_stops_more_takes(self):
        self.maker = self.make_maker(max_position_per_market=2)
        self.evaluate(self.maker)
        self.assertEqual(self.risk.net(T), 2)
        self.evaluate(self.maker)
        self.assertEqual(self.risk.net(T), 2)      # without the booking this would keep buying

    def test_the_take_cost_comes_off_what_the_bot_thinks_it_can_spend(self):
        before = self.maker.available_cents
        self.evaluate(self.maker)
        self.assertEqual(before - self.maker.available_cents, 124)    # 2 x 60¢ + 4¢ fee


class QuoteFillTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.maker = self.make_maker()
        self.evaluate(self.maker)              # bid 2 @ 41, ask 2 @ 59
        self.bid = next(o for o in self.ex.resting(T) if o["side"] == "bid")["order_id"]
        self.ask = next(o for o in self.ex.resting(T) if o["side"] == "ask")["order_id"]
        self.stream = feeds.UserStream(FakeAuth(), "wss://fake")

    def deliver(self, fill):
        self.stream.handle(self.ex.ws_message(fill))
        self.maker.process_live_fills(self.stream.drain_fills())

    def test_bid_filled_makes_the_position_long(self):
        self.deliver(self.ex.hit(self.bid))
        self.assertEqual(self.risk.net(T), 2)
        self.assertAlmostEqual(self.risk.total_exposure_usd(), 0.82)

    def test_ask_filled_makes_the_position_short(self):
        self.deliver(self.ex.hit(self.ask))
        self.assertEqual(self.risk.net(T), -2)
        self.assertAlmostEqual(self.risk.total_exposure_usd(), 0.82)   # 2 x 41¢ of NO

    def test_fill_is_recorded_with_kalshis_fee_not_the_formula(self):
        # Makers pay nothing on KXBTCD today; if Kalshi charges one anyway, that's what counts.
        self.deliver(self.ex.hit(self.bid, fee_cents=1))
        row = ledger.read_all_fills()[0]
        self.assertEqual((row["mode"], row["liquidity"], row["fee_cents"], row["strategy"]),
                         ("live", "maker", 1, "fair-value"))
        self.assertAlmostEqual(self.risk.market_pnl_usd(T), -0.01)

    def test_same_fill_from_stream_and_rest_counts_once(self):
        self.deliver(self.ex.hit(self.bid))
        self.maker.process_live_fills(self.maker.client.get_fills())
        self.assertEqual(self.risk.net(T), 2)
        self.assertEqual(len(ledger.read_all_fills()), 1)

    def test_partly_filled_quote_stays_tracked_with_what_is_left(self):
        self.deliver(self.ex.hit(self.bid, count=1))
        quote = next(q for q in self.maker.resting_quotes[T] if q.order_id == self.bid)
        self.assertEqual(quote.count, 1)

    def test_fully_filled_quote_stops_being_tracked(self):
        self.deliver(self.ex.hit(self.bid))
        self.assertEqual([q.order_id for q in self.maker.resting_quotes[T]], [self.ask])

    def test_part_of_a_contract_keeps_its_exact_count(self):
        self.deliver(self.ex.hit(self.bid, count=0.5))
        self.deliver(self.ex.hit(self.bid, count=0.25))
        self.assertEqual(self.risk.net(T), 0.75)

    def test_fills_on_a_replaced_quote_still_count(self):
        # A trade can reach us after the bot has moved on from that quote.
        fill = self.ex.hit(self.bid, count=1)
        self.maker._cancel_resting(T, "moving")
        self.deliver(fill)
        self.assertEqual(self.risk.net(T), 1)

    def test_a_fill_the_bot_never_placed_still_counts(self):
        # e.g. something bought by hand on the website: it's still money at risk.
        order = self.maker.client.create_order(T2, "buy", 1, 50, post_only=True)
        self.deliver(self.ex.hit(order["order_id"]))
        self.assertEqual(self.risk.net(T2), 1)

    def test_position_limit_sees_quote_fills(self):
        self.maker = self.make_maker(max_position_per_market=2)
        self.deliver(self.ex.hit(self.bid))
        self.evaluate(self.maker)
        sides = {q.action for q in self.maker.resting_quotes[T]}
        self.assertEqual(sides, {"sell"})

    def test_daily_loss_limit_sees_quote_fills(self):
        self.maker = self.make_maker(max_daily_loss_usd=0.50)
        self.deliver(self.ex.hit(self.bid))
        self.risk.update_marks({T: 10})        # bought at 41¢, now 10¢: down 62¢
        self.assertTrue(self.risk.halted)

    def test_market_loss_limit_sees_quote_fills(self):
        self.maker = self.make_maker(max_single_market_loss_usd=0.50)
        self.deliver(self.ex.hit(self.bid))
        self.risk.update_marks({T: 10})
        self.assertIn(T, self.risk.reduce_only)


class RejectedOrderTests(LiveCase):
    def test_five_rejected_orders_in_a_row_halt_the_bot(self):
        maker = self.make_maker()
        self.ex.order_failures.extend([400] * 5)
        for _ in range(3):
            self.evaluate(maker)
        self.assertTrue(self.risk.halted)
        self.assertIn("rejected", self.risk.halt_reason)
        self.assertEqual(self.ex.resting(T), [])   # the 6th order would have gone through

    def test_four_do_not(self):
        maker = self.make_maker()
        self.ex.order_failures.extend([400] * 4)
        self.evaluate(maker)
        self.evaluate(maker)
        self.assertFalse(self.risk.halted)

    def test_an_accepted_order_starts_the_count_again(self):
        maker = self.make_maker()
        self.ex.order_failures.extend([400] * 4)
        self.evaluate(maker)
        self.evaluate(maker)                   # four rejected
        self.evaluate(maker)                   # both accepted
        self.ex.order_failures.extend([400] * 4)
        maker._cancel_resting(T, "test")
        self.evaluate(maker)
        self.evaluate(maker)
        self.assertFalse(self.risk.halted)

    def test_an_order_with_no_answer_counts_as_a_failure(self):
        maker = self.make_maker()
        self.ex.order_failures.extend([requests.Timeout("timed out")] * 5)
        for _ in range(3):
            self.evaluate(maker)
        self.assertTrue(self.risk.halted)

    def test_a_quote_that_would_cross_isnt_a_failure(self):
        # Kalshi refuses it with a 400. In a fast market that's the post-only
        # flag doing its job, not something that should halt the bot.
        maker = self.make_maker()
        self.ex.set_book(T, 40, 41)            # the book moved after the bot read it
        for _ in range(6):
            maker._place(T, "buy", 41, 2, "fair-value", False)
        self.assertFalse(self.risk.halted)
        self.assertEqual(maker.orders_failed_in_a_row, 0)
        self.assertEqual(maker.client.consecutive_errors, 0)
        self.assertEqual(maker.resting_quotes.get(T, []), [])

    def test_a_quote_that_would_cross_doesnt_clear_real_failures(self):
        maker = self.make_maker()
        self.ex.order_failures.extend([400] * 4)
        for _ in range(4):
            maker._place(T, "buy", 30, 2, "fair-value", False)
        self.ex.set_book(T, 40, 41)
        maker._place(T, "buy", 41, 2, "fair-value", False)
        self.assertEqual(maker.orders_failed_in_a_row, 4)

    def test_kalshis_refusal_for_an_unfunded_exchange_counts_as_a_failure(self):
        # What happened for real on 2026-11-04.
        maker = self.make_maker()
        self.ex.shards.update({0: 4924, 2: 0})
        for _ in range(3):
            self.evaluate(maker)
        self.assertTrue(self.risk.halted)
        self.assertIn("insufficient_shard_balance", self.risk.halt_reason)

    def test_an_answer_in_another_shape_counts_as_a_failure(self):
        # The order may be up, but with no ID nothing can cancel it. Carrying on
        # would place another every cycle, so it must count toward the halt.
        maker = self.make_maker()
        real = self.ex._post_order
        self.ex._post_order = lambda body: FakeResponse(201, {"order": json.loads(real(body).text)})
        for _ in range(3):
            self.evaluate(maker)
        self.assertTrue(self.risk.halted)
        self.assertEqual(maker.resting_quotes.get(T, []), [])

    def test_a_take_answer_with_no_price_is_booked_from_the_fills_instead(self):
        self.price_model(70.0)
        maker = self.make_maker()
        real = self.ex._post_order

        def no_price(body):
            answer = real(body)
            return FakeResponse(answer.status_code, {k: v for k, v in answer.json().items() if k != "average_fill_price"})
        self.ex._post_order = no_price
        self.evaluate(maker)
        self.assertEqual(self.risk.net(T), 0)
        maker.process_live_fills(maker.client.get_fills())
        self.assertEqual(self.risk.net(T), 2)

    def test_a_failed_take_still_lets_the_market_requote(self):
        self.price_model(70.0)
        maker = self.make_maker()
        self.ex.order_failures.append(400)     # the take
        self.evaluate(maker)
        self.assertEqual({o["side"] for o in self.ex.resting(T)}, {"ask"})   # the take used up the buy side


class PostOnlyTests(LiveCase):
    def test_a_quote_kalshi_cancels_on_arrival_is_not_tracked(self):
        # The docs don't say if a post-only order that would cross errors or
        # comes back empty, so both have to be safe.
        self.ex.post_only_cross_answers_empty = True
        maker = self.make_maker()
        self.ex.set_book(T, 40, 41)            # the book moved after the bot read it
        maker._place(T, "buy", 41, 2, "fair-value", False)
        self.assertEqual(maker.resting_quotes.get(T, []), [])


class FundsTests(LiveCase):
    def test_no_order_goes_out_before_the_balance_is_known(self):
        maker = self.make_maker()
        maker.available_cents = None
        self.evaluate(maker)
        self.assertEqual(self.ex.writes(), [])

    def test_an_order_the_account_cant_pay_for_is_not_sent(self):
        maker = self.make_maker()
        maker.available_cents = 100            # bid 2 @ 41 = 82¢ fits; ask 2 @ 59 holds another 82¢
        self.evaluate(maker)
        self.assertEqual([o["side"] for o in self.ex.resting(T)], ["bid"])

    def test_not_being_able_to_pay_is_not_a_rejection(self):
        maker = self.make_maker()
        maker.available_cents = 1
        for _ in range(4):
            self.evaluate(maker)
        self.assertFalse(self.risk.halted)

    def test_closing_a_position_needs_no_money(self):
        maker = self.make_maker()
        self.ex.set_book(T2, 40, 60)
        maker._place(T2, "buy", 41, 2, "fair-value", False)     # 82¢ held elsewhere, more than there is
        self.risk.record_fill(T, "yes", "buy", 2, 41)
        self.ex.positions[T] = 2.0
        maker.available_cents = 0
        self.evaluate(maker)
        self.assertEqual([o["side"] for o in self.ex.resting(T)], ["ask"])


def iso(hours_from_now):
    t = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return t.isoformat().replace("+00:00", "Z")


class InlinePool:
    def submit(self, fn, *args):
        fn(*args)


SUBSCRIBED = {"id": 1, "type": "subscribed", "msg": {"channel": "fill", "sid": 13}}


class EngineLiveCase(LiveCase):
    def setUp(self):
        super().setUp()
        pnl._settlement_cache.clear()
        self.addCleanup(pnl._settlement_cache.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub = mock.MagicMock(last_update=None)
        hub.snapshot.return_value = {}
        for target, value in (("engine.feeds.HUB", hub),
                              ("engine.HALT_FILE", Path(tmp.name) / "halted.txt"),
                              ("engine.RISK_DAY_FILE", Path(tmp.name) / "risk_day.json"),
                              ("engine.build_client", lambda _config: real_client(self.ex)),
                              ("engine.feeds.KalshiStream", mock.MagicMock()),
                              ("engine.price_bars.BarStore", mock.MagicMock()),
                              ("feeds.UserStream.start", lambda _self: None)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", list(root.handlers))
        self.ex.markets[T] = {"close_time": iso(6)}

    def make_engine(self, **limits):
        e = engine.Engine(BotConfig(kalshi=KalshiConfig(paper_trading=False), risk=small_limits(**limits),
                                    watched_tickers=[]))
        e.auto_rotate = False
        e._pool = InlinePool()
        return e

    def ready(self, e):
        """The start sequence, without the trading threads."""
        ok, why = e._prepare_live()
        self.assertTrue(ok, why)
        e.user_stream.handle(SUBSCRIBED)
        e._live_tick()
        return e

    def watch(self, e, ticker=T, hours_to_close=6, **book_args):
        if ticker not in e.cfg.watched_tickers:
            e.cfg.watched_tickers.append(ticker)
        e.market_info[ticker] = {"ticker": ticker, "close_time": iso(hours_to_close), "exchange_index": 2}
        e._current_book = lambda _t: book(**book_args)
        e._refresh_balance()

    def check_positions(self, e):
        e._last_reconcile = 0
        e._live_tick()


class LiveStartTests(EngineLiveCase):
    def test_start_cancels_orders_a_previous_run_left(self):
        real_client(self.ex).create_order(T, "buy", 2, 30, post_only=True)
        self.ready(self.make_engine())
        self.assertEqual(self.ex.resting(T), [])

    def test_start_leaves_orders_on_other_kinds_of_market_alone(self):
        self.ex.shards[0] = 5000
        self.ex.set_book("FED-26DEC-T3", 40, 60)
        real_client(self.ex).create_order("FED-26DEC-T3", "buy", 2, 30, post_only=True)
        self.ready(self.make_engine())
        self.assertEqual(len(self.ex.resting("FED-26DEC-T3")), 1)

    def test_start_books_fills_it_missed_while_down(self):
        e = self.make_engine()
        real_client(self.ex).create_order(T, "buy", 2, 60, "immediate_or_cancel")
        self.ready(e)
        self.assertEqual(e.risk.net(T), 2)

    def try_to_start(self):
        e = self.make_engine()
        e.cfg.watched_tickers.append(T)
        ok, why = e.start()
        self.assertFalse(e.running)
        self.assertFalse(ok)
        return why

    def test_start_refuses_a_position_it_cant_explain(self):
        # Trading from a wrong position is the 2026-10-21 restart: 2x past the
        # limits and exit quotes working the wrong side, for -$87.
        self.ex.positions[T] = 5.0
        self.assertIn(T, self.try_to_start())
        self.assertEqual(self.ex.writes(), [])

    def test_start_refuses_when_it_cant_read_the_balance(self):
        self.ex.read_failures["/portfolio/balance"] = requests.ConnectionError("down")
        self.assertIn("balance", self.try_to_start())

    def test_start_refuses_when_it_cant_read_positions(self):
        self.ex.read_failures["/portfolio/positions"] = requests.ConnectionError("down")
        self.assertIn("position", self.try_to_start())

    def test_start_learns_the_balance(self):
        e = self.make_engine()
        e.cfg.watched_tickers.append(T)
        self.ready(e)
        self.assertEqual(e.maker.available_cents, 5000)

    def test_start_refuses_when_the_money_is_on_another_exchange(self):
        # 2026-11-04: all $49.24 on exchange 0, BTC on exchange 2. The total
        # looks fine, and every order comes back 404.
        self.ex.shards.update({0: 4924, 2: 0})
        why = self.try_to_start()
        self.assertIn("exchange 2", why)
        self.assertIn("exchange-indexes", why)
        self.assertEqual(self.ex.writes(), [])

    def test_start_goes_ahead_with_no_money_if_there_are_positions_to_close(self):
        self.ex.shards.update({0: 4924, 2: 0})
        ledger.append_fill(T, "yes", "buy", 2, 41, mode="live", liquidity="maker", fee_cents=0, order_id="o", trade_id="t")
        self.ex.positions[T] = 2.0
        self.assertTrue(self.make_engine()._prepare_live()[0])


class LiveRestartTests(EngineLiveCase):
    def test_restart_restores_live_positions_and_ignores_paper_ones(self):
        ledger.append_fill(T2, "yes", "buy", 40, 50, mode="paper", strategy="take", liquidity="taker")
        ledger.append_fill(T, "yes", "sell", 2, 40, mode="live", strategy="take", liquidity="taker", fee_cents=4,
                           order_id="o1", trade_id="t1")
        e = self.make_engine()
        self.assertEqual(e.risk.net(T), -2)
        self.assertEqual(e.risk.net(T2), 0)
        self.assertIn(T, e.cfg.watched_tickers)

    def test_restart_does_not_double_count_a_take_booked_from_its_answer(self):
        self.price_model(70.0)
        first = self.ready(self.make_engine())
        self.watch(first)
        first.run_cycle()
        self.assertEqual(first.risk.net(T), 2)

        second = self.ready(self.make_engine())
        self.assertEqual(second.risk.net(T), 2)
        self.assertEqual(len(ledger.read_all_fills()), 1)

    def test_restart_does_not_double_count_a_fill_from_the_stream(self):
        first = self.ready(self.make_engine())
        self.watch(first)
        first.run_cycle()
        bid = next(q for q in first.maker.resting_quotes[T] if q.action == "buy")
        first.user_stream.handle(self.ex.ws_message(self.ex.hit(bid.order_id)))
        first.run_cycle()
        self.assertEqual(first.risk.net(T), 2)

        second = self.ready(self.make_engine())
        self.assertEqual(second.risk.net(T), 2)

    def test_restart_uses_the_fee_kalshi_charged(self):
        ledger.append_fill(T, "yes", "buy", 2, 60, mode="live", liquidity="maker", fee_cents=7, order_id="o", trade_id="t")
        e = self.make_engine()
        self.assertEqual(e.risk.positions[T].total_fees_cents, 7)

    def test_settled_while_down_is_booked_and_not_open(self):
        self.ex.markets[T] = {"status": "finalized", "result": "no", "close_time": iso(-3)}
        ledger.append_fill(T, "yes", "buy", 2, 60, mode="live", liquidity="taker", fee_cents=4, order_id="o", trade_id="t")
        e = self.make_engine()
        self.assertEqual(e.risk.net(T), 0)
        e.risk.update_marks({})
        self.assertAlmostEqual(e.risk.daily_pnl_usd, 0.0)      # nothing saved for today, so the day starts here
        self.assertAlmostEqual(e.risk.market_pnl_usd(T), -1.24)


class DailyLossAcrossRestartsTests(EngineLiveCase):
    def lose(self, e, cents=62):
        """Buy 2 at 41¢, sell them lower: flat, and `cents` down."""
        e.maker.process_live_fills(self.round_trip(cents))
        e.risk.update_marks({})

    def round_trip(self, cents):
        self._n = getattr(self, "_n", 0) + 2
        common = {"client_order_id": None, "ticker": T, "count": 2, "maker": True, "fee_cents": 0, "ts": time.time()}
        return [{**common, "trade_id": f"t{self._n}", "order_id": f"o{self._n}", "action": "buy", "price_cents": 41},
                {**common, "trade_id": f"t{self._n + 1}", "order_id": f"o{self._n + 1}", "action": "sell",
                 "price_cents": 41 - cents / 2}]

    def test_todays_losses_still_count_after_a_restart(self):
        # The server restarted four times on 2026-10-28. A limit that forgets
        # on restart isn't a daily limit.
        first = self.make_engine(max_daily_loss_usd=1.00)
        self.lose(first, 62)
        self.assertAlmostEqual(first.risk.daily_pnl_usd, -0.62)
        self.assertFalse(first.risk.halted)

        second = self.make_engine(max_daily_loss_usd=1.00)
        second.risk.update_marks({})
        self.assertAlmostEqual(second.risk.daily_pnl_usd, -0.62)
        self.lose(second, 50)
        self.assertTrue(second.risk.halted)

    def test_a_new_day_starts_from_zero_after_a_restart(self):
        first = self.make_engine(max_daily_loss_usd=1.00)
        self.lose(first, 62)
        saved = json.loads(engine.RISK_DAY_FILE.read_text())
        saved["day"] = "2020-01-01"
        engine.RISK_DAY_FILE.write_text(json.dumps(saved))

        second = self.make_engine(max_daily_loss_usd=1.00)
        second.risk.update_marks({})
        self.assertAlmostEqual(second.risk.daily_pnl_usd, 0.0)

    def test_day_rollover_is_saved_for_the_next_restart(self):
        e = self.make_engine(max_daily_loss_usd=1.00)
        self.lose(e, 62)
        e.risk._day = date(2020, 1, 1)
        e.risk.update_marks({})                # the day turns over: -$0.62 is yesterday's
        self.assertAlmostEqual(e.risk.daily_pnl_usd, 0.0)

        restarted = self.make_engine(max_daily_loss_usd=1.00)
        restarted.risk.update_marks({})
        self.assertAlmostEqual(restarted.risk.daily_pnl_usd, 0.0)

    def test_open_position_is_valued_from_the_first_moment(self):
        # Valued at nothing until the first book arrives, a position up $15
        # at midnight would look like a $15 loss and could trip the limit.
        ledger.append_fill(T, "yes", "buy", 2, 41, mode="live", liquidity="maker", fee_cents=0, order_id="o", trade_id="t")
        self.ex.set_book(T, 60, 62)
        e = self.make_engine()
        e.risk.update_marks({})
        self.assertAlmostEqual(e.risk.session_pnl_usd, 0.40)   # 2 x (61 - 41)


class LiveCycleTests(EngineLiveCase):
    def test_fills_from_the_stream_count_before_the_next_decision(self):
        e = self.ready(self.make_engine(max_position_per_market=2))
        self.watch(e)
        e.run_cycle()
        bid = next(q for q in e.maker.resting_quotes[T] if q.action == "buy")
        e.user_stream.handle(self.ex.ws_message(self.ex.hit(bid.order_id)))
        e.run_cycle()
        self.assertEqual(e.risk.net(T), 2)
        self.assertEqual({o["side"] for o in self.ex.resting(T)}, {"ask"})

    def test_no_orders_until_the_fill_stream_is_up(self):
        e = self.make_engine()
        self.assertTrue(e._prepare_live()[0])
        self.watch(e)
        e.run_cycle()
        self.assertEqual(self.ex.writes(), [])

    def test_quotes_are_pulled_when_the_fill_stream_drops(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.assertEqual(len(self.ex.resting(T)), 2)
        e.user_stream.disconnected()
        e.run_cycle()
        self.assertEqual(self.ex.resting(T), [])
        self.assertIn("fill", e.last_error)

    def test_no_takes_while_the_fill_stream_is_down(self):
        self.price_model(70.0)
        e = self.ready(self.make_engine())
        self.watch(e)
        e.user_stream.disconnected()
        e.run_cycle()
        self.assertEqual(self.ex.positions, {})

    def test_after_reconnecting_it_catches_up_then_trades_again(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        e.user_stream.disconnected()
        fill = self.ex.hit(self.ex.resting(T)[0]["order_id"])     # happens while the stream is down
        e.user_stream.handle(SUBSCRIBED)
        e.run_cycle()
        self.assertEqual(self.ex.resting(T), [])                  # not caught up yet
        e._live_tick()
        self.assertTrue(e.user_stream.healthy)
        self.assertEqual(abs(e.risk.net(T)), float(fill["count_fp"]))
        e.run_cycle()
        self.assertTrue(self.ex.resting(T))

    def test_stream_problem_clears_from_the_status_when_it_recovers(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.user_stream.disconnected()
        e.run_cycle()
        e.user_stream.handle(SUBSCRIBED)
        e._live_tick()
        e.run_cycle()
        self.assertIsNone(e.last_error)

    def test_no_orders_on_a_market_past_its_close(self):
        # Every one would be rejected, and five rejections halt the bot.
        e = self.ready(self.make_engine())
        self.watch(e, hours_to_close=-0.1)
        e.run_cycle()
        self.assertEqual(self.ex.writes(), [])
        self.assertEqual(e.maker.orders_failed_in_a_row, 0)

    def test_quotes_are_pulled_when_a_market_closes(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.assertEqual(len(self.ex.resting(T)), 2)
        self.watch(e, hours_to_close=-0.1)
        e.run_cycle()
        self.assertEqual(self.ex.resting(T), [])

    def test_a_fill_in_a_kind_of_market_the_bot_doesnt_trade_isnt_counted(self):
        e = self.ready(self.make_engine())
        self.ex.shards[0] = 5000
        self.ex.set_book("FED-26DEC-T3", 40, 60)
        real_client(self.ex).create_order("FED-26DEC-T3", "buy", 2, 60, "immediate_or_cancel")
        e.user_stream.handle(self.ex.ws_message(self.ex.fills[-1]))
        e.run_cycle()
        self.assertEqual(e.risk.net("FED-26DEC-T3"), 0)

    def test_live_refuses_to_watch_other_kinds_of_market(self):
        e = self.make_engine()
        ok, why = e.add_ticker("FED-26DEC-T3")
        self.assertFalse(ok)
        self.assertNotIn("FED-26DEC-T3", e.cfg.watched_tickers)

    def test_live_still_watches_the_btc_ladder(self):
        e = self.make_engine()
        self.assertTrue(e.add_ticker(T)[0])

    def test_market_stream_doesnt_keep_the_trade_tape_nobody_reads(self):
        # Paper fills drain it. Live, it would grow until the 909 MB box ran out.
        self.ready(self.make_engine())._start_market_stream()
        self.assertEqual(engine.feeds.KalshiStream.call_args.kwargs["keep_trades"], False)


class PositionCheckTests(EngineLiveCase):
    def test_matching_positions_leave_the_bot_alone(self):
        self.price_model(70.0)
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.check_positions(e)
        self.check_positions(e)
        self.assertFalse(e.risk.halted)

    def test_a_fill_the_stream_missed_is_found_and_booked(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.ex.hit(self.ex.resting(T)[0]["order_id"])
        self.check_positions(e)
        self.assertEqual(abs(e.risk.net(T)), 2)
        self.check_positions(e)
        self.assertFalse(e.risk.halted)

    def test_one_mismatch_can_be_timing_and_doesnt_halt(self):
        e = self.ready(self.make_engine())
        self.ex.positions[T] = 7.0
        self.check_positions(e)
        self.assertFalse(e.risk.halted)

    def test_a_mismatch_that_clears_is_forgotten(self):
        e = self.ready(self.make_engine())
        self.ex.positions[T] = 7.0
        self.check_positions(e)
        del self.ex.positions[T]
        self.check_positions(e)
        self.ex.positions[T] = 7.0
        self.check_positions(e)
        self.assertFalse(e.risk.halted)

    def test_the_same_mismatch_twice_halts_and_names_it(self):
        e = self.ready(self.make_engine())
        self.ex.positions[T] = 7.0
        self.check_positions(e)
        self.check_positions(e)
        self.assertTrue(e.risk.halted)
        self.assertIn(T, e.risk.halt_reason)

    def test_a_position_only_the_bot_thinks_it_has_counts_too(self):
        e = self.ready(self.make_engine())
        e.risk.record_fill(T, "yes", "buy", 2, 41)
        self.check_positions(e)
        self.check_positions(e)
        self.assertTrue(e.risk.halted)

    def test_positions_on_other_kinds_of_market_are_not_the_bots_business(self):
        e = self.ready(self.make_engine())
        self.ex.positions["FED-26DEC-T3"] = 7.0
        self.check_positions(e)
        self.check_positions(e)
        self.assertFalse(e.risk.halted)

    def test_a_closed_market_waiting_to_settle_is_not_compared(self):
        # Kalshi and the bot don't clear it at the same instant.
        e = self.ready(self.make_engine())
        e.risk.record_fill(T, "yes", "buy", 2, 41)
        self.watch(e, hours_to_close=-0.5)
        self.check_positions(e)
        self.check_positions(e)
        self.assertFalse(e.risk.halted)

    def test_an_order_the_bot_isnt_tracking_is_cancelled(self):
        # e.g. the answer to a quote was lost, so nothing would ever replace it.
        e = self.ready(self.make_engine())
        real_client(self.ex).create_order(T, "buy", 2, 30, post_only=True)
        self.check_positions(e)
        self.assertEqual(self.ex.resting(T), [])

    def test_quotes_the_bot_is_tracking_stay(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.check_positions(e)
        self.assertEqual(len(self.ex.resting(T)), 2)

    def test_a_quote_that_disappeared_from_kalshi_stops_being_tracked(self):
        # It expired, or Kalshi cancelled it: the bot should quote again.
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        q = e.maker.resting_quotes[T][0]
        q.placed_at -= 60
        real_client(self.ex).cancel_order(q.order_id, T)
        self.check_positions(e)
        self.assertNotIn(q.order_id, [x.order_id for x in e.maker.resting_quotes[T]])

    def test_a_quote_too_new_for_a_lagging_list_stays_tracked(self):
        # On 2026-11-04 Kalshi's list didn't show an order placed a moment
        # earlier. Dropping it would leave it up, with a second one beside it.
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        q = e.maker.resting_quotes[T][0]
        self.ex.order_list_lag_seconds = 2.0
        self.check_positions(e)
        self.assertIn(q.order_id, [x.order_id for x in e.maker.resting_quotes[T]])

    def test_an_order_just_cancelled_but_still_listed_causes_no_trouble(self):
        # The same lag the other way: the list still shows it, so the bot
        # cancels it again and Kalshi says it's gone.
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.ex.order_list_lag_seconds = 2.0
        for q in e.maker.resting_quotes[T]:
            q.placed_at -= 60
        e.maker._cancel_resting(T, "moving")
        self.check_positions(e)
        self.assertFalse(e.risk.halted)
        self.assertEqual(self.ex.resting(T), [])
        self.assertEqual(e.maker.orders_failed_in_a_row, 0)

    def test_a_quote_placed_after_the_list_was_read_stays_tracked(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        q = e.maker.resting_quotes[T][0]
        q.placed_at = time.time() + 60          # "newer than the list"
        real_client(self.ex).cancel_order(q.order_id, T)
        self.check_positions(e)
        self.assertIn(q.order_id, [x.order_id for x in e.maker.resting_quotes[T]])

    def test_not_being_able_to_check_for_a_minute_halts(self):
        e = self.ready(self.make_engine())
        self.ex.read_failures["/portfolio/positions"] = requests.ConnectionError("down")
        for _ in range(4):
            self.check_positions(e)
        self.assertFalse(e.risk.halted)
        self.check_positions(e)
        self.assertTrue(e.risk.halted)

    def test_the_balance_stays_current(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        self.ex.balance = 1234
        e._last_balance = 0
        e._live_tick()
        self.assertEqual(e.maker.available_cents, 1234)

    def test_a_failed_balance_read_keeps_the_last_one(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        self.ex.read_failures["/portfolio/balance"] = requests.ConnectionError("down")
        e._last_balance = 0
        e._live_tick()
        self.assertEqual(e.maker.available_cents, 5000)


class ExchangeFundsTests(EngineLiveCase):
    """Kalshi checks funds on each market's own exchange, and API orders don't
    move money between them. The balance it reports is the total."""

    def test_it_can_spend_whats_on_the_markets_exchange_not_the_total(self):
        self.ex.shards.update({0: 4924, 2: 1000})
        e = self.ready(self.make_engine())
        self.watch(e)
        self.assertEqual(e.maker.available_cents, 1000)
        self.assertAlmostEqual(e.live_account["balance_usd"], 59.24)
        self.assertAlmostEqual(e.live_account["spendable_usd"], 10.00)

    def test_an_order_only_the_total_could_pay_for_is_not_sent(self):
        self.ex.shards.update({0: 4924, 2: 50})     # bid 2 @ 41 needs 82¢ and a fee
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        self.assertEqual(self.ex.writes(), [])
        self.assertEqual(e.maker.orders_failed_in_a_row, 0)

    def test_no_orders_until_it_knows_the_markets_exchange(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        del e.market_info[T]["exchange_index"]
        e._refresh_balance()
        e.run_cycle()
        self.assertEqual(self.ex.writes(), [])

    def test_with_no_breakdown_it_uses_the_total(self):
        self.ex.hide_breakdown = True
        e = self.ready(self.make_engine())
        self.watch(e)
        self.assertEqual(e.maker.available_cents, 5000)

    def test_markets_on_two_exchanges_use_the_smaller_amount(self):
        self.ex.shards.update({0: 300, 2: 1000})
        e = self.ready(self.make_engine())
        self.watch(e)
        e.cfg.watched_tickers.append("OTHER")
        e.market_info["OTHER"] = {"ticker": "OTHER", "close_time": iso(6), "exchange_index": 0}
        e._refresh_balance()
        self.assertEqual(e.maker.available_cents, 300)

    def test_the_dashboard_state_shows_it(self):
        self.ex.shards.update({0: 4924, 2: 0})
        e = self.make_engine()
        self.watch(e)
        self.assertEqual(e.snapshot()["live"]["account"]["spendable_usd"], 0)


class PullingEverythingTests(EngineLiveCase):
    def everything_up(self):
        e = self.ready(self.make_engine())
        self.watch(e)
        e.run_cycle()
        real_client(self.ex).create_order(T, "buy", 1, 20, post_only=True)    # one it doesn't know about
        self.assertEqual(len(self.ex.resting(T)), 3)
        return e

    def test_a_halt_clears_kalshi_not_just_what_the_bot_tracks(self):
        e = self.everything_up()
        e.risk._halt("test")
        e._loop()
        self.assertEqual(self.ex.resting(T), [])
        self.assertTrue(engine.HALT_FILE.exists())

    def test_the_kill_switch_clears_kalshi(self):
        e = self.everything_up()
        e.running = True
        e.kill()
        self.assertEqual(self.ex.resting(T), [])

    def test_the_kill_switch_clears_kalshi_when_stopped(self):
        e = self.everything_up()
        e.running = False
        e.kill()
        self.assertEqual(self.ex.resting(T), [])

    def test_stopping_clears_kalshi(self):
        e = self.everything_up()
        e.running = True
        e.stop()
        self.assertEqual(self.ex.resting(T), [])


class RequoteTests(LiveCase):
    def setUp(self):
        super().setUp()
        self.maker = self.make_maker()
        self.evaluate(self.maker)              # bid 2 @ 41, ask 2 @ 59
        self.ex.requests.clear()

    def bid(self):
        return next(o for o in self.ex.resting(T) if o["side"] == "bid")

    def test_a_one_cent_move_leaves_the_quote_alone(self):
        # Chasing every cent was 5 replacements a second, 56 in the worst second.
        self.price_model(48.4)                 # the bid would be 40
        self.evaluate(self.maker)
        self.assertEqual(self.ex.writes(), [])
        self.assertEqual(self.bid()["price"], 41)

    def test_a_two_cent_move_replaces_it(self):
        self.price_model(47.4)                 # the bid would be 39
        self.evaluate(self.maker)
        self.assertEqual(self.bid()["price"], 39)

    def test_an_old_quote_is_replaced_even_at_the_same_price(self):
        old = self.bid()["order_id"]
        for q in self.maker.resting_quotes[T]:
            q.placed_at -= 16
        self.evaluate(self.maker)
        self.assertNotEqual(self.bid()["order_id"], old)

    def test_a_quote_no_longer_wanted_goes_at_once(self):
        self.risk.reduce_only[T] = "test"      # flat and reduce-only: no sides allowed
        self.evaluate(self.maker)
        self.assertEqual(self.ex.resting(T), [])

    def test_paper_still_follows_every_cent(self):
        cfg = small_limits()
        paper = MarketMaker(real_client(self.ex), RiskManager(cfg),
                            BotConfig(kalshi=KalshiConfig(paper_trading=True), risk=cfg, watched_tickers=[]))
        self.price_model(50.0)
        paper.evaluate_market(T, {"ticker": T}, book())
        self.price_model(48.4)
        paper.evaluate_market(T, {"ticker": T}, book())
        bid = next(q for q in paper.resting_quotes[T] if q.action == "buy")
        self.assertEqual(bid.price_cents, 40)


class WriteBudgetTests(LiveCase):
    def make_maker(self, **limits):
        maker = super().make_maker(**limits)
        maker.budget = WriteBudget()
        return maker

    def test_it_refills_at_its_rate_up_to_one_second_of_budget(self):
        now = [0.0]
        budget = WriteBudget(rate=80, clock=lambda: now[0])
        self.assertTrue(budget.spend(80))
        self.assertFalse(budget.spend(1))
        now[0] += 0.5
        self.assertTrue(budget.spend(40))
        self.assertFalse(budget.spend(1))
        now[0] += 100
        self.assertTrue(budget.spend(80))
        self.assertFalse(budget.spend(1))

    def test_a_take_can_use_what_a_quote_cant(self):
        self.price_model(70.0)
        maker = self.make_maker()
        maker.budget.tokens = 35               # enough for one order, not while keeping 30 back
        self.evaluate(maker)
        self.assertEqual(self.ex.positions[T], 2.0)
        self.assertEqual(self.ex.resting(T), [])

    def test_with_nothing_left_not_even_a_take_goes_out(self):
        self.price_model(70.0)
        maker = self.make_maker()
        maker.budget.tokens = 5
        maker.budget.rate = 0
        self.evaluate(maker)
        self.assertEqual(self.ex.writes(), [])
        self.assertFalse(self.risk.halted)     # waiting isn't failing

    def test_a_quote_at_the_wrong_price_is_cancelled_even_with_no_budget(self):
        # Leaving it up is the expensive choice: it's about to be picked off.
        maker = self.make_maker()
        self.evaluate(maker)
        maker.budget.tokens = 0
        maker.budget.rate = 0
        self.price_model(44.0)                 # the bid should fall from 41 to 36
        self.evaluate(maker)
        self.assertEqual([o for o in self.ex.resting(T) if o["side"] == "bid"], [])

    def test_a_quote_that_is_only_old_waits_for_budget(self):
        maker = self.make_maker()
        self.evaluate(maker)
        before = {o["order_id"] for o in self.ex.resting(T)}
        for q in maker.resting_quotes[T]:
            q.placed_at -= 16
        maker.budget.tokens = 0
        maker.budget.rate = 0
        self.evaluate(maker)
        self.assertEqual({o["order_id"] for o in self.ex.resting(T)}, before)

    def test_a_fast_market_stays_inside_kalshis_limit(self):
        # Kalshi's Basic write bucket: refills 100 tokens a second, holds at most
        # 100. An order costs 10, a cancel 2. Below zero means a 429.
        now = [1000.0]
        maker = self.make_maker()
        maker.budget = WriteBudget(clock=lambda: now[0])
        tickers = [f"KXBTCD-26NOV0517-T{80000 + 250 * i}.00" for i in range(6)]
        for t in tickers:
            self.ex.set_book(t, 40, 60)
        bucket, lowest, last_second_writes = 100.0, 100.0, 0
        for tick in range(120):                   # 6 seconds: a new price every 50ms, on every strike
            self.price_model(50.0 + (3 if tick % 2 else -3))
            for t in tickers:
                self.ex.requests.clear()
                self.evaluate(maker, t)
                for method, *_ in self.ex.requests:
                    bucket -= {"POST": 10, "DELETE": 2}.get(method, 0)
                    last_second_writes += method != "GET" and tick >= 100
                lowest = min(lowest, bucket)
            now[0] += 0.05
            bucket = min(100.0, bucket + 5.0)
        self.assertGreaterEqual(lowest, 0)
        self.assertGreater(last_second_writes, 0)  # and it does keep quoting


class LiveLimitsTests(LiveCase):
    def test_live_trades_2_contracts_by_default(self):
        self.price_model(70.0)
        cfg = BotConfig(kalshi=KalshiConfig(paper_trading=False), watched_tickers=[])
        maker = MarketMaker(real_client(self.ex), RiskManager(cfg.risk), cfg)
        maker.available_cents = self.ex.balance
        self.evaluate(maker)
        self.assertEqual(self.ex.positions[T], 2.0)
        self.assertEqual([o["count"] for o in self.ex.resting(T)], [2.0])

    def test_every_live_limit_fits_inside_a_50_dollar_account(self):
        r = BotConfig(kalshi=KalshiConfig(paper_trading=False), watched_tickers=[]).risk
        self.assertLess(r.max_total_exposure_usd, 50)
        self.assertLess(r.max_daily_loss_usd, 50)
        self.assertLess(r.max_single_market_loss_usd, r.max_daily_loss_usd)
        self.assertLessEqual(r.max_position_per_market * 1.00, r.max_total_exposure_usd)

    def test_exposure_limit_stops_new_positions(self):
        cfg = live_risk()
        risk = RiskManager(cfg)
        for i in range(5):
            risk.record_fill(f"KXBTCD-26NOV0517-T{i}", "yes", "buy", cfg.max_position_per_market, 80)
        self.assertGreaterEqual(risk.total_exposure_usd(), cfg.max_total_exposure_usd)
        self.assertEqual(risk.allowed_sides(T)[0], set())

    def test_paper_keeps_its_own_sizes(self):
        cfg = BotConfig(kalshi=KalshiConfig(paper_trading=True), watched_tickers=[])
        self.assertEqual(cfg.risk, RiskConfig())
        self.assertEqual(cfg.risk.take_size, 15)


class LiveDashboardTests(unittest.TestCase):
    """The real dashboard module, with a stand-in for the trading engine."""

    NOW = time.time()
    PAPER = {"ts": NOW, "ticker": T2, "side": "yes", "action": "buy", "count": 40, "price_cents": 50, "mode": "paper"}
    BUY = {"ts": NOW, "ticker": T, "side": "yes", "action": "buy", "count": 2, "price_cents": 41, "mode": "live",
           "liquidity": "maker", "fee_cents": 1}
    SELL = {"ts": NOW + 1, "ticker": T, "side": "yes", "action": "sell", "count": 2, "price_cents": 10, "mode": "live",
            "liquidity": "maker", "fee_cents": 0}

    @classmethod
    def setUpClass(cls):
        fake_engine = mock.MagicMock(books={}, market_info={})
        fake_engine.client.get_markets_batch.return_value = []
        argv, handlers = sys.argv, list(logging.getLogger().handlers)
        sys.modules.pop("dashboard", None)
        with mock.patch("engine.Engine", return_value=fake_engine), mock.patch.object(sys, "argv", ["test"]):
            import dashboard
        logging.getLogger().handlers[:] = handlers
        sys.argv = argv
        cls.dashboard, cls.engine = dashboard, fake_engine
        cls.client = dashboard.app.test_client()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("dashboard", None)

    def setUp(self):
        self.go_live()
        self.engine.live_account = {"balance_usd": 41.50, "portfolio_value_usd": 6.25, "updated": 0}
        self.dashboard._cache["pnl"] = (0.0, None)
        patcher = mock.patch("ledger.read_all_fills", return_value=[self.PAPER, self.BUY, self.SELL])
        patcher.start()
        self.addCleanup(patcher.stop)

    def go_live(self, live=True):
        patcher = mock.patch.object(self.dashboard.CONFIG.kalshi, "paper_trading", not live)
        patcher.start()
        self.addCleanup(patcher.stop)

    def summary(self):
        return self.dashboard._pnl_summary()

    def test_live_lists_only_live_fills(self):
        fills = self.client.get("/api/fills").get_json()["fills"]
        self.assertEqual({f["mode"] for f in fills}, {"live"})

    def test_paper_lists_only_paper_fills(self):
        self.go_live(False)
        fills = self.client.get("/api/fills").get_json()["fills"]
        self.assertEqual({f["mode"] for f in fills}, {"paper"})

    def test_a_market_chart_shows_only_fills_from_this_mode(self):
        with mock.patch.object(self.dashboard.price_bars, "rows", return_value=[]):
            fills = self.client.get(f"/api/history/{T2}").get_json()["fills"]
        self.assertEqual(fills, [])

    def test_money_is_what_kalshi_says(self):
        s = self.summary()
        self.assertEqual(s["balance_usd"], 41.50)
        self.assertEqual(s["equity_usd"], 47.75)
        self.assertEqual(s["starting_balance_usd"], 50.0)

    def test_before_kalshi_answers_money_comes_from_the_live_fills(self):
        self.engine.live_account = None
        s = self.summary()
        self.assertAlmostEqual(s["equity_usd"], 49.37)    # $50, down 62¢, 1¢ fee

    def test_pnl_counts_only_live_fills_with_kalshis_fees(self):
        s = self.summary()
        self.assertAlmostEqual(s["net_usd"], -0.63)
        self.assertEqual(s["fill_count"], 2)

    def test_todays_pnl_measures_from_the_live_start(self):
        self.assertAlmostEqual(self.summary()["today_usd"], -0.63)

    def test_paper_still_starts_from_its_500(self):
        self.go_live(False)
        s = self.summary()
        self.assertEqual(s["starting_balance_usd"], 500.0)

    def test_the_terminal_report_also_keeps_to_this_mode(self):
        out = io.StringIO()
        with mock.patch.object(show_pnl.CONFIG.kalshi, "paper_trading", False), mock.patch("show_pnl.build_client"), \
                contextlib.redirect_stdout(out):
            show_pnl.main()
        self.assertIn("$50.00", out.getvalue())
        self.assertNotIn(T2, out.getvalue())


class LiveCheckTests(unittest.TestCase):
    """live_check.py, run against the fake exchange."""
    DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
    REAL = "https://external-api.kalshi.com/trade-api/v2"

    def setUp(self):
        self.ex = FakeExchange()
        self.ex.set_book(T, 40, 60)
        self.ex.markets[T] = {"price_level_structure": "linear_cent", "close_time": iso(6)}
        self.answer = "yes"
        self.subscribes = True

    def stream(self, _auth, _url):
        stream = feeds.UserStream(FakeAuth(), "wss://fake")
        if self.subscribes:
            stream.handle(SUBSCRIBED)
        self.ex.on_fill = lambda row: stream.handle(self.ex.ws_message(row))
        return stream

    def run_check(self, *args, base_url=DEMO, client=None, wait_seconds=0.05):
        out = io.StringIO()
        if client is None:
            client = KalshiClient(base_url, FakeAuth())
            client._session = self.ex
        with mock.patch.object(KalshiClient, "MAX_REQUESTS_PER_SECOND", 1_000_000):
            code = live_check.run(list(args), client, ws_url="wss://fake", stream_factory=self.stream,
                                  confirm=lambda _prompt: self.answer, out=out, wait_seconds=wait_seconds)
        self.output = out.getvalue()
        return code

    def test_with_no_options_it_sends_nothing(self):
        self.assertEqual(self.run_check(base_url=self.REAL), 0)
        self.assertEqual(self.ex.writes(), [])

    def test_money_on_another_exchange_fails_and_says_where_to_move_it(self):
        self.ex.shards.update({0: 4924, 2: 0})
        self.assertEqual(self.run_check(), 1)
        self.assertIn("exchange 2", self.output)
        self.assertIn("exchange-indexes", self.output)
        self.assertEqual(self.ex.writes(), [])

    def test_it_doesnt_try_orders_the_exchange_cant_pay_for(self):
        self.ex.shards.update({0: 4924, 2: 0})
        self.assertEqual(self.run_check("--order", "--take"), 1)
        self.assertEqual(self.ex.writes(), [])

    def test_a_fill_stream_that_never_subscribes_is_a_failure(self):
        self.subscribes = False
        self.assertEqual(self.run_check(), 1)

    def test_no_api_key_is_a_failure_not_a_crash(self):
        client = KalshiClient(self.DEMO, None)
        client._session = self.ex
        self.assertEqual(self.run_check(client=client), 1)

    def test_a_cancel_costing_more_than_the_bot_assumes_is_a_failure(self):
        self.ex.endpoint_costs = [{"method": "DELETE", "path": "/trade-api/v2/portfolio/events/orders/:order_id", "cost": 10}]
        self.assertEqual(self.run_check(), 1)

    def test_the_order_check_passes_and_leaves_nothing_behind(self):
        self.assertEqual(self.run_check("--order"), 0, self.output)
        self.assertEqual(self.ex.resting(), [])
        self.assertEqual(self.ex.positions, {})
        self.assertEqual(self.ex.balance, 5000)

    def test_an_order_list_that_lags_isnt_a_failure(self):
        # What the real order list did on 2026-11-04: an order placed a moment
        # ago wasn't on it, and one cancelled a moment ago still was.
        self.ex.order_list_lag_seconds = 0.1
        self.assertEqual(self.run_check("--order", wait_seconds=2.0), 0, self.output)
        self.assertEqual(self.ex.resting(), [])

    def test_an_order_that_never_shows_up_is_a_failure(self):
        self.ex.order_list_lag_seconds = 100
        self.assertEqual(self.run_check("--order"), 1)

    def test_it_says_when_resting_orders_hold_money(self):
        self.ex.resting_holds_funds = True
        self.assertEqual(self.run_check("--order"), 0, self.output)
        self.assertIn("resting orders hold money", self.output)

    def test_it_says_when_they_dont(self):
        self.assertEqual(self.run_check("--order"), 0, self.output)
        self.assertIn("resting orders do not hold money", self.output)

    def test_the_order_check_never_bids_near_the_market(self):
        # 2026-11-04: it bid at an ask it had read 8 seconds earlier. By then that
        # didn't cross, so the bid rested, someone sold to it, and "Every check
        # passed" left the user holding a contract.
        self.assertEqual(self.run_check("--order"), 0, self.output)
        orders = [r[3] for r in self.ex.requests if r[0] == "POST"]
        self.assertEqual([(o["price"], o["count"]) for o in orders], [("0.0100", "1.00")])

    def test_the_order_check_says_so_if_it_leaves_a_position(self):
        real = self.ex._cancel

        def someone_sold_to_it_first(order_id, params):
            self.ex.hit(order_id)
            return real(order_id, params)
        self.ex._cancel = someone_sold_to_it_first
        self.assertEqual(self.run_check("--order"), 1)
        self.assertEqual(self.ex.positions, {T: 1.0})
        self.assertIn("close it on Kalshi", self.output)

    def test_the_order_check_doesnt_blame_a_position_that_was_already_there(self):
        self.ex.positions[T] = 3.0
        self.assertEqual(self.run_check("--order"), 0, self.output)

    def test_the_order_check_leaves_the_bots_own_orders_alone(self):
        client = KalshiClient(self.DEMO, FakeAuth())
        client._session = self.ex
        client.create_order(T, "buy", 2, 30, post_only=True)
        self.assertEqual(self.run_check("--order"), 0, self.output)
        self.assertEqual(len(self.ex.resting(T)), 1)

    def test_the_order_check_notices_a_cancel_that_didnt_happen(self):
        real_cancel = self.ex._cancel
        self.ex._cancel = lambda order_id, params: FakeResponse(200, {"order_id": order_id, "client_order_id": "",
                                                                      "reduced_by": "0.00", "ts_ms": 1})
        self.addCleanup(setattr, self.ex, "_cancel", real_cancel)
        self.assertEqual(self.run_check("--order"), 1)

    def test_the_order_check_notices_an_answer_in_another_shape(self):
        real = self.ex._post_order

        def old_shape(body):
            return FakeResponse(201, {"order": json.loads(real(body).text)})
        self.ex._post_order = old_shape
        self.assertEqual(self.run_check("--order"), 1)
        self.assertEqual(self.ex.resting(), [])     # it still cleans up after itself

    def test_the_take_check_passes_and_ends_flat(self):
        self.assertEqual(self.run_check("--take"), 0, self.output)
        self.assertEqual(self.ex.positions, {})
        self.assertEqual(len(self.ex.fills), 2)

    def test_a_contract_it_couldnt_sell_back_is_reported(self):
        real = self.ex._post_order

        def no_bids_after_the_buy(body):
            if body["side"] == "ask":
                self.ex.set_book(T, None, 60)
            return real(body)
        self.ex._post_order = no_bids_after_the_buy
        self.assertEqual(self.run_check("--take"), 1)
        self.assertEqual(self.ex.positions, {T: 1.0})
        self.assertIn(T, self.output)

    def test_a_fill_the_stream_never_delivers_is_a_failure(self):
        real_stream = self.stream

        def silent(auth, url):
            stream = real_stream(auth, url)
            self.ex.on_fill = None
            return stream
        self.stream = silent
        self.assertEqual(self.run_check("--take"), 1)

    def test_a_price_answered_on_the_no_side_is_a_failure(self):
        # The bot books a sell at the answer's average_fill_price, so it has to be the YES price.
        real = self.ex._post_order

        def no_price_on_sells(body):
            answer = real(body)
            if body["side"] == "ask" and answer.status_code == 201:
                data = answer.json()
                if "average_fill_price" in data:
                    data["average_fill_price"] = f"{1 - float(data['average_fill_price']):.4f}"
                    return FakeResponse(201, data)
            return answer
        self.ex._post_order = no_price_on_sells
        self.assertEqual(self.run_check("--take"), 1)

    def test_real_orders_need_the_real_money_flag(self):
        self.assertEqual(self.run_check("--order", base_url=self.REAL), 2)
        self.assertEqual(self.ex.writes(), [])

    def test_the_real_money_flag_still_asks_first(self):
        self.answer = "no"
        self.assertEqual(self.run_check("--take", "--real-money", base_url=self.REAL), 2)
        self.assertEqual(self.ex.writes(), [])

    def test_real_money_goes_ahead_on_yes(self):
        self.assertEqual(self.run_check("--take", "--real-money", base_url=self.REAL), 0, self.output)
        self.assertEqual(self.ex.positions, {})

    def test_demo_needs_neither(self):
        self.answer = "no"
        self.assertEqual(self.run_check("--order"), 0, self.output)


class PaperIsUntouchedTests(LiveCase):
    def test_paper_mode_sends_nothing_and_writes_paper_rows(self):
        cfg = small_limits()
        client = real_client(self.ex)
        maker = MarketMaker(client, RiskManager(cfg),
                            BotConfig(kalshi=KalshiConfig(paper_trading=True), risk=cfg, watched_tickers=[]))
        self.price_model(70.0)
        maker.evaluate_market(T, {"ticker": T}, book())
        self.assertEqual(self.ex.requests, [])
        row = ledger.read_all_fills()[0]
        self.assertEqual(row["mode"], "paper")
        self.assertNotIn("fee_cents", row)


if __name__ == "__main__":
    unittest.main()
