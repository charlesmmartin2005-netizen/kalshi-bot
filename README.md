# Kalshi trading bot

A paper-trading bot for Kalshi with three strategies, a scanner that covers
every open market, and a local dashboard. It defaults to **paper trading**:
no orders are sent, and fills are simulated from each market's real public
trade tape and recorded in `fills_log.jsonl`.

## Strategies

| Strategy | Where it runs | Where the edge comes from |
|---|---|---|
| **Fair value** | Crypto ladders (BTC, ETH) more than 2h from close | A pricing model calibrated every 60s to the market's own strikes. It agrees with the market at rest and reprices the instant spot moves, while resting Kalshi quotes lag. It takes the book when the gap is ≥ `take_edge_cents` after the taker fee. |
| **Book** (spread capture) | Any market with a 5–15¢ spread and real volume | Quotes 1¢ inside each side. The edge is theoretical: one-way order flow can erase it. |
| **Arbitrage** (`arb.py`) | Every mutually-exclusive event and strike ladder | Locked-in payoffs. Detection only; not auto-executed. |

Since 2026-10-20 the fair-value strategy runs as **rolling ladder + delta
lean** (backtest and comparison in `research/README.md`):
- **Rolling ladder:** auto-pick watches the 6 BTC + 3 ETH strikes nearest the money. Auto-rotate (on by default) re-centres them every 15 minutes. A market rolled off with a position goes reduce-only until it's flat.
- **Size:** 15-contract quotes and takes, up to 75 per market, $450 total exposure.
- **Delta lean:** the bot sums the dollars its whole book gains or loses per 1% coin move, and leans *resting quotes* (not takes) against it, by up to 4¢ at $30/1%.

All strategies share the same risk rules:
- **Inventory skew:** quotes shift against an open position.
- **Reduce-only at the limit:** at a position, exposure or per-market loss limit, the side that shrinks the position stays live.
- **Exit quotes:** when the spread is too tight to make money but a position is open, the reducing side still joins the best price so the bot can get out.
- **Daily-loss kill switch.**
- **Price-dependent fees:** Kalshi's formula, in `fees.py`.

## Run it

Double-click `start-dashboard.bat`, or:

```
.venv\Scripts\python.exe dashboard.py     # dashboard → http://127.0.0.1:8050
.venv\Scripts\python.exe main.py          # terminal only (needs KALSHI_WATCHED_TICKERS)
.venv\Scripts\python.exe scanner.py       # rank every active market by strategy
.venv\Scripts\python.exe arb.py           # look for arbitrage right now
.venv\Scripts\python.exe simulate.py T1 T2 --days 14   # old vs new rules, 2,000 futures
.venv\Scripts\python.exe show_pnl.py      # P&L report from the ledger
```

Paper mode needs no API key. To trade live you need a key in `.env`, and
you should read "Before going live" first.

## Files

| File | Role |
|---|---|
| `config.py` | Every tunable number and risk limit |
| `strategy.py` | Pure quoting logic, shared by the bot and the simulator |
| `fair_value.py` | Crypto model (tradeable) and NWS temperature model (display only) |
| `fees.py` | Kalshi's quadratic fee formula |
| `risk_manager.py` | Allowed sides, loss breakers, kill switch |
| `market_maker.py` | Order sync, takes, and paper fills from the trade tape |
| `engine.py` | The main loop (shared by `main.py` and the dashboard) |
| `catalog.py` | One cached sweep of every open event (~13k events, ~15s) |
| `scanner.py` / `arb.py` | Market ranking / arbitrage detection over the catalog |
| `simulate.py` | Monte Carlo of the old vs new rules on resampled real history |
| `backtest.py` | Minute-by-minute replay of the fair-value strategy on settled BTC daily markets, with markouts |
| `ledger.py` / `pnl.py` | Fill log and average-cost, settlement-aware P&L |
| `kalshi_client.py` | REST client, request throttle (10/s) and retry on 429 |
| `feeds.py` | Streaming BTC/ETH index (Coinbase, Kraken, Bitstamp, Gemini websockets) and the Kalshi websocket (needs an API key) |
| `dashboard.py` + `static/index.html` | Local web dashboard |
| `autopick.py` | Chooses the strikes to watch (Auto-pick button / auto-rotate) |
| `research/strategy_lab.py` | Strategy comparison over cached months of real data (not used by the bot) |
| `research/index_check.py` | Exchanges vs Kalshi's real settlement values |
| `research/late_window.py` | Is the model tradeable in the last 2h? (no) |

