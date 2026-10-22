"""
Central configuration for the Kalshi market-making bot.

All risk limits live here on purpose — if you're tuning risk, this is the
one file you should need to touch. Nothing in market_maker.py or
risk_manager.py hardcodes numbers; they all read from here.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class KalshiConfig:
    # --- Auth ---
    # Your Kalshi API key ID (from your account's API key settings) and the
    # path to the PEM-encoded RSA private key file you generated alongside it.
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")

    # --- Endpoints ---
    # Verify these against Kalshi's current docs before going live — API
    # hosts have moved before (elections.kalshi.com vs trading-api.kalshi.com).
    base_url: str = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
    ws_url: str = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")

    # --- Mode ---
    # PAPER = simulate fills against live market data, never send real orders.
    # LIVE = real orders, real money. Always start in PAPER.
    paper_trading: bool = os.getenv("KALSHI_PAPER_TRADING", "true").lower() == "true"


@dataclass
class RiskConfig:
    # Sizes and limits below are the "rolling ladder + delta lean" strategy
    # (research/README.md): 3x the original size. Its first backtest
    # (+$498 / +$548 a month) matched trades to quotes priced after those
    # trades; with that fixed, see research/README.md (2026-10-22).

    # --- Position limits ---
    # Max contracts held (net) in any single market at once.
    max_position_per_market: int = 75
    # Max total capital (in dollars) deployed across all open positions.
    # Backtests peaked at ~$520; capped below the $500 paper balance.
    max_total_exposure_usd: float = 450.0

    # --- Loss limits (circuit breakers) ---
    # Bot halts and requires manual restart if realized losses in a session
    # exceed this. This is your "how bad can today go" number. (Worst
    # backtest day: −$44.)
    max_daily_loss_usd: float = 150.0
    # A market goes reduce-only if its loss exceeds this.
    max_single_market_loss_usd: float = 60.0

    # --- Quoting parameters ---
    # Minimum edge (in cents, per contract) required after estimated fees
    # before the bot will post a quote at all. This is the number that
    # determines whether you're actually profitable net of fees.
    # 6¢ (was 2¢ + a maker fee Kalshi doesn't charge, ~3.75¢ at 50¢): resting
    # quotes near fair value are picked off about as often as they earn, so
    # only wide ones pay. With BTC only, 6¢ was the one setting positive in
    # all six 2026-10-22 tests (two months x three fill models,
    # research/README.md); 2¢ ranged from -$694 to +$1,152 a month.
    min_edge_cents: float = 6.0
    # How many cents inside the current best bid/ask to place your quote.
    quote_inset_cents: int = 1
    # Max contracts to quote at a time per side.
    quote_size: int = 15

    # --- Inventory management ---
    # How far (cents) both quotes shift against an open position when it's
    # at the full max_position_per_market — scaled linearly below that. Long
    # YES -> quotes move down (sell more eagerly, buy less), and vice versa.
    # This is what keeps one-way order flow from parking the bot at its
    # position limit (the #1 failure in the Fed-market simulation).
    inventory_skew_cents: float = 3.0
    # Skew and delta lean pull the quote that shrinks a position toward fair
    # value; this keeps it at least this far (cents, after fee) from the raw
    # model. None = no floor. 0 = never pay more than the model to unwind:
    # in the 2026-10-22 tests (two months x three fill models) it nudged the
    # worst month from +$20 to +$27. 2c and 4c floors helped Sep-Oct but lost
    # Aug-Sep (-$19, -$100): unwinding slower rides more into settlement.
    min_reduce_edge_cents: float | None = 0.0
    # Delta lean: all BTC (or ETH) strikes are one bet on the coin, so the
    # bot adds up how many dollars the whole book gains/loses per 1% move
    # and leans RESTING quotes (never takes) against it — up to
    # delta_lean_cents on the most price-sensitive strike, reached at
    # delta_lean_ref_usd per 1%. Positions then drain through maker fills
    # instead of riding to settlement. Backtest: same profit, ~1/3 the daily
    # swing. Stronger leans (6¢+ at $8-15) lost money — keep it gentle.
    # 0 disables.
    delta_lean_cents: float = 4.0
    delta_lean_ref_usd: float = 30.0

    # --- Fair-value trading (markets with a pricing model: crypto, NYC temp) ---
    # Cross the spread (take) when the book is mispriced versus the model by
    # at least this many cents per contract AFTER the taker fee. The crypto
    # model is calibrated to each ladder and sits within ~1¢ of the market at
    # rest, so a 4¢ gap means the book is lagging a spot move.
    enable_taking: bool = True
    take_edge_cents: float = 4.0
    # Contracts per take order.
    take_size: int = 15
    # Takes all point one way in a trending move (2026-10-22: 94% sells into
    # a BTC drop, book at -$98 per 1% by 3pm), so cap how far they can push
    # the book's coin exposure: no take past this many $ per 1% move, either
    # way. Takes that reduce exposure are never blocked. None = no cap. $50
    # cost nothing in the 2026-10-22 tests (mean +$143 vs +$145 a month);
    # the 1-minute backtest can't show the fast take bursts it's aimed at.
    max_take_book_delta_usd: float | None = 50.0

    # --- Timing / staleness controls ---
    # Cancel and re-quote if a resting order has been open longer than this
    # (seconds) — protects against adverse selection on stale quotes.
    max_quote_age_seconds: float = 15.0
    # If the market's best bid/ask moves by more than this many cents since
    # your quote was placed, cancel immediately rather than wait for the
    # staleness timer.
    requote_on_move_cents: int = 2

    # --- Kill switch ---
    # Consecutive API errors before the bot halts entirely.
    max_consecutive_errors: int = 5
    # Markets to avoid market-making in (e.g. approaching resolution, where
    # informed order flow spikes). Populate with tickers as needed.
    excluded_tickers: list = field(default_factory=list)


@dataclass
class BotConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    # Tickers this bot instance will actively make markets on. Start with
    # one or two liquid markets while validating the strategy. Set them in
    # .env as a comma-separated list, or add/remove them live from the
    # dashboard.
    watched_tickers: list = field(default_factory=lambda: [
        t.strip() for t in os.getenv("KALSHI_WATCHED_TICKERS", "").split(",") if t.strip()
    ])
    # (Repricing is event-driven — every coin tick or Kalshi book change.
    # Timings live at the top of engine.py.)


CONFIG = BotConfig()
