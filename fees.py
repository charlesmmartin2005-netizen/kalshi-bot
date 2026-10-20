"""
Kalshi trading fees. Replaces the old flat 1¢/contract placeholder.

Kalshi lists these series with fee_type "quadratic": the fee per order is

    ceil_to_cent( rate * contracts * P * (1 - P) )      P = price in dollars

so it's largest at 50¢ (1.75¢/contract at rate 0.07) and near zero at the
extremes. Resting (maker) orders may be charged a lower rate than taking on
some markets. VERIFY both rates on kalshi.com/fee-schedule — both default to
the taker rate here, which overstates maker fees rather than understating
them, so paper P&L errs on the pessimistic side.
"""
import math
import os

TAKER_RATE = float(os.getenv("KALSHI_TAKER_FEE_RATE", "0.07"))
MAKER_RATE = float(os.getenv("KALSHI_MAKER_FEE_RATE", "0.07"))


def order_fee_cents(price_cents: float, count: float, maker: bool = True) -> int:
    """Fee for one order (whole cents, rounded up like Kalshi does)."""
    p = min(max(price_cents, 0), 100) / 100
    rate = MAKER_RATE if maker else TAKER_RATE
    return math.ceil(round(rate * count * p * (1 - p) * 100, 9))


def per_contract_fee_cents(price_cents: float, maker: bool = True) -> float:
    """Average fee per contract at a given price, for edge calculations
    (no per-order rounding)."""
    p = min(max(price_cents, 0), 100) / 100
    return (MAKER_RATE if maker else TAKER_RATE) * p * (1 - p) * 100
