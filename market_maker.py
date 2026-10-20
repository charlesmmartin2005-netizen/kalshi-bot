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
"""
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime

import fair_value
import ledger
from config import BotConfig
from kalshi_client import KalshiClient, normalize_orderbook
from risk_manager import RiskManager
from strategy import plan_quotes

log = logging.getLogger("market_maker")

# Paper fills: how long a cancelled/replaced quote can still be matched to
# trades that happened while it was resting.
CANCELLED_MEMORY_SECONDS = 30


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

    @property
    def paper(self):
        return self.cfg.kalshi.paper_trading

    # ---- one evaluation cycle for a single market -------------------------

    def evaluate_market(self, ticker: str, market: dict | None = None, book: dict | None = None) -> dict:
        if book is None:
            book = normalize_orderbook(self.client.get_orderbook(ticker))
        fv = fair_value.fair_value(market) if market and fair_value.supported(market) else None
        sides, why = self.risk.allowed_sides(ticker)
        if fv and fv.underlying:
            self.deltas[ticker] = (fv.underlying, fv.delta)
        lean, port = self._delta_lean(ticker, fv)
        if lean:
            # The lean moves resting quotes only; takes are judged on the raw
            # model (hedging by crossing the spread lost money in testing).
            leaned = replace(fv, cents=min(99.0, max(1.0, fv.cents + lean)))
            plan = plan_quotes(book, self.risk.net(ticker), self.cfg.risk, sides, leaned)
            plan.takes = plan_quotes(book, self.risk.net(ticker), self.cfg.risk, sides, fv).takes
        else:
            plan = plan_quotes(book, self.risk.net(ticker), self.cfg.risk, sides, fv)

        for action, price, size in plan.takes:
            self._take(ticker, action, price, size, fv)
        self._sync_side(ticker, "buy", plan.buy, book, plan.mode)
        self._sync_side(ticker, "sell", plan.sell, book, plan.mode)

        self.last_plan[ticker] = {
            "mode": plan.mode, "reason": why or plan.reason, "skew": round(plan.skew, 2),
            "fv": round(fv.cents, 1) if fv else None, "fv_model": fv.model if fv else None,
            "fv_detail": fv.detail if fv else None, "fv_tradeable": bool(fv and fv.tradeable),
            "takes": len(plan.takes), "lean": round(lean, 2),
            "book_delta_usd": round(port, 2) if port is not None else None,
        }
        return book

    def _delta_lean(self, ticker, fv) -> tuple[float, float | None]:
        """Cents to shift this market's quote center, and the whole book's
        $ gain per +1% move in this market's coin. Long the coin -> negative
        lean (sell more eagerly), scaled by how price-sensitive this strike
        is relative to the most sensitive one in the same coin."""
        r = self.cfg.risk
        if not fv or not fv.tradeable or not fv.underlying or not r.delta_lean_cents:
            return 0.0, None
        same = {t: d for t, (u, d) in self.deltas.items() if u == fv.underlying}
        port = sum(self.risk.net(t) * d for t, d in same.items())
        dmax = max((abs(d) for d in same.values()), default=0) or 1
        scale = max(-1.0, min(1.0, port / r.delta_lean_ref_usd))
        return -r.delta_lean_cents * scale * fv.delta / dmax, port

    def min_required_spread(self) -> float:
        """Spread needed for book-mode quoting at a 50¢ mid (fees peak there)."""
        from fees import per_contract_fee_cents
        return round(2 * per_contract_fee_cents(50) + self.cfg.risk.min_edge_cents, 1)

    # ---- order management ---------------------------------------------------

    def _sync_side(self, ticker, action, target, book, mode):
        """Make the resting quote on one side match `target` ((price, size) or
        None). Replaces it if the price changed or it's older than
        max_quote_age_seconds."""
        quotes = self.resting_quotes.setdefault(ticker, [])
        current = [q for q in quotes if q.action == action]
        stale = any(time.time() - q.placed_at > self.cfg.risk.max_quote_age_seconds for q in current)
        if current and target and not stale and current[0].price_cents == target[0]:
            return
        for q in current:
            self._cancel(q)
            quotes.remove(q)
        if target:
            price, size = target
            touch = book["yes_bid"] if action == "buy" else book["yes_ask"]
            improved = touch is None or (price > touch if action == "buy" else price < touch)
            self._place(ticker, action, price, size, mode, improved)

    def _place(self, ticker, action, price, size, strategy, improved):
        client_order_id = str(uuid.uuid4())
        if self.paper:
            order_id = f"paper-{client_order_id}"
            log.info("[PAPER] Quote %s yes %d @ %d¢ on %s (%s)", action, size, price, ticker, strategy)
        else:
            resp = self.client.create_order(ticker=ticker, side="yes", action=action, count=size,
                                            price_cents=price, client_order_id=client_order_id)
            order_id = resp.get("order", {}).get("order_id", client_order_id)
            log.info("Placed live order %s: %s yes %d @ %d¢ on %s", order_id, action, size, price, ticker)
        self.resting_quotes.setdefault(ticker, []).append(RestingQuote(
            order_id=order_id, ticker=ticker, side="yes", action=action, price_cents=price,
            count=size, strategy=strategy, improved=improved))

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
            try:
                self.client.cancel_order(q.order_id)
            except Exception as e:
                log.error("Cancel failed for %s: %s", q.order_id, e)

    def _take(self, ticker, action, price, size, fv):
        log.info("%sTAKE %s yes %d @ %d¢ on %s (model %.1f¢)",
                 "[PAPER] " if self.paper else "", action, size, price, ticker, fv.cents)
        if self.paper:
            # The touch was really there, so a take fills immediately.
            self._record_fill(ticker, action, size, price, maker=False, strategy="take")
        else:
            self.client.create_order(ticker=ticker, side="yes", action=action, count=size,
                                     price_cents=price, client_order_id=str(uuid.uuid4()))
            # TODO(live): confirm fills via /portfolio/fills before recording.

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
                self._record_fill(ticker, q.action, n, q.price_cents, maker=True, strategy=q.strategy)
                if q.count <= 0 and q in quotes:
                    quotes.remove(q)
        if len(seen) > 5000:
            self._seen_trades[ticker] = set(list(seen)[-2000:])

    def _record_fill(self, ticker, action, count, price, maker, strategy):
        log.info("[PAPER FILL] %s yes %d @ %d¢ on %s (%s, %s)", action, count, price, ticker,
                 "maker" if maker else "taker", strategy)
        ledger.append_fill(ticker, "yes", action, count, price, mode="paper",
                           liquidity="maker" if maker else "taker", strategy=strategy)
        self.risk.record_fill(ticker, "yes", action, count, price, maker=maker)
