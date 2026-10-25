# Kalshi Market-Making Bot — Starter Scaffold

> [!NOTE]
> Archived. This describes the bot's first scaffold. The current bot, its files and its settings
> are in the [main README](../README.md); some files and settings named below no longer exist or have moved.

A starting scaffold for a Kalshi market-making bot: quote inside the
spread on watched markets, capture the edge, with hard risk limits and a
paper-trading mode built in from the start.

**This is a scaffold, not a finished profitable bot.** It compiles and the
logic is sound in shape, but it has not been run against Kalshi's live API
in this environment (no network access here), and several pieces are
explicitly marked as simplified placeholders you need to replace:

- **Fee estimate** in `market_maker.py` (`ESTIMATED_FEE_CENTS_PER_CONTRACT`)
  — replace with Kalshi's actual current fee schedule.
- **Position/cost-basis accounting** in `risk_manager.py` — the current
  `record_fill` logic is simplified (last-price cost basis, not proper
  VWAP). Fine for a first paper-trading pass, not fine for real P&L
  tracking.
- **Fill simulation** in `market_maker.py` (`simulate_fills`) — assumes a
  fill whenever the opposing price crosses your quote, which ignores queue
  position. Real fills will be less frequent than paper fills. Treat paper
  P&L as an optimistic upper bound.
- **Auth signing payload** in `auth.py` — verify the exact
  timestamp+method+path signing format against Kalshi's current docs
  before going live. Signing bugs fail closed (401s), so this is safe to
  debug in paper mode.
- **Endpoint URLs** in `config.py` — Kalshi's API host has moved before;
  confirm the current base URL and WebSocket URL in their docs.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your API key ID and private key path
```

Generate an API key pair from your Kalshi account settings — this gives
you a key ID and a private key file (`.pem`). Point
`KALSHI_PRIVATE_KEY_PATH` at that file.

In `main.py` or wherever you instantiate `CONFIG`, set
`CONFIG.watched_tickers` to one or two liquid markets to start:

```python
from config import CONFIG
CONFIG.watched_tickers = ["KXFED-26MAR19"]  # example — use a real, liquid ticker
```

## Running

```bash
python main.py
```

Defaults to **paper trading** (`KALSHI_PAPER_TRADING=true` in `.env`).
Nothing is sent to Kalshi in this mode — quotes and fills are logged and
simulated against live market data so you can see how the strategy
performs before risking capital.

## Recommended path to going live

1. Run in paper mode for several days across a few different markets.
   Look at `maker.paper_fills` and your logs — how often do quotes fill,
   at what net edge after the real fee schedule, how often do you get
   requoted out before filling at all.
2. Fix the placeholders listed above with real numbers.
3. Switch to live with tiny size (`quote_size = 1`, low
   `max_total_exposure_usd`) and watch it manually alongside the bot —
   don't walk away yet.
4. Scale up gradually only as the live behavior matches paper behavior.

## Files

| File | Purpose |
|---|---|
| `config.py` | All tunable parameters — risk limits, quoting behavior, endpoints |
| `auth.py` | RSA-PSS request signing |
| `kalshi_client.py` | REST client + order book normalization (yes/no → bid/ask) |
| `risk_manager.py` | Position limits, loss circuit breakers, kill switch |
| `market_maker.py` | Core quoting loop + paper-mode fill simulation |
| `main.py` | Entry point / main loop |

## Not included yet, worth adding next

- WebSocket integration for live order book updates and fill
  notifications (current version polls REST, which is fine for validating
  logic but too slow for real market making at scale).
- Proper VWAP-based cost basis and realized/unrealized P&L calculation.
- Persistent logging/storage of fills and positions across restarts.
- Automated intra-market arbitrage scanning (mutually-exclusive contract
  sets summing away from 100¢) as a second, complementary strategy.