## Market data (2026-10-20)

- **Event-driven repricing.** The bot wakes on every coin tick or Kalshi book change and re-quotes every market from the latest data in about 20ms. Before, it polled every 1–3 seconds.
- **Coin price is a streaming index:** the median of the Coinbase, Kraken, Bitstamp and Gemini websocket mids, no login needed. Across 124 real settlements it matched Kalshi's settlement value (`expiration_value`, a 60-second average of CF Benchmarks' RTI) about 20% closer than Coinbase alone: 0.58 vs 0.73bp for BTC. The 60-second averaging window is in the model too.
- **Kalshi data.** Kalshi's websocket needs an API key (it returns 401 without one).
  - **With a key,** books and the trade tape stream live.
  - **Without a key,** the bot polls REST at ~8 requests/s: one batched request for every market's best bid/ask every 0.4s, trade tapes every 2s, and depth every 10s.
- **The last 2 hours stay off-limits** (`research/late_window.py`). Even with the index and the averaging window, the market mid beat the model in every bucket in both months, and taking against the book lost money in nearly every bucket.

## What testing showed (2026-10-19)

- **Crypto model:** within ~0.6–0.95¢ of the market on average across BTC
  and ETH ladders after the volatility fit. Uncalibrated, it was 2–4¢ off in a
  consistent pattern, which would have made the bot a volatility bettor.
- **Weather model:** 13–39¢ off. The market clearly has better data, so the
  model is display-only.
- **Arbitrage:** zero real opportunities across 13,149 events. The first
  version of the checker found 39 that weren't real: same-strike markets with
  different deadlines, per-team over/unders, and candidate lists missing an
  "other" option. Synthetic tests confirm it catches real setups.
- **New vs old rules** (Fed markets, 2,000 two-week futures): time quoting
  36% → 85%, worst case −$6.27 → −$4.38, typical drawdown $3.95 → $2.69.
  Still slightly negative on average, because those markets have no edge.

- **Fair-value backtest** (`backtest.py`, 15 settled BTC 5pm markets,
  Oct 5–19, 4 strikes nearest the money): +$37.47 total, +$2.50/day, 9 up
  and 6 down days, max drawdown $31.77. Takes moved in the bot's favor by
  +1.5¢ after 5 minutes and +4.1¢ after 60 (n=54); maker fills by +2.3¢
  after 5 minutes (n=856). BTC fell ~5% over the period and most takes were
  sells, so part of the result may be trend rather than edge.

## Before going live

- `create_order` targets Kalshi's V2 order endpoint, and its schema has
  **not** been verified against a real response. Test with one contract.
- Live fills are not recorded yet (`TODO(live)` in `market_maker.py`).
  Paper fills are.
- Fees assume the taker rate for maker fills too. Check the real maker rate
  at kalshi.com/fee-schedule and set `KALSHI_MAKER_FEE_RATE`.
- Paper-trade the fair-value strategy for a while first. Its edge is speed,
  which hourly history can't backtest.
- The Kalshi websocket client (`feeds.KalshiStream`) has never been run,
  because there was no API key when it was written. Watch "Kalshi data" on
  the dashboard after adding a key. If the stream fails, the bot falls back
  to REST.
- Repricing on every tick means many cancel/replace orders. Check Kalshi's
  write rate limit, and add a minimum time between requotes per market,
  before sending real orders.
