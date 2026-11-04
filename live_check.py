"""
Tries the bot's live path against Kalshi's real API, and says what's true.
test_live.py proves the bot follows Kalshi's docs as read on 2026-11-04. This
checks the docs against what the servers actually answer, before the bot trusts
them with money. It uses the key and endpoints in .env, and doesn't need the
bot to be stopped or KALSHI_PAPER_TRADING changed.

Read-only. Sends no orders:

    python live_check.py

Kalshi checks funds on each market's own exchange, and API orders don't move
money between exchanges. So the money has to be on the exchange BTC trades on
(this says which) before any order can work. The order checks don't try until
it is. Move it at https://kalshi.com/account/exchange-indexes.

Orders. Against Kalshi's demo environment these use mock funds. To use the real
one, add --real-money, and it asks first:

    python live_check.py --order    One contract bid at 1¢, cancelled straight
                                    away. Then a bid at the ask with post-only
                                    (it should not fill) and one too big to pay
                                    for (it should be refused). Costs nothing
                                    unless Kalshi ignores post-only: at most
                                    one contract at the ask.

    python live_check.py --take     Buys one contract and sells it straight
                                    back, as the bot's takes do: the spread and
                                    two taker fees, about 5¢ near the money.
                                    If it can't sell back it says so, and you
                                    hold one contract.

Exit code: 0 everything passed, 1 something failed, 2 refused to run.
"""
import argparse
import json
import sys
import time
import uuid

import feeds
from config import CONFIG
from kalshi_client import WriteBudget, build_client, top_of_book
from market_maker import QUOTE_EXPIRY_SECONDS

SERIES = "KXBTCD"
MOVE_MONEY_AT = "https://kalshi.com/account/exchange-indexes"
ID_PREFIX = "live-check-"     # so this only ever cleans up its own orders, never the bot's
ANSWER_FIELDS = ("order_id", "fill_count", "remaining_count", "ts_ms")


class Report:
    def __init__(self, out):
        self.out = out
        self.failures = 0

    def say(self, text=""):
        self.out.write(text + "\n")

    def ok(self, text):
        self.say(f"  ok    {text}")

    def fail(self, text):
        self.failures += 1
        self.say(f"  FAIL  {text}")

    def learned(self, text):
        self.say(f"  note  {text}")

    def check(self, passed, text, detail=""):
        if passed:
            self.ok(text)
        else:
            self.fail(f"{text}{': ' + detail if detail else ''}")
        return passed


def _error_text(e):
    response = getattr(e, "response", None)
    return f"HTTP {response.status_code}: {response.text[:300]}" if response is not None else str(e)


def _order_id():
    return f"{ID_PREFIX}{uuid.uuid4()}"


def _attempt(r, text, fn):
    """fn()'s result, or None with a failure reported."""
    try:
        result = fn()
    except Exception as e:
        r.fail(f"{text}: {_error_text(e)}")
        return None
    r.ok(text)
    return result


# ---- read-only -----------------------------------------------------------------

def check_reads(r, client):
    r.say("Reading the account")
    balance = _attempt(r, "balance", client.get_balance)
    if balance is not None:
        r.check("balance" in balance and "portfolio_value" in balance, "balance has the fields the bot reads", json.dumps(balance))
        if "balance" in balance:
            r.learned(f"{balance['balance'] / 100:.2f} available, {balance.get('portfolio_value', 0) / 100:.2f} in positions")
    positions = _attempt(r, "positions", client.get_positions)
    if positions is not None:
        r.learned(f"{len(positions)} open position(s)")
    orders = _attempt(r, "resting orders", lambda: client.get_orders(status="resting")["orders"])
    if orders is not None:
        r.learned(f"{len(orders)} resting order(s)")
    fills = _attempt(r, "fills from the last 30 days, all readable", lambda: client.get_fills(min_ts=time.time() - 30 * 86400))
    if fills is not None:
        r.learned(f"{len(fills)} fill(s)")
    costs = _attempt(r, "what orders and cancels cost", client.get_endpoint_costs)
    if costs is not None:
        check_costs(r, costs)


