"""
P&L accounting over the fills ledger, shared by show_pnl.py (terminal) and
the dashboard.

Uses average-cost accounting: every time a position's direction flips
through zero or reduces, the difference between entry and exit price on
the closed portion is realized P&L. Contracts still held are unrealized —
UNLESS the market has settled, in which case the settlement value ($1.00
for a "yes" resolution, $0.00 for "no") is booked as realized P&L. Without
that check, a "yes" position in a market that resolved "no" would silently
look like an open position forever instead of the loss it actually is.
"""
import time

import ledger
from fees import order_fee_cents

STARTING_BALANCE_USD = 500.00

# Fees use Kalshi's price-dependent formula (fees.py). Settlement itself is
# NOT charged a trading fee. Old ledger rows without a "liquidity" field are
# treated as maker fills.


class MarketLedger:
    """Average-cost position tracker for one ticker."""
    def __init__(self, ticker):
        self.ticker = ticker
        self.net_contracts = 0       # positive = net long yes, negative = net long no (short yes)
        self.avg_cost_cents = 0.0
        self.realized_pnl_cents = 0.0
        self.total_fees_cents = 0.0
        self.fill_count = 0
        self.settled = False
        self.settlement_note = ""

    def apply_fill(self, side, action, count, price_cents, maker=True):
        self.fill_count += 1
        self.total_fees_cents += order_fee_cents(price_cents, count, maker)

        # Normalize everything to "yes exposure": buying yes or selling no
        # increases yes exposure; selling yes or buying no decreases it.
        if action == "buy" and side == "yes":
            delta, fill_price = count, price_cents
        elif action == "sell" and side == "yes":
            delta, fill_price = -count, price_cents
        elif action == "buy" and side == "no":
            delta, fill_price = -count, 100 - price_cents
        else:  # sell no
            delta, fill_price = count, 100 - price_cents

        if self.net_contracts == 0 or (self.net_contracts > 0) == (delta > 0):
            # Adding to (or opening) a position in the same direction —
            # update average cost, no realized P&L yet.
            total_cost = self.avg_cost_cents * abs(self.net_contracts) + fill_price * abs(delta)
            self.net_contracts += delta
            if self.net_contracts != 0:
                self.avg_cost_cents = total_cost / abs(self.net_contracts)
        else:
            # Reducing or flipping a position — realize P&L on the closed portion.
            closing = min(abs(delta), abs(self.net_contracts))
            if self.net_contracts > 0:
                # Was long yes, now selling — profit if fill_price > avg cost.
                self.realized_pnl_cents += closing * (fill_price - self.avg_cost_cents)
            else:
                # Was short yes (long no), now buying yes back.
                self.realized_pnl_cents += closing * (self.avg_cost_cents - fill_price)
            self.net_contracts += delta
            if abs(delta) > closing:
                # Flipped past zero into the opposite direction.
                self.avg_cost_cents = fill_price

    def settle(self, settlement_price_cents, result_label: str):
        """Close out any remaining open position at the market's final
        settlement price. No trading fee applies to settlement itself —
        only to the original fills that opened the position."""
        if self.net_contracts == 0:
            return
        if self.net_contracts > 0:
            self.realized_pnl_cents += self.net_contracts * (settlement_price_cents - self.avg_cost_cents)
        else:
            self.realized_pnl_cents += abs(self.net_contracts) * (self.avg_cost_cents - settlement_price_cents)
        self.settled = True
        self.settlement_note = f"settled '{result_label}' @ {settlement_price_cents}¢"
        self.net_contracts = 0


# Finalized markets never change, so remember them instead of re-asking the
# API on every dashboard refresh.
_settlement_cache: dict[str, tuple[int, str]] = {}


def check_settlement(client, ticker):
    """Returns (is_finalized, settlement_price_cents, result_label) for a
    ticker, or (False, None, None) if it's still open or the check fails."""
    if ticker in _settlement_cache:
        return (True, *_settlement_cache[ticker])
    try:
        market = client.get_market(ticker).get("market", {})
        if market.get("status") == "finalized":
            result = market.get("result", "")
            settlement_price_cents = 100 if result == "yes" else 0
            _settlement_cache[ticker] = (settlement_price_cents, result)
            return True, settlement_price_cents, result
    except Exception:
        pass
    return False, None, None


def _is_maker(fill: dict) -> bool:
    return fill.get("liquidity", "maker") == "maker"


def compute(client=None, fills=None) -> dict:
    """Replay the ledger into per-market ledgers and portfolio totals. Pass
    a client to book settlements for open positions in resolved markets."""
    fills = ledger.read_all_fills() if fills is None else fills
    by_ticker: dict[str, MarketLedger] = {}
    for f in fills:
        ml = by_ticker.setdefault(f["ticker"], MarketLedger(f["ticker"]))
        ml.apply_fill(f["side"], f["action"], f["count"], f["price_cents"], _is_maker(f))

    if client is not None:
        for ticker, ml in by_ticker.items():
            if ml.net_contracts != 0:
                is_final, settle_price, result = check_settlement(client, ticker)
                if is_final:
                    ml.settle(settle_price, result)

    realized = sum(ml.realized_pnl_cents for ml in by_ticker.values())
    fees = sum(ml.total_fees_cents for ml in by_ticker.values())
    net_usd = (realized - fees) / 100

    # Equity curve: balance after each fill, for the dashboard chart. Uses
    # trade-level realized P&L only (settlements land at the end).
    curve, running = [], MarketLedgerSet()
    for f in fills:
        running.apply(f)
        curve.append({"ts": f["ts"], "balance": STARTING_BALANCE_USD + running.net_usd()})
    if curve and abs(curve[-1]["balance"] - (STARTING_BALANCE_USD + net_usd)) > 1e-9:
        curve.append({"ts": time.time(), "balance": STARTING_BALANCE_USD + net_usd})

    return {
        "markets": sorted(by_ticker.values(), key=lambda ml: -ml.realized_pnl_cents),
        "realized_usd": realized / 100,
        "fees_usd": fees / 100,
        "net_usd": net_usd,
        "starting_balance_usd": STARTING_BALANCE_USD,
        "balance_usd": STARTING_BALANCE_USD + net_usd,
        "fill_count": len(fills),
        "equity_curve": curve,
    }


class MarketLedgerSet:
    """Incremental replay helper used to build the equity curve."""
    def __init__(self):
        self.ledgers: dict[str, MarketLedger] = {}

    def apply(self, f):
        ml = self.ledgers.setdefault(f["ticker"], MarketLedger(f["ticker"]))
        ml.apply_fill(f["side"], f["action"], f["count"], f["price_cents"], _is_maker(f))

    def net_usd(self):
        return sum(ml.realized_pnl_cents - ml.total_fees_cents for ml in self.ledgers.values()) / 100
