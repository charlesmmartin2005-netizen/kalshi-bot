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
    # Checked 2026-11-04: Kalshi recommends external-api.kalshi.com and
    # external-api-ws.kalshi.com, and still supports these. Set KALSHI_BASE_URL
    # and KALSHI_WS_URL to use the demo environment (README, "Going live").
    base_url: str = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
    ws_url: str = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
    # Our own fills. Empty = the same host as ws_url.
    fills_ws_url: str = os.getenv("KALSHI_FILLS_WS_URL", "")

    # --- Mode ---
    # PAPER = simulate fills against live market data, never send real orders.
    # LIVE = real orders, real money, and live_risk()'s limits. Always start in
    # PAPER (README, "Going live").
    paper_trading: bool = os.getenv("KALSHI_PAPER_TRADING", "true").lower() == "true"
    # What went into the account, so the dashboard can show profit since then.
    # Update it if you deposit or withdraw.
    live_starting_balance_usd: float = float(os.getenv("KALSHI_LIVE_START_USD", "50"))

    @property
    def mode_name(self) -> str:
        """How the fill ledger labels this mode's rows."""
        return "paper" if self.paper_trading else "live"


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
    # 8¢ since 2026-10-29, together with max_take_hours and the 3h model
    # stop (fair_value.CRYPTO_MIN_HOURS_TO_TRADE): the three made +$45 to
    # +$248 a month more than before under all four fill models, better in
    # both months. On its own 8¢ helped only under the harsh fill models, and
    # all three were picked on the same two months, so judge it live.
    min_edge_cents: float = 8.0
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
    # No takes earlier than this many hours before close (for the daily
    # markets: none from the 5pm CT open until ~2am). Far from the close a
    # model gap is more often the model being wrong than the book lagging:
    # paper takes with >14h left made +0.2¢ a contract a minute later after
    # the fee, those within 14h +3.6¢ (10-22..10-28), and blocking them made
    # $25-65 a month more in both months under all four fill models
    # (research/README.md, 2026-10-29). 14-18h all did; 8-12h cut good takes.
    # None = take any time the model is tradeable.
    max_take_hours: float | None = 14.0

    # --- Timing / staleness controls ---
    # Cancel and re-quote if a resting order has been open longer than this
    # (seconds) — protects against adverse selection on stale quotes.
    max_quote_age_seconds: float = 15.0
    # Live only: a quote stays where it is until the price it should be at has
    # moved this many cents (or it hits max_quote_age_seconds). Following every
    # cent was 5 replacements a second, 56 in the busiest second (2026-11-04
    # log), against Kalshi's roughly 8 a second. Paper still follows every cent.
    requote_on_move_cents: int = 2

    # --- Kill switch ---
    # Consecutive API errors before the bot halts entirely.
    max_consecutive_errors: int = 5
    # Markets to avoid market-making in (e.g. approaching resolution, where
    # informed order flow spikes). Populate with tickers as needed.
    excluded_tickers: list = field(default_factory=list)


# Live quotes expire on Kalshi after market_maker.QUOTE_EXPIRY_SECONDS (60), so
# they are replaced before then even when the price hasn't moved. This leaves
# 20 seconds for the replacement to go out.
LIVE_QUOTE_REFRESH_SECONDS = 40.0


def live_risk(scale: float | None = None) -> RiskConfig:
    """The limits for real money, sized for a $50 account: everything scales by
    2/15, 2 contracts where paper trades 15. Total exposure is held below the
    balance instead of scaling to $60. The daily loss limit is the number to
    think about: $20 is 40% of the account. Raise these only together with the
    balance. Paper keeps RiskConfig's own numbers, so paper results stay
    comparable with what came before.

    scale (KALSHI_LIVE_SIZE_SCALE in .env, 1 if unset) multiplies every size
    and dollar limit together, so they keep their proportions: 1.5 is 3
    contracts, $60 of exposure and a $30 daily loss limit. The bot never
    changes it by itself. On 2026-11-05 the account hit the $40 exposure limit
    and the 10-contract position limit, and in paper 35 of 64 takes found 15
    or more contracts at the price.

    A live quote is replaced at 40s old, not paper's 15: the bot already moves
    it whenever its price is off by requote_on_move_cents, and 60% of 102,596
    orders in 39 hours (2026-11-10..12) replaced a quote at the same price."""
    k = float(os.getenv("KALSHI_LIVE_SIZE_SCALE", "1")) if scale is None else scale
    if not 0 < k <= 10:
        raise ValueError(f"KALSHI_LIVE_SIZE_SCALE must be above 0 and at most 10, not {k}")
    return RiskConfig(
        max_position_per_market=max(1, round(10 * k)),
        max_total_exposure_usd=40.0 * k,
        max_daily_loss_usd=20.0 * k,
        max_single_market_loss_usd=8.0 * k,
        quote_size=max(1, round(2 * k)),
        take_size=max(1, round(2 * k)),
        delta_lean_ref_usd=4.0 * k,
        max_take_book_delta_usd=round(6.67 * k, 2),
        max_quote_age_seconds=LIVE_QUOTE_REFRESH_SECONDS,
    )


@dataclass
class BotConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    risk: RiskConfig | None = None         # None = live_risk() when live, RiskConfig() on paper
    # Tickers this bot instance will actively make markets on. Start with
    # one or two liquid markets while validating the strategy. Set them in
    # .env as a comma-separated list, or add/remove them live from the
    # dashboard.
    watched_tickers: list = field(default_factory=lambda: [
        t.strip() for t in os.getenv("KALSHI_WATCHED_TICKERS", "").split(",") if t.strip()
    ])
    # (Repricing is event-driven — every coin tick or Kalshi book change.
    # Timings live at the top of engine.py.)

    def __post_init__(self):
        if self.risk is None:
            self.risk = RiskConfig() if self.kalshi.paper_trading else live_risk()


CONFIG = BotConfig()