def check_costs(r, costs):
    default = costs.get("default_cost")
    listed = {(c["method"], c["path"]): c["cost"] for c in costs.get("endpoint_costs", [])}
    order = listed.get(("POST", "/trade-api/v2/portfolio/events/orders"), default)
    cancel = listed.get(("DELETE", "/trade-api/v2/portfolio/events/orders/:order_id"), default)
    r.check(order == WriteBudget.ORDER, f"an order costs {order} tokens, as the bot assumes", f"WriteBudget.ORDER is {WriteBudget.ORDER}")
    r.check(cancel == WriteBudget.CANCEL, f"a cancel costs {cancel} tokens, as the bot assumes", f"WriteBudget.CANCEL is {WriteBudget.CANCEL}")


def check_fill_stream(r, auth, ws_url, stream_factory, wait_seconds):
    r.say("Fill stream")
    if not auth:
        r.fail("no API key")
        return None
    stream = stream_factory(auth, ws_url)
    deadline = time.time() + wait_seconds
    while not stream.subscribed and time.time() < deadline:
        time.sleep(0.05)
    r.check(stream.subscribed, "subscribed to the fill channel", f"no answer in {wait_seconds:g}s from {ws_url}")
    return stream


def pick_market(r, client):
    """The open market nearest 50¢ with both sides quoted, which is where the
    bot trades, or else any open market."""
    r.say("Finding a market")
    markets = _attempt(r, f"open {SERIES} markets", lambda: client.get_markets(series_ticker=SERIES, limit=200)["markets"])
    if not markets:
        r.fail(f"no open {SERIES} markets")
        return None

    def nearness(m):
        book = top_of_book(m)
        if book["yes_bid"] is None or book["yes_ask"] is None:
            return 1000
        return abs((book["yes_bid"] + book["yes_ask"]) / 2 - 50)
    market = min(markets, key=nearness)
    book = top_of_book(market)
    r.learned(f"{market['ticker']}: bid {book['yes_bid']}¢, ask {book['yes_ask']}¢, "
              f"prices {market.get('price_level_structure', 'unknown')}")
    r.check(market.get("price_level_structure") in (None, "linear_cent"), "prices move in whole cents, as the bot assumes",
            str(market.get("price_ranges")))
    return market


def check_funds(r, client, market) -> bool:
    """Is there money on the exchange this market trades on?"""
    r.say("Money on the right exchange")
    exchange, balance = market.get("exchange_index"), client.get_balance()
    rows = balance.get("balance_breakdown")
    if rows is None:
        r.learned("Kalshi gave no breakdown by exchange for this key")
        return True
    on_exchange = {row["exchange_index"]: float(row["balance"]) for row in rows}
    r.learned("by exchange: " + ", ".join(f"{i}: ${v:.2f}" for i, v in sorted(on_exchange.items())))
    here = on_exchange.get(exchange, 0)
    return r.check(here > 0, f"{SERIES} trades on exchange {exchange}, where you have ${here:.2f}",
                   f"move money there at {MOVE_MONEY_AT} (API orders don't move it for you, the way Kalshi's app does)")


# ---- resting orders ------------------------------------------------------------

def check_answer(r, answer):
    """The fields the bot reads from an order's answer."""
    missing = [k for k in ANSWER_FIELDS if k not in answer]
    return r.check(not missing, "the answer has the fields the bot reads", f"missing {missing}: {json.dumps(answer)[:300]}")


def check_order(r, client, market):
    ticker = market["ticker"]
    r.say(f"Resting order on {ticker}")
    before = client.get_balance()["balance"]
    answer = _attempt(r, "one contract bid at 1¢, post-only, with an expiry", lambda: client.create_order(
        ticker, "buy", 1, 1, post_only=True, expires_at=int(time.time()) + QUOTE_EXPIRY_SECONDS,
        client_order_id=_order_id()))
    if answer is None:
        return
    r.learned("answer: " + json.dumps(answer))
    if not check_answer(r, answer):
        leave_nothing_behind(r, client, ticker)
        return
    order_id = answer["order_id"]
    listed = [o for o in client.get_orders(status="resting", ticker=ticker)["orders"] if o["order_id"] == order_id]
    r.check(bool(listed), "Kalshi lists it as resting")
    held = before - client.get_balance()["balance"]
    r.learned(f"resting orders {'hold' if held else 'do not hold'} money: the balance moved by {held}¢ for a 1¢ order")
    cancelled = _attempt(r, "cancelled", lambda: client.cancel_order(order_id, ticker))
    if cancelled is not None:
        r.check(float(cancelled.get("reduced_by", 0)) == 1, "the cancel took off one contract", json.dumps(cancelled))
    gone = [o for o in client.get_orders(status="resting", ticker=ticker)["orders"] if o["order_id"] == order_id]
    r.check(not gone, "Kalshi no longer lists it")
    r.check(client.cancel_order(order_id, ticker) == {}, "cancelling it again is treated as already gone")
    check_post_only(r, client, market)
    check_too_big(r, client, ticker, client.get_balance()["balance"])
    leave_nothing_behind(r, client, ticker)


