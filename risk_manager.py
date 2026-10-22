"""
All "should the bot be allowed to do X" decisions go through here. The
market-making logic should never check its own risk limits inline — it
asks RiskManager, which is the single source of truth and the thing you'd
audit if something ever goes wrong.

Changes from the first build:
  - Positions use proper average-cost accounting (pnl.MarketLedger) instead
    of "last fill price", and fees use Kalshi's formula.
  - Hitting a limit makes a market REDUCE-ONLY (the side that shrinks the
    position stays live) instead of cancelling both sides and freezing.
  - The loss breakers are actually wired up: update_marks() runs every
    cycle, marks open positions to the mid, halts the bot on the daily loss
    limit, and makes a market reduce-only on the single-market limit. In the
    first build both checks existed but nothing ever called them.
  - Positions survive a restart: restore() rebuilds them from the fill
    ledger. Before, a restarted bot believed it was flat (2026-10-21).
"""
import logging
from datetime import datetime, timezone

from config import RiskConfig
from pnl import MarketLedger

log = logging.getLogger("risk_manager")


class RiskManager:
    def __init__(self, risk_config: RiskConfig):
        self.cfg = risk_config
        self.positions: dict[str, MarketLedger] = {}   # ticker -> ledger (has .net_contracts)
        self.marks: dict[str, float] = {}              # ticker -> last mid, cents
        self.reduce_only: dict[str, str] = {}          # ticker -> reason
        self.halted: bool = False
        self.halt_reason: str = ""
        self._day = datetime.now(timezone.utc).date()
        self._day_start_pnl_usd = 0.0
        self.session_pnl_usd = 0.0
        self.daily_pnl_usd = 0.0

    # ---- state updates ------------------------------------------------

    def record_fill(self, ticker: str, side: str, action: str, count: int, price_cents: int,
                    maker: bool = True):
        pos = self.positions.setdefault(ticker, MarketLedger(ticker))
        pos.apply_fill(side, action, count, price_cents, maker)
        log.info("Fill recorded: %s %s %s %d @ %d¢ (%s) -> net position %d",
                 ticker, action, side, count, price_cents, "maker" if maker else "taker",
                 pos.net_contracts)

    def market_pnl_usd(self, ticker: str) -> float:
        """Realized − fees + open position marked to the last mid."""
        pos = self.positions.get(ticker)
        if not pos:
            return 0.0
        open_val = 0.0
        if pos.net_contracts and ticker in self.marks:
            open_val = pos.net_contracts * (self.marks[ticker] - pos.avg_cost_cents)
        return (pos.realized_pnl_cents - pos.total_fees_cents + open_val) / 100

    def update_marks(self, marks: dict[str, float]):
        """Call once per cycle with {ticker: mid_cents}. Runs the loss breakers."""
        self.marks.update({t: m for t, m in marks.items() if m is not None})
        today = datetime.now(timezone.utc).date()
        self.session_pnl_usd = sum(self.market_pnl_usd(t) for t in self.positions)
        if today != self._day:
            self._day, self._day_start_pnl_usd = today, self.session_pnl_usd
        self.daily_pnl_usd = self.session_pnl_usd - self._day_start_pnl_usd

        if not self.halted and self.daily_pnl_usd <= -self.cfg.max_daily_loss_usd:
            self._halt(f"Daily loss limit hit: ${self.daily_pnl_usd:.2f}")
        for t in self.positions:
            pnl = self.market_pnl_usd(t)
            if pnl <= -self.cfg.max_single_market_loss_usd and t not in self.reduce_only:
                self.reduce_only[t] = f"market loss ${pnl:.2f}"
                log.warning("Single-market loss limit on %s ($%.2f) — reduce-only", t, pnl)

    def restore(self, fills: list, mode: str, is_settled) -> dict[str, int]:
        """Rebuild open positions from the fill ledger after a restart, so
        limits, exposure, skew and exit quotes start from what the bot really
        holds instead of from flat. Replays `mode` fills ("paper" or "live")
        and keeps markets with an open position that haven't settled
        (is_settled(ticker) -> bool). Returns {ticker: net contracts}."""
        replay: dict[str, MarketLedger] = {}
        for f in fills:
            if f.get("mode", "paper") != mode:
                continue
            ml = replay.setdefault(f["ticker"], MarketLedger(f["ticker"]))
            ml.apply_fill(f["side"], f["action"], f["count"], f["price_cents"],
                          f.get("liquidity", "maker") == "maker")
        for t, ml in replay.items():
            if ml.net_contracts and not is_settled(t):
                self.positions[t] = ml
        return {t: ml.net_contracts for t, ml in self.positions.items() if ml.net_contracts}

    def record_api_error(self, consecutive_errors: int):
        if consecutive_errors >= self.cfg.max_consecutive_errors:
            self._halt(f"{consecutive_errors} consecutive API errors")

    def _halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        log.critical("KILL SWITCH TRIGGERED: %s", reason)

    # ---- pre-trade checks ------------------------------------------------

    def net(self, ticker: str) -> int:
        pos = self.positions.get(ticker)
        return pos.net_contracts if pos else 0

    def allowed_sides(self, ticker: str) -> tuple[set, str]:
        """Which order sides may be placed on this market right now, and why
        any were removed. A side that REDUCES the position is never blocked
        by a size/exposure/loss limit — only by a halt or exclusion."""
        if self.halted:
            return set(), f"bot halted: {self.halt_reason}"
        if ticker in self.cfg.excluded_tickers:
            return set(), "ticker excluded"
        net = self.net(ticker)
        reducing = {"sell"} if net > 0 else {"buy"} if net < 0 else set()
        if ticker in self.reduce_only:
            return reducing, f"reduce-only: {self.reduce_only[ticker]}"
        if abs(net) >= self.cfg.max_position_per_market:
            return reducing, f"position limit ({net}) — reduce-only"
        if self.total_exposure_usd() >= self.cfg.max_total_exposure_usd:
            return reducing, "total exposure limit — reduce-only"
        return {"buy", "sell"}, ""

    def can_quote(self, ticker: str) -> tuple[bool, str]:
        sides, why = self.allowed_sides(ticker)
        return bool(sides), why

    def total_exposure_usd(self) -> float:
        """Money at risk: cost of long YES, or cost of the NO side when short."""
        total = 0.0
        for pos in self.positions.values():
            n = pos.net_contracts
            if n > 0:
                total += n * pos.avg_cost_cents / 100
            elif n < 0:
                total += -n * (100 - pos.avg_cost_cents) / 100
        return total
