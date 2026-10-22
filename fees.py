"""
Kalshi trading fees. Replaces the old flat 1¢/contract placeholder.

Kalshi lists these series with fee_type "quadratic": the fee per order is

    ceil_to_cent( rate * contracts * P * (1 - P) )      P = price in dollars

so it's largest at 50¢ (1.75¢/contract at rate 0.07) and near zero at the
extremes. Resting (maker) fills are free on "most markets": checked against
kalshi.com/fee-schedule on 2026-10-22, KXBTCD and KXETHD are not among the
158 series that charge makers (those pay 0.0175, a quarter of the taker
rate). Before, maker fills were charged the taker rate, which overstated
paper fees by ~$600/month at backtest volumes. Set KALSHI_MAKER_FEE_RATE
for a series that does charge makers.
"""
import math
import os

TAKER_RATE = float(os.getenv("KALSHI_TAKER_FEE_RATE", "0.07"))
MAKER_RATE = float(os.getenv("KALSHI_MAKER_FEE_RATE", "0.0"))


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
