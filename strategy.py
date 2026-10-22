"""
Pure quoting logic: given a book, a position and (optionally) a fair value,
decide what to bid, what to offer, and whether to take. No API calls and no
state, so the live engine and simulate.py run the exact same rules.

Two modes:

  FAIR VALUE (a tradeable model exists — crypto >2h from close)
    Quotes sit around the model price, not the book: buy at least
    fee + min_edge below fair value, sell at least that far above. If the
    book's own touch is better for us, improve it by `quote_inset_cents`
    instead. Takes the book outright when it's mispriced vs the model by
    `take_edge_cents` after the taker fee, up to the size shown at the touch.

  BOOK (no model — e.g. Fed markets)
    The original strategy: 1¢ inside each side when the spread clears
    2x maker fee + min_edge.

In both modes:
  - Inventory skew: both quotes shift against the current position, scaled
    to `inventory_skew_cents` at the position limit, so the bot leans toward
    getting flat instead of piling into one-way flow. The reducing side
    still stays `min_reduce_edge_cents` from the raw model.
  - Reduce-only: sides that would grow the position are dropped when the
    risk manager says so (position/exposure limit, per-market loss breach),
    but the side that shrinks it stays live — the old bot cancelled both and
    froze holding the position.
  - Exit quote: if the spread is too tight to market-make but a position is
    open, the reducing side still joins the touch so the bot can get out.
"""
import math
from dataclasses import dataclass, field

from fees import per_contract_fee_cents


@dataclass
class Plan:
    buy: tuple[int, int] | None = None     # (price_cents, size) resting bid
    sell: tuple[int, int] | None = None    # (price_cents, size) resting ask
    takes: list = field(default_factory=list)   # [(action, price_cents, size)]
    mode: str = "idle"                     # "fair-value" | "book" | "exit" | "idle"
    reason: str = ""
    skew: float = 0.0


def _clamp(p):
    return int(min(99, max(1, p)))


def _shown(book, key):
    """Whole contracts shown at the touch, or no limit when the book doesn't
    say (simulate.py / backtest.py books carry prices only)."""
    size = book.get(key)
    return math.inf if size is None else int(size)


def plan_quotes(book: dict, position: int, cfg, allowed: set, fv=None, anchor=None) -> Plan:
    """
    book:     normalized book (yes_bid / yes_ask in cents, either may be None)
    position: net YES contracts (+ long YES, − long NO)
    cfg:      RiskConfig
    allowed:  {"buy", "sell"} subset from RiskManager.allowed_sides
    fv:       FairValue or None (for resting quotes it may include the delta lean)
    anchor:   the raw model in cents, without the lean (defaults to fv.cents);
              the unwind floor is measured from it
    """
    bid, ask = book.get("yes_bid"), book.get("yes_ask")
    plan = Plan(skew=-(position / cfg.max_position_per_market) * cfg.inventory_skew_cents)
    if not allowed:
        plan.reason = "no sides allowed by risk manager"
        return plan

    buy_room = cfg.max_position_per_market - position     # contracts we may still buy
    sell_room = cfg.max_position_per_market + position    # contracts we may still sell
    buy_size = min(cfg.quote_size, buy_room) if "buy" in allowed else 0
    sell_size = min(cfg.quote_size, sell_room) if "sell" in allowed else 0

    if fv is not None and fv.tradeable:
        plan.mode = "fair-value"
        f = fv.cents
        # -- take when the book is clearly wrong vs the model, never more
        #    than the touch shows
        if cfg.enable_taking:
            if ask is not None and buy_size > 0:
                edge = f - ask - per_contract_fee_cents(ask, maker=False)
                n = min(cfg.take_size, buy_size, _shown(book, "yes_ask_size"))
                if edge >= cfg.take_edge_cents and n > 0:
                    plan.takes.append(("buy", ask, n))
                    buy_size -= n
            if bid is not None and sell_size > 0:
                edge = bid - f - per_contract_fee_cents(bid, maker=False)
                n = min(cfg.take_size, sell_size, _shown(book, "yes_bid_size"))
                if edge >= cfg.take_edge_cents and n > 0:
                    plan.takes.append(("sell", bid, n))
                    sell_size -= n
        # -- rest quotes around (skewed) fair value
        center = f + plan.skew
        need = per_contract_fee_cents(center) + cfg.min_edge_cents
        buy_px, sell_px = math.floor(center - need), math.ceil(center + need)
        # The side that shrinks the position may lean toward fair value to
        # unwind (skew, delta lean), but never closer than
        # min_reduce_edge_cents to the raw model: on 2026-10-22 unwind bids
        # pulled up to fair value were picked off for -4.5¢ a contract.
        if cfg.min_reduce_edge_cents is not None and position:
            a = f if anchor is None else anchor
            floor = per_contract_fee_cents(a) + cfg.min_reduce_edge_cents
            if position < 0:
                buy_px = min(buy_px, math.floor(a - floor))
            else:
                sell_px = max(sell_px, math.ceil(a + floor))
        if bid is not None:
            buy_px = min(buy_px, bid + cfg.quote_inset_cents)
            sell_px = max(sell_px, bid + 1)
        if ask is not None:
            sell_px = max(sell_px, ask - cfg.quote_inset_cents)
            buy_px = min(buy_px, ask - 1)
        if buy_size > 0 and 1 <= buy_px <= 99:
            plan.buy = (_clamp(buy_px), buy_size)
        if sell_size > 0 and 1 <= sell_px <= 99:
            plan.sell = (_clamp(sell_px), sell_size)
        plan.reason = f"fair value {f:.1f}¢"
        return plan

    # -- book mode
    if bid is None or ask is None:
        plan.reason = "one-sided book"
        return plan
    spread = ask - bid
    mid = (bid + ask) / 2
    min_spread = 2 * per_contract_fee_cents(mid) + cfg.min_edge_cents
    shift = round(plan.skew)
    if spread >= min_spread:
        plan.mode = "book"
        buy_px = min(bid + cfg.quote_inset_cents + shift, ask - 1)
        sell_px = max(ask - cfg.quote_inset_cents + shift, bid + 1)
        if buy_px < sell_px:
            if buy_size > 0:
                plan.buy = (_clamp(buy_px), buy_size)
            if sell_size > 0:
                plan.sell = (_clamp(sell_px), sell_size)
        plan.reason = f"spread {spread}¢ ≥ {min_spread:.1f}¢"
    elif position != 0:
        # Too tight to make money, but holding inventory: join the touch on
        # the reducing side so the position can still be worked out.
        plan.mode = "exit"
        if position > 0 and sell_size > 0:
            plan.sell = (_clamp(ask), min(sell_size, position))
        elif position < 0 and buy_size > 0:
            plan.buy = (_clamp(bid), min(buy_size, -position))
        plan.reason = f"spread {spread}¢ too tight — exit only"
    else:
        plan.reason = f"spread {spread}¢ < {min_spread:.1f}¢"
    return plan
