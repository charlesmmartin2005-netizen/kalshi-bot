"""
Executes the strategy for each watched market: fetch the book, ask
fair_value for a model price, let strategy.plan_quotes decide, then make
the resting orders match the plan (cancel/replace only what changed) and
send any takes.

PAPER mode never sends orders. Fills are simulated from the market's real
public trade tape (process_trades): a resting quote fills when an actual
taker traded at a price our quote would have been first in line for. The
first build filled whenever the book *moved through* the quote, which in
quiet markets missed most real flow — it showed ~6 fills where trade-based
replay showed ~18 for the same two weeks.

LIVE mode sends real orders, and every position the risk manager sees comes
from Kalshi. A take is booked from the order's own answer, so the very next
decision knows about it. Everything else comes through process_live_fills,
from Kalshi's fill stream or its REST fills. No order goes out that the account
can't pay for, and orders that keep failing halt the bot.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime

import fair_value
import ledger
from config import BotConfig
from fees import order_fee_cents
from kalshi_client import KalshiClient, WriteBudget, normalize_orderbook, whole
from risk_manager import RiskManager
from strategy import plan_quotes

log = logging.getLogger("market_maker")

# Paper fills: how long a cancelled/replaced quote can still be matched to
# trades that happened while it was resting.
CANCELLED_MEMORY_SECONDS = 30
# Live quotes carry an expiry, so a bot that dies can't leave them on Kalshi at
# old prices for long (2026-10-26: quotes left while the PC slept lost $115 in
# paper). Well past max_quote_age_seconds, so a running bot replaces them first.
QUOTE_EXPIRY_SECONDS = 60
# Quotes leave this much of the write budget unspent, so a take never waits on one.
TAKE_RESERVE_TOKENS = 30


@dataclass
class RestingQuote:
    order_id: str
    ticker: str
    side: str          # "yes"
    action: str        # "buy" or "sell"
    price_cents: int
    count: int
    strategy: str = ""
    improved: bool = False   # strictly better than the book's touch when placed
    model_cents: float | None = None   # raw model when placed (logged with fills)
    placed_at: float = field(default_factory=time.time)
    cancelled_at: float | None = None


class MarketMaker:
    def __init__(self, client: KalshiClient, risk: RiskManager, config: BotConfig):
        self.client = client
        self.risk = risk
        self.cfg = config
        self.resting_quotes: dict[str, list[RestingQuote]] = {}
        self.last_plan: dict[str, dict] = {}      # ticker -> summary for the dashboard
        self._seen_trades: dict[str, set] = {}
        self.deltas: dict[str, tuple[str, float]] = {}   # ticker -> (coin, Δprob per 1% move)
        self._recently_cancelled: dict[str, list[RestingQuote]] = {}
        # Paper takes never reach the real book, so the offer we just lifted
        # is still there on the next poll — on 2026-10-21 the bot "bought"
        # one 38¢ offer seven times in 3s. Contracts taken at each touch price
        # come off what that touch shows until the touch moves.
        self._paper_taken: dict[str, dict] = {}   # ticker -> {(action, price): contracts}
        self._cap_logged: dict[str, float] = {}  # coin -> last "takes held back" log time
        # Paper-fill diagnostics: for every tape trade, cents short of our
        # quote on that side (0 = reached it); dashboard/health checks read it.
        self.fill_stats = {"trades": 0, "no_quote": 0, "miss_hist": [0] * 11}
        # Live only.
        self.available_cents: float | None = None   # Kalshi's available balance; None = not known, so no orders
        self._seen_fills: set[tuple] = set()        # (trade_id, order_id) already handled
        # Contracts booked from an order's own answer that Kalshi hasn't sent
        # as fills yet: those fills, when they arrive, are already counted.
        self._booked_by_answer: dict[str, float] = {}
        self._order_info: dict[str, tuple] = {}     # order or client order ID -> (strategy, model cents)
        self.orders_failed_in_a_row = 0
        self.budget = WriteBudget()

    @property
    def paper(self):
        return self.cfg.kalshi.paper_trading

    # ---- one evaluation cycle for a single market -------------------------

    def evaluate_market(self, ticker: str, market: dict | None = None, book: dict | None = None,
                        allow_takes: bool = True) -> dict:
        if book is None:
            book = normalize_orderbook(self.client.get_orderbook(ticker))
        fv = fair_value.fair_value(market) if market and fair_value.supported(market) else None
        sides, why = self.risk.allowed_sides(ticker)
        if fv and fv.underlying:
            self.deltas[ticker] = (fv.underlying, fv.delta)
        lean, port = self._delta_lean(ticker, fv)
        plan_book = self._paper_depth(ticker, book) if self.paper else book
        if lean:
            # The lean moves resting quotes only; takes are judged on the raw
            # model (hedging by crossing the spread lost money in testing).
            leaned = replace(fv, cents=min(99.0, max(1.0, fv.cents + lean)))
            plan = plan_quotes(plan_book, self.risk.net(ticker), self.cfg.risk, sides, leaned, anchor=fv.cents)
            plan.takes = plan_quotes(plan_book, self.risk.net(ticker), self.cfg.risk, sides, fv).takes
        else:
            plan = plan_quotes(plan_book, self.risk.net(ticker), self.cfg.risk, sides, fv)
        if not allow_takes:
            plan.takes = []
        plan.takes = self._cap_take_delta(plan.takes, fv)

        for action, price, size in plan.takes:
            self._take(ticker, action, price, size, fv)
        model = fv.cents if fv and fv.tradeable else None
        self._sync_side(ticker, "buy", plan.buy, book, plan.mode, model)
        self._sync_side(ticker, "sell", plan.sell, book, plan.mode, model)

        self.last_plan[ticker] = {
            "mode": plan.mode, "reason": why or plan.reason, "skew": round(plan.skew, 2),
            "fv": round(fv.cents, 1) if fv else None, "fv_model": fv.model if fv else None,
            "fv_detail": fv.detail if fv else None, "fv_tradeable": bool(fv and fv.tradeable),
            "takes": len(plan.takes), "lean": round(lean, 2),
            "book_delta_usd": round(port, 2) if port is not None else None,
        }
        return book

    def _paper_depth(self, ticker, book) -> dict:
        """The book with our own paper takes removed from the touch sizes."""
        taken = self._paper_taken.setdefault(ticker, {})
        touch = {"buy": book.get("yes_ask"), "sell": book.get("yes_bid")}
        for key in [k for k in taken if touch[k[0]] != k[1]]:
            del taken[key]                 # touch moved off that price: fresh level next time
        if not taken:
            return book
        book = dict(book)
        for (action, _), n in taken.items():
            key = "yes_ask_size" if action == "buy" else "yes_bid_size"
            if book.get(key) is not None:
                book[key] = max(0.0, book[key] - n)
        return book

    def _book_delta(self, coin) -> float:
        """The whole book's $ gain per +1% move in `coin` (each contract pays
        $1, so net contracts x probability change per 1%)."""
        return sum(self.risk.net(t) * d for t, (u, d) in self.deltas.items() if u == coin)

    def _delta_lean(self, ticker, fv) -> tuple[float, float | None]:
        """Cents to shift this market's quote center, and the whole book's
        $ gain per +1% move in this market's coin. Long the coin -> negative
        lean (sell more eagerly), scaled by how price-sensitive this strike
        is relative to the most sensitive one in the same coin."""
        r = self.cfg.risk
        if not fv or not fv.tradeable or not fv.underlying or not r.delta_lean_cents:
            return 0.0, None
        same = [d for u, d in self.deltas.values() if u == fv.underlying]
        port = self._book_delta(fv.underlying)
        dmax = max((abs(d) for d in same), default=0) or 1
        scale = max(-1.0, min(1.0, port / r.delta_lean_ref_usd))
        return -r.delta_lean_cents * scale * fv.delta / dmax, port

    def _cap_take_delta(self, takes, fv) -> list:
        """Shrink or drop takes that would push the book's coin exposure past
        max_take_book_delta_usd per 1% (either way). A take that reduces
        exposure always goes through."""
        cap = self.cfg.risk.max_take_book_delta_usd
        if not takes or cap is None or not fv or not fv.underlying or not fv.delta:
            return takes
        port, kept = self._book_delta(fv.underlying), []
        for action, price, size in takes:
            step = fv.delta if action == "buy" else -fv.delta
            while size > 0 and abs(port + step * size) > max(cap, abs(port)):
                size -= 1
            if size > 0:
                kept.append((action, price, size))
                port += step * size
            elif time.time() - self._cap_logged.get(fv.underlying, 0) > 60:   # reprices run many times a second
                self._cap_logged[fv.underlying] = time.time()
                log.info("Takes held back on %s: book exposure $%.0f per 1%% is at the $%.0f cap",
                         fv.underlying, port, cap)
        return kept

    def min_required_spread(self) -> float:
        """Spread needed for book-mode quoting at a 50¢ mid (fees peak there)."""
        from fees import per_contract_fee_cents
        return round(2 * per_contract_fee_cents(50) + self.cfg.risk.min_edge_cents, 1)

    # ---- order management ---------------------------------------------------

    def _sync_side(self, ticker, action, target, book, mode, model_cents=None):
        """Make the resting quote on one side match `target` ((price, size) or
        None). Replaces it if the price changed or it's older than
        max_quote_age_seconds."""
        quotes = self.resting_quotes.setdefault(ticker, [])
        current = [q for q in quotes if q.action == action]
        stale = any(time.time() - q.placed_at > self.cfg.risk.max_quote_age_seconds for q in current)
        if current and target:
            gap = abs(current[0].price_cents - target[0])
            close_enough = gap == 0 if self.paper else gap < self.cfg.risk.requote_on_move_cents
            if close_enough and not stale:
                return
            if close_enough and not self.paper and not self.budget.can_spend(
                    WriteBudget.ORDER + WriteBudget.CANCEL, keep=TAKE_RESERVE_TOKENS):
                return                     # only old, not wrong: it can wait for budget
        for q in current:
            self._cancel(q)
            quotes.remove(q)
        if target:
            price, size = target
            touch = book["yes_bid"] if action == "buy" else book["yes_ask"]
            improved = touch is None or (price > touch if action == "buy" else price < touch)
            self._place(ticker, action, price, size, mode, improved, model_cents)

    def _place(self, ticker, action, price, size, strategy, improved, model_cents=None):
        if not self.paper and not (self._can_pay(ticker, action, price, size)
                                   and self.budget.spend(WriteBudget.ORDER, keep=TAKE_RESERVE_TOKENS)):
            return
        client_order_id = str(uuid.uuid4())
        if self.paper:
            order_id = f"paper-{client_order_id}"
            log.info("[PAPER] Quote %s yes %d @ %d¢ on %s (%s)", action, size, price, ticker, strategy)
        else:
            self._remember_order(client_order_id, strategy, model_cents)
            resp = self._send_order(ticker, action, size, price, post_only=True,
                                    expires_at=int(time.time()) + QUOTE_EXPIRY_SECONDS,
                                    client_order_id=client_order_id)
            if resp is None:
                return
            order_id = resp["order_id"]
            self._remember_order(order_id, strategy, model_cents)
            log.info("Placed live order %s: %s yes %s @ %d¢ on %s", order_id, action, size, price, ticker)
            if float(resp["remaining_count"]) <= 0:
                return                     # a post-only order that would have crossed
        self.resting_quotes.setdefault(ticker, []).append(RestingQuote(
            order_id=order_id, ticker=ticker, side="yes", action=action, price_cents=price,
            count=size, strategy=strategy, improved=improved, model_cents=model_cents))

    def _cancel(self, q: RestingQuote):
        q.cancelled_at = time.time()
        if self.paper:
            # Keep it briefly: the trade tape arrives a few seconds late, and
            # a trade that happened while this quote was resting must still
            # be able to fill it.
            recent = self._recently_cancelled.setdefault(q.ticker, [])
            recent.append(q)
            cutoff = time.time() - CANCELLED_MEMORY_SECONDS
            recent[:] = [x for x in recent if x.cancelled_at >= cutoff]
        else:
            self.budget.owe(WriteBudget.CANCEL)     # a quote at a wrong price goes, budget or not
            try:
                self.client.cancel_order(q.order_id, q.ticker)
            except Exception as e:
                log.error("Cancel failed for %s: %s", q.order_id, e)

    def _take(self, ticker, action, price, size, fv):
        log.info("%sTAKE %s yes %d @ %d¢ on %s (model %.1f¢)",
                 "[PAPER] " if self.paper else "", action, size, price, ticker, fv.cents)
        if self.paper:
            # The touch was really there (and plan_quotes capped us at its
            # size), so a take fills immediately.
            taken = self._paper_taken.setdefault(ticker, {})
            taken[(action, price)] = taken.get((action, price), 0) + size
            self._record_fill(ticker, action, size, price, maker=False, strategy="take", model_cents=fv.cents)
        else:
            if not self._can_pay(ticker, action, price, size):
                log.info("Take on %s skipped: the account can't pay for it", ticker)
                return
            if not self.budget.spend(WriteBudget.ORDER):
                log.warning("Take on %s skipped: at the limit on orders per second", ticker)
                return
            # Immediate-or-cancel: whatever doesn't fill now is gone, not left
            # on the book at a price nothing is watching.
            client_order_id = str(uuid.uuid4())
            self._remember_order(client_order_id, "take", fv.cents)
            resp = self._send_order(ticker, action, size, price, "immediate_or_cancel",
                                    client_order_id=client_order_id)
            if resp is not None:
                self._book_take_answer(ticker, action, resp, fv.cents)

    def _cancel_resting(self, ticker: str, reason: str = ""):
        quotes = self.resting_quotes.get(ticker, [])
        if not quotes:
            return
        log.info("Cancelling %d resting quote(s) on %s (%s)", len(quotes), ticker, reason)
        for q in quotes:
            self._cancel(q)
        self.resting_quotes[ticker] = []

    def cancel_all(self, reason: str):
        for ticker in list(self.resting_quotes):
            self._cancel_resting(ticker, reason=reason)

    # ---- live orders and fills ------------------------------------------------

    def resume_live(self, fills: list[dict]):
        """After a restart, from the ledger's live rows: which fills are already
        booked, whether they arrived as fills or in a take's own answer."""
        for f in fills:
            if f.get("trade_id"):
                self._seen_fills.add((f["trade_id"], f.get("order_id")))
            elif f.get("order_id"):
                self._booked_by_answer[f["order_id"]] = round(self._booked_by_answer.get(f["order_id"], 0) + f["count"], 2)

    def _send_order(self, ticker, action, size, price, time_in_force="good_till_canceled", **options):
        """The order's answer, or None if it failed. Rejected and unanswered
        orders both count: repeating them forever isn't trading. So does an
        answer the bot can't read: the order may be up with no ID to cancel it
        by, and carrying on would place another every cycle."""
        if self.risk.halted:
            return None
        try:
            resp = self.client.create_order(ticker, action, size, price, time_in_force, **options)
            missing = [k for k in ("order_id", "fill_count", "remaining_count") if k not in resp]
            if missing:
                raise ValueError(f"the answer has no {missing}: {str(resp)[:200]}")
        except Exception as e:
            self.orders_failed_in_a_row += 1
            log.error("Order failed (%d in a row): %s yes %s @ %d¢ on %s: %s",
                      self.orders_failed_in_a_row, action, size, price, ticker, e)
            if self.orders_failed_in_a_row >= self.cfg.risk.max_consecutive_errors:
                self.risk._halt(f"{self.orders_failed_in_a_row} orders in a row rejected or unanswered; last: {e}")
            return None
        self.orders_failed_in_a_row = 0
        return resp

    def _can_pay(self, ticker, action, price, size) -> bool:
        """Can the account pay for this order, after what's held for the quotes
        already resting? Closing a position costs nothing. This counts resting
        quotes even if Kalshi's balance already leaves them out, which only
        makes it cautious."""
        if self.available_cents is None:
            return False
        opening = size - self._closing_part(ticker, action, size)
        if opening <= 0:
            return True
        need = opening * (price if action == "buy" else 100 - price) + order_fee_cents(price, opening, maker=False)
        held = sum(q.count * (q.price_cents if q.action == "buy" else 100 - q.price_cents)
                   for quotes in self.resting_quotes.values() for q in quotes)
        return need + held <= self.available_cents

    def _closing_part(self, ticker, action, size):
        """How much of an order of this size closes the position, not opens one."""
        net = self.risk.net(ticker)
        return min(size, abs(net)) if (net > 0 and action == "sell") or (net < 0 and action == "buy") else 0

    def _remember_order(self, order_id, strategy, model_cents):
        self._order_info[order_id] = (strategy, model_cents)
        if len(self._order_info) > 5000:
            for key in list(self._order_info)[:3000]:
                del self._order_info[key]

    def _book_take_answer(self, ticker, action, resp, model_cents):
        """An immediate-or-cancel order is finished when it answers, so what the
        answer says filled is all that ever will."""
        order_id, filled = resp["order_id"], float(resp["fill_count"])
        self._remember_order(order_id, "take", model_cents)
        if filled <= 0:
            log.info("Take on %s missed: nothing left at the price", ticker)
            return
        if "average_fill_price" not in resp or "average_fee_paid" not in resp:
            log.error("Take on %s filled but the answer has no price or fee, so it waits for the fill: %s", ticker, resp)
            return
        filled = whole(filled)
        price = whole(round(float(resp["average_fill_price"]) * 100, 2))
        fee = whole(round(float(resp["average_fee_paid"]) * filled * 100, 4))
        opening = filled - self._closing_part(ticker, action, filled)
        self.available_cents -= opening * (price if action == "buy" else 100 - price) + fee
        self._booked_by_answer[order_id] = filled
        self._book_live(ticker, action, filled, price, False, "take", model_cents, fee, order_id, None, None)

    def process_live_fills(self, fills: list[dict]):
        """Book fills from Kalshi (normalize_fill's shape): from the stream, from
        REST, or both, in any order. Each counts once, and fills of a take
        already booked from its answer don't count again. Fills of an order the
        bot didn't place count too: it's still the account's money at risk."""
        for f in sorted(fills, key=lambda f: f["ts"]):
            key = (f["trade_id"], f["order_id"])
            if key in self._seen_fills:
                continue
            self._seen_fills.add(key)
            self._quote_filled(f)
            left = self._booked_by_answer.get(f["order_id"], 0)
            if left > 0:
                self._booked_by_answer[f["order_id"]] = round(left - f["count"], 2)
                continue
            info = self._order_info.get(f["order_id"]) or self._order_info.get(f.get("client_order_id"))
            strategy, model = info or (("" if f["maker"] else "take"), None)
            self._book_live(f["ticker"], f["action"], f["count"], f["price_cents"], f["maker"], strategy, model,
                            f["fee_cents"], f["order_id"], f["trade_id"], f["ts"])

    def _quote_filled(self, fill):
        quotes = self.resting_quotes.get(fill["ticker"], [])
        for q in quotes:
            if q.order_id == fill["order_id"]:
                q.count = round(q.count - fill["count"], 2)
                if q.count <= 0:
                    quotes.remove(q)
                return

    def _book_live(self, ticker, action, count, price, maker, strategy, model_cents, fee_cents, order_id, trade_id, ts):
        log.info("[LIVE FILL] %s yes %s @ %s¢ on %s (%s, %s, fee %s¢)", action, count, price, ticker,
                 "maker" if maker else "taker", strategy, fee_cents)
        ledger.append_fill(ticker, "yes", action, count, price, mode="live", liquidity="maker" if maker else "taker",
                           strategy=strategy, model_cents=model_cents, fee_cents=fee_cents, order_id=order_id,
                           trade_id=trade_id, ts=ts)
        self.risk.record_fill(ticker, "yes", action, count, price, maker=maker, fee_cents=fee_cents)

    # ---- paper fills from the real trade tape ---------------------------------

    def process_trades(self, ticker: str, trades: list):
        """trades: Kalshi /markets/trades rows (any order). A buy quote fills
        when a taker SOLD yes at or below our bid; a sell quote fills when a
        taker BOUGHT yes at or above our ask. At exactly our price we only
        count it if our quote improved the touch (we'd have been alone at the
        front of the queue); otherwise someone ahead of us got it."""
        seen = self._seen_trades.setdefault(ticker, set())
        quotes = self.resting_quotes.get(ticker, [])
        cancelled = self._recently_cancelled.get(ticker, [])
        for t in sorted(trades, key=lambda t: t["created_time"]):
            if t["trade_id"] in seen:
                continue
            seen.add(t["trade_id"])
            ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
            price = round(float(t["yes_price_dollars"]) * 100)
            remaining = float(t["count_fp"])
            taker_buys = t["taker_side"] == "yes"
            # Diagnostics: how far was this trade from our quote on its side?
            side = "sell" if taker_buys else "buy"
            live = [q for q in cancelled + list(quotes) if q.action == side and q.placed_at <= ts
                    and (q.cancelled_at is None or ts < q.cancelled_at)]
            st = self.fill_stats
            st["trades"] += 1
            if live:
                q0 = live[-1]
                gap = (q0.price_cents - price) if side == "sell" else (price - q0.price_cents)
                st["miss_hist"][min(max(int(gap), 0), 10)] += 1     # 0 = reached our quote
            else:
                st["no_quote"] += 1
            # Match against whatever was actually resting when the trade
            # happened — including quotes replaced since (the bot reprices
            # every few seconds, and the tape lags).
            for q in cancelled + list(quotes):
                if remaining <= 0 or q.count <= 0 or ts < q.placed_at:
                    continue
                if q.cancelled_at is not None and ts >= q.cancelled_at:
                    continue
                if taker_buys and q.action == "sell":
                    hit = price > q.price_cents or (price == q.price_cents and q.improved)
                elif not taker_buys and q.action == "buy":
                    hit = price < q.price_cents or (price == q.price_cents and q.improved)
                else:
                    continue
                if not hit:
                    continue
                n = max(1, int(min(remaining, q.count)))
                remaining -= n
                q.count -= n
                self._record_fill(ticker, q.action, n, q.price_cents, maker=True, strategy=q.strategy,
                                  model_cents=q.model_cents)
                if q.count <= 0 and q in quotes:
                    quotes.remove(q)
        if len(seen) > 5000:
            self._seen_trades[ticker] = set(list(seen)[-2000:])

    def _record_fill(self, ticker, action, count, price, maker, strategy, model_cents=None):
        log.info("[PAPER FILL] %s yes %d @ %d¢ on %s (%s, %s)", action, count, price, ticker,
                 "maker" if maker else "taker", strategy)
        ledger.append_fill(ticker, "yes", action, count, price, mode="paper",
                           liquidity="maker" if maker else "taker", strategy=strategy, model_cents=model_cents)
        self.risk.record_fill(ticker, "yes", action, count, price, maker=maker)