def check_post_only(r, client, market):
    ticker, ask = market["ticker"], top_of_book(market)["yes_ask"]
    if ask is None:
        r.learned("skipped the post-only test: nothing offered in this market")
        return
    try:
        answer = client.create_order(ticker, "buy", 1, ask, post_only=True, client_order_id=_order_id(),
                                     expires_at=int(time.time()) + QUOTE_EXPIRY_SECONDS)
    except Exception as e:
        r.ok(f"a post-only bid at the {ask}¢ ask was refused ({_error_text(e)})")
        return
    r.learned("post-only answer: " + json.dumps(answer))
    r.check(float(answer.get("fill_count", 0)) == 0, "a post-only bid at the ask did not fill",
            f"it filled, so you now hold a contract on {ticker}")


def check_too_big(r, client, ticker, balance_cents):
    count = 2 * balance_cents + 100           # at 1¢ each: twice the balance, and more than an empty account has
    try:
        answer = client.create_order(ticker, "buy", count, 1, post_only=True, client_order_id=_order_id(),
                                     expires_at=int(time.time()) + QUOTE_EXPIRY_SECONDS)
    except Exception as e:
        r.ok(f"an order too big to pay for was refused ({_error_text(e)})")
        return
    r.fail(f"an order for {count} contracts at 1¢ was accepted: {json.dumps(answer)[:200]}")


def leave_nothing_behind(r, client, ticker):
    def mine():
        return [o for o in client.get_orders(status="resting", ticker=ticker)["orders"]
                if o.get("client_order_id", "").startswith(ID_PREFIX)]
    for o in mine():
        client.cancel_order(o["order_id"], ticker)
    r.check(not mine(), "this check left no orders resting")


# ---- takes ---------------------------------------------------------------------

def _stream_fills(stream, order_id, wait_seconds, got):
    deadline = time.time() + wait_seconds
    while True:
        got.extend(stream.drain_fills())
        mine = [f for f in got if f["order_id"] == order_id]
        if mine or time.time() >= deadline:
            return mine
        time.sleep(0.05)


def one_take(r, client, stream, ticker, action, price, wait_seconds, got):
    """Sends one immediate-or-cancel order and compares every source. Returns
    contracts filled, or None if the order failed."""
    answer = _attempt(r, f"{action} one at {price}¢, immediate-or-cancel",
                      lambda: client.create_order(ticker, action, 1, price, "immediate_or_cancel"))
    if answer is None:
        return None
    r.learned("answer: " + json.dumps(answer))
    if not check_answer(r, answer):
        return None
    filled = float(answer["fill_count"])
    r.check(float(answer["remaining_count"]) == 0, "nothing left over", json.dumps(answer))
    if filled == 0:
        r.fail(f"it missed: nothing left at {price}¢ by the time it arrived")
        return 0
    price_answered = round(float(answer["average_fill_price"]) * 100, 2)
    r.check(action == "buy" and price_answered <= price or action == "sell" and price_answered >= price,
            f"it filled at {price_answered}¢, the YES price, no worse than the limit", f"limit was {price}¢")
    fee_answered = round(float(answer["average_fee_paid"]) * 100, 4)
    pushed = _stream_fills(stream, answer["order_id"], wait_seconds, got)
    r.check(bool(pushed), "the fill stream delivered it")
    rest = [f for f in client.get_fills(min_ts=time.time() - 300) if f["order_id"] == answer["order_id"]]
    r.check(bool(rest), "the fills list has it")
    for source, fills in (("stream", pushed), ("fills list", rest)):
        if fills:
            f = fills[0]
            r.check(f["action"] == action and f["count"] == 1 and abs(f["price_cents"] - price_answered) < 0.01,
                    f"{source} agrees with the answer: {f['action']} {f['count']} @ {f['price_cents']}¢", json.dumps(f))
            r.check(abs(f["fee_cents"] - fee_answered) < 0.01 and not f["maker"],
                    f"{source} agrees on the fee, {f['fee_cents']}¢, as the taker", f"answer said {fee_answered}¢")
    return filled


