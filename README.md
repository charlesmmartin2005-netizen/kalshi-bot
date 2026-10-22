# Kalshi trading bot

A paper-trading bot for Kalshi with three strategies, a scanner that covers
every open market, and a local dashboard. It defaults to **paper trading**:
no orders are sent, and fills are simulated from each market's real public
trade tape and recorded in `fills_log.jsonl`.

## Strategies

| Strategy | Where it runs | Where the edge comes from |
|---|---|---|
| **Fair value** | BTC daily ladder more than 2h from close | A pricing model calibrated every 60s to the market's own strikes. It agrees with the market at rest and reprices the instant spot moves, while resting Kalshi quotes lag. It takes the book when the gap is ≥ `take_edge_cents` after the taker fee. |
| **Book** (spread capture) | Any market with a 5–15¢ spread and real volume | Quotes 1¢ inside each side. The edge is theoretical: one-way order flow can erase it. |
| **Arbitrage** (`arb.py`) | Every mutually-exclusive event and strike ladder | Locked-in payoffs. Detection only; not auto-executed. |

Since 2026-10-20 the fair-value strategy runs as **rolling ladder + delta
lean** (backtest and comparison in `research/README.md`):
- **Rolling ladder:** auto-pick watches the 6 BTC strikes nearest the money. Auto-rotate (on by default) re-centres them every 15 minutes. A market rolled off with a position goes reduce-only until it's flat.
- **Size:** 15-contract quotes and takes, up to 75 per market, $450 total exposure.
- **Delta lean:** the bot sums the dollars its whole book gains or loses per 1% coin move, and leans *resting quotes* (not takes) against it, by up to 4¢ at $30/1%.

Since 2026-10-22 (research/README.md): **BTC only** (ETH dropped), **resting
quotes at least 6¢ from fair value** (was ~3.75¢ at 50¢), and **makers pay
no fee**, matching Kalshi's fee schedule for these series. Quotes that
unwind a position never cross the model (skew and lean used to pull them
to fair value and beyond), and **takes stop at $50 of coin exposure per 1%
move** either way. Tests on
sub-second data found the edge is in taking big gaps fast; quotes near fair
value were picked off about as often as they earned.

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
.venv\Scripts\python.exe research\paper_markouts.py --since 2026-10-22   # is the paper edge real?
```

Paper mode needs no API key. To trade live you need a key in `.env`, and
you should read "Before going live" first.

## Run unattended

A scheduled task, **Kalshi bot**, runs `run-bot.ps1` hidden at every logon.
That script starts `dashboard.py --autostart --no-browser` (trading starts by
itself) and starts it again 10 seconds after it exits for any reason. Open
positions come back from `fills_log.jsonl`, so a restart is safe. The
dashboard is still at http://127.0.0.1:8050; double-clicking
`start-dashboard.bat` while the bot runs just opens it, because a second
dashboard won't start on a taken port.

```
powershell -ExecutionPolicy Bypass -File stop-bot.ps1     # stop (until next logon)
powershell -Command "Start-ScheduledTask -TaskName 'Kalshi bot'"   # start again now
powershell -Command "Unregister-ScheduledTask -TaskName 'Kalshi bot' -Confirm:$false"   # remove the task
```

Logs: `logs\bot.log` (everything the dashboard shows, 5 MB × 4 files),
`logs\launcher.log` (each start and exit), `logs\crash.log` (Python errors).

The bot stops while the PC sleeps: set Settings → System → Power & battery →
"Make my device sleep after" (plugged in) to Never. After a reboot it comes
back when you log in. The kill switch still holds: a halt (button or loss
limit) is saved to `halted.txt`, so the bot stays halted across restarts
until you reset it on the dashboard.

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
| `dashboard.py` + `static/index.html` | Local web dashboard: Overview (today, settlement payout), Ladder, Edge (markouts vs history), Activity (ledger, settings, log), Scanner |
| `markouts.py` | Scores fills by how the market moved 1, 5 and 60 minutes later (Edge view, `research/paper_markouts.py`) |
| `run-bot.ps1` / `stop-bot.ps1` | Keep the dashboard running unattended (restart loop) / stop it |
| `autopick.py` | Chooses the strikes to watch (Auto-pick button / auto-rotate) |
| `research/strategy_lab.py` | Strategy comparison over cached months of real data (not used by the bot) |
| `research/paper_markouts.py` | Scores every paper fill by how the market moved 1, 5 and 60 minutes later, net of fees |
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
  sells, so part of the result may be trend rather than edge. *Superseded:*
  these maker fills were matched to trades that happened before the quote's
  price snapshot (fixed 2026-10-22).

## What testing showed (2026-10-22)

Details and numbers in `research/README.md`.
- **The earlier backtests looked ahead.** `strategy_lab.py` and
  `backtest.py` filled resting quotes against trades from before the data
  the quotes were priced from. Fixed, the old live settings went from
  +$842 a month to anywhere between −$694 and +$1,152 depending on how fast
  quotes really follow Kalshi's book, which 1-minute history can't show.
- **The edge is speed on big gaps.** Against every real Kalshi trade, with
  the model priced from Binance 1-second data 1s earlier: taking prices the
  model calls 6–8¢ cheap after the fee made +1.4 to +2.8¢ per contract 5
  minutes later in both months; 4–6¢ gaps made +0.4 to +1.3¢; smaller gaps
  lost. The edge halves within 1 second and is nearly gone after 10.
- **Resting quotes near fair value are about breakeven**, and in paper
  trading they've been picked off. BTC only with quotes 6¢ from fair value
  was the one setup positive in all six tests (two months × three fill
  models): +$20 to +$273 a month. In the lab's harshest model (quotes
  frozen for a whole minute) it still loses $179–341 a month, against
  $1,616–2,118 for the old settings.
- **Makers pay no fee** on KXBTCD/KXETHD (kalshi.com/fee-schedule); the bot
  had charged them the taker rate.

## Before going live

- `create_order` targets Kalshi's V2 order endpoint, and its schema has
  **not** been verified against a real response. Test with one contract.
- Live fills are not recorded yet (`TODO(live)` in `market_maker.py`).
  Paper fills are.
- Maker fills are free on KXBTCD/KXETHD (fee schedule checked 2026-10-22).
  Series on Kalshi's maker-fee list charge 0.0175; set
  `KALSHI_MAKER_FEE_RATE` if you trade one.
- Judge paper trading with `research/paper_markouts.py`, not daily P&L:
  takes should beat the fee 5 minutes later and maker fills shouldn't lose.
  All strikes settle on one BTC price a day, so P&L needs months to mean
  anything; markouts need about a week.
- Kalshi's websocket needs an API key. Without one the bot polls the book
  every 0.4s, and the take edge halves within a second.
- Paper-trade the fair-value strategy for a while first. Its edge is speed,
  which hourly history can't backtest.
- The Kalshi websocket client (`feeds.KalshiStream`) has never been run,
  because there was no API key when it was written. Watch "Kalshi data" on
  the dashboard after adding a key. If the stream fails, the bot falls back
  to REST.
- Repricing on every tick means many cancel/replace orders. Check Kalshi's
  write rate limit, and add a minimum time between requotes per market,
  before sending real orders.