def check_take(r, client, market, stream, wait_seconds):
    ticker = market["ticker"]
    r.say(f"Take on {ticker}")
    if stream is None or not stream.subscribed:
        r.fail("not trying a take without the fill stream")
        return
    start = client.get_positions().get(ticker, 0)
    book = top_of_book(client.get_markets_batch([ticker])[0])
    if book["yes_ask"] is None or book["yes_bid"] is None:
        r.fail("this market doesn't have both sides quoted, so there's nothing to buy and sell back")
        return
    got = []
    bought = one_take(r, client, stream, ticker, "buy", book["yes_ask"], wait_seconds, got)
    if not bought:
        return
    held = client.get_positions().get(ticker, 0)
    r.check(held - start == bought, "the position went up by that", f"Kalshi says {held}, it was {start}")
    book = top_of_book(client.get_markets_batch([ticker])[0])
    if book["yes_bid"] is None:
        r.fail(f"no bid to sell into. You hold {bought:g} contract(s) on {ticker}: close them on Kalshi")
        return
    one_take(r, client, stream, ticker, "sell", book["yes_bid"], wait_seconds, got)
    now = client.get_positions().get(ticker, 0)
    r.check(now == start, "sold back, and the position is where it started",
            f"Kalshi says {now} on {ticker}, it was {start}: close the difference on Kalshi")


# ---- the whole thing -----------------------------------------------------------

def run(argv, client, ws_url, stream_factory, confirm, out, wait_seconds=10.0) -> int:
    parser = argparse.ArgumentParser(description="Try the bot's live path against Kalshi's real API.")
    parser.add_argument("--order", action="store_true", help="also place and cancel a resting order")
    parser.add_argument("--take", action="store_true", help="also buy one contract and sell it back")
    parser.add_argument("--real-money", action="store_true", help="allow orders outside the demo environment")
    args = parser.parse_args(argv)
    r = Report(out)
    is_demo = ".demo." in client.base_url or "demo-api" in client.base_url
    r.say(f"Kalshi at {client.base_url} ({'demo, mock funds' if is_demo else 'REAL MONEY'})")
    sends_orders = args.order or args.take
    if sends_orders and not is_demo:
        if not args.real_money:
            r.say("These options send real orders. Add --real-money if that's what you mean, "
                  "or point KALSHI_BASE_URL and KALSHI_WS_URL at the demo environment.")
            return 2
        cost = ("about 5¢ in spread and fees, and possibly one contract you'd have to close" if args.take
                else "nothing unless Kalshi ignores post-only, then one contract at the ask")
        if confirm(f"Send real orders on your real account? Cost: {cost}. Type yes to go ahead: ").strip().lower() != "yes":
            r.say("Nothing sent.")
            return 2
    if client.auth is None:
        r.fail("no API key: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
        return 1
    check_reads(r, client)
    stream = check_fill_stream(r, client.auth, ws_url, stream_factory, wait_seconds)
    market = pick_market(r, client)
    if market and check_funds(r, client, market):
        if args.order:
            check_order(r, client, market)
        if args.take:
            check_take(r, client, market, stream, wait_seconds)
    r.say()
    r.say("Every check passed." if not r.failures else f"{r.failures} check(s) failed. Don't go live until they're explained.")
    return 1 if r.failures else 0


def _real_stream(auth, ws_url):
    stream = feeds.UserStream(auth, ws_url)
    stream.start()
    return stream


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ws = CONFIG.kalshi.fills_ws_url or CONFIG.kalshi.ws_url
    sys.exit(run(sys.argv[1:], build_client(CONFIG), ws, _real_stream, input, sys.stdout))
