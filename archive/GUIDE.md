# Kalshi Market-Making Bot — Complete Reference Guide

Everything you can configure, what it does, and what values make sense.
Keep this next to the code — when in doubt about what a setting does,
check here before changing it.

---

## 1. Files overview

| File | What it's for |
|---|---|
| `.env` | Your secrets and connection settings (API key, endpoints, live/paper mode) |
| `config.py` | Every tunable number that controls risk and quoting behavior |
| `main.py` | Runs the actual bot loop |
| `auth.py` | Signs requests to Kalshi — you shouldn't need to touch this |
| `kalshi_client.py` | Talks to Kalshi's API — you shouldn't need to touch this |
| `risk_manager.py` | Enforces the limits from `config.py` — you shouldn't need to touch this |
| `market_maker.py` | The actual quoting logic — you shouldn't need to touch this |
| `check_liquidity.py` | One-off tool: checks order book depth for tickers you list manually |
| `find_liquid_markets_v3.py` | One-off tool: scans series for real two-sided markets automatically |
| `debug_market_fields.py` | One-off tool: dumps raw API JSON for a ticker, for debugging |

You'll routinely touch `.env` and `main.py`. Everything else is either
"set once and forget" (`config.py`) or diagnostic tools you run
occasionally, not part of the bot itself.

---

## 2. `.env` — connection settings

| Variable | What it does | What to put |
|---|---|---|
| `KALSHI_API_KEY_ID` | Identifies you to Kalshi on every request | The Key ID from your Kalshi account's API Keys page |
| `KALSHI_PRIVATE_KEY_PATH` | Where your private key file lives | A path relative to your bot folder, e.g. `./Chunk_key.txt` — must be the ACTUAL FILE, not the key text pasted directly |
| `KALSHI_BASE_URL` | Which API server to talk to | `https://api.elections.kalshi.com/trade-api/v2` for production — verify against docs.kalshi.com if it stops working, hosts occasionally change |
| `KALSHI_WS_URL` | WebSocket server (not yet used by the current bot — reserved for future live order book streaming) | Leave as default |
| `KALSHI_PAPER_TRADING` | The single most important switch in this whole project | `true` = simulate everything, zero real orders. `false` = real orders, real money. **Always start `true`.** |

---

## 3. `config.py` — `RiskConfig` (the risk limits)

These are the numbers that decide how much the bot can lose and how
aggressively it trades. This section is worth understanding fully before
ever going live.

### Position limits

| Setting | What it does | Reasonable starting value |
|---|---|---|
| `max_position_per_market` | Max number of contracts (net) the bot will hold in one market at once. Once hit, the bot stops quoting new orders on that market until the position shrinks. | `10-25` while testing |
| `max_total_exposure_usd` | Max total dollars deployed across ALL open positions combined. A hard ceiling on total capital at risk at any moment. | `$50-200` while testing |

### Loss limits (circuit breakers)

| Setting | What it does | Reasonable starting value |
|---|---|---|
| `max_daily_loss_usd` | If realized losses in one session hit this number, the bot halts completely and cancels all resting orders. This is your "worst case for today" number — set it to an amount you're fully at peace losing. | `$10-25` while testing |
| `max_single_market_loss_usd` | Same idea but scoped to one market — protects against one bad market dragging down everything even before the daily limit trips. | `$5-15` while testing |

### Quoting parameters — these control HOW the bot quotes

| Setting | What it does | Reasonable starting value |
|---|---|---|
| `min_edge_cents` | Minimum profit margin (in cents per contract) required, AFTER estimated fees, before the bot will place a quote at all. Higher = pickier, fewer trades, more profit per trade. Lower = more trades, thinner margins. | `2-3` |
| `quote_inset_cents` | How many cents inside the current best bid/ask your quote sits. `1` means if the market is 62/68, you quote 63/67. Bigger inset = more competitive but smaller edge captured. | `1` |
| `quote_size` | How many contracts to quote per side. Bigger = bigger potential profit AND bigger potential loss per fill. | `1-5` while testing |

### Timing / staleness controls — these protect against getting picked off

| Setting | What it does | Reasonable starting value |
|---|---|---|
| `max_quote_age_seconds` | Cancel and refresh a resting quote after this many seconds, even if nothing else changed. Old quotes are more likely to get "picked off" by someone with newer information. | `10-15` |
| `requote_on_move_cents` | If the market's best bid/ask moves more than this many cents since you quoted, cancel immediately rather than waiting for the age timer. Faster reaction to real price moves. | `2` |

### Kill switch

| Setting | What it does | Reasonable starting value |
|---|---|---|
| `max_consecutive_errors` | If the bot hits this many API errors in a row (network issues, auth failures, etc.), it halts entirely rather than continuing to fail silently. | `5` |
| `excluded_tickers` | A list of tickers the bot will never quote, even if you add them to `watched_tickers`. Useful for manually blocking a market you've decided is too risky (e.g., something close to resolution). | `[]` (empty) by default |

---

## 4. `config.py` — `BotConfig` (what to watch)

| Setting | What it does |
|---|---|
| `watched_tickers` | The list of exact market tickers the bot actively quotes. This is empty by default — **the bot will refuse to run until you set this**, either in `config.py` or right after `from config import CONFIG` in `main.py`. |
| `loop_interval_seconds` | How often (in seconds) the main loop re-checks each watched market, even with no other trigger. `2` is a reasonable default — fast enough to react, not so fast you hammer the API. |

---

## 5. Understanding the bot's log output

| Log line pattern | What it means |
|---|---|
| `Starting Kalshi market maker in PAPER mode. Watching: [...]` | Normal startup. Confirms mode and which tickers are active. |
| `[PAPER] Would place buy yes N @ X¢ on TICKER` | In paper mode: the bot decided to quote, no real order sent. |
| `[PAPER FILL] buy yes N @ X¢ on TICKER` | In paper mode: the simulated price crossed your quote — a simulated fill. This is what you're watching for to validate the strategy. |
| `Requoting ... (stale=True moved=False)` | The resting quote's age hit `max_quote_age_seconds`, so it's being cancelled and replaced. Normal, expected behavior. |
| `Requoting ... (stale=False moved=True)` | The market price moved past `requote_on_move_cents` since you quoted, triggering an immediate refresh. Normal. |
| `KXTICKER: incomplete book, skipping` | The market has no real bid, no real ask, or both — nothing to quote against right now. Not an error, just means this cycle had nothing to do. |
| `KILL SWITCH TRIGGERED: ...` | A hard limit was breached (daily loss, consecutive errors). The bot halts, cancels all resting orders, and stops. Requires you to manually review and restart. |

---

## 6. Safety checklist before ever setting `KALSHI_PAPER_TRADING=false`

Go through this list in order — don't skip ahead:

- [ ] Bot has run in paper mode for multiple sessions across more than one market
- [ ] You've seen at least a few `[PAPER FILL]` events and the implied P&L makes sense to you
- [ ] You've replaced the placeholder fee estimate in `market_maker.py` with Kalshi's real, current fee schedule
- [ ] You've verified the order-placement schema in `create_order` (in `kalshi_client.py`) against Kalshi's current API docs — this was flagged as unverified and is the single biggest risk of an untested live order failing or behaving unexpectedly
- [ ] `max_position_per_market`, `max_total_exposure_usd`, and `max_daily_loss_usd` are all set to amounts you are fully prepared to lose
- [ ] `quote_size` is small (1-2 contracts) for your first live session
- [ ] You plan to watch the bot manually for at least the first live session, not walk away
- [ ] You have a way to quickly kill the process (Ctrl+C) if something looks wrong — the bot cancels resting orders on shutdown, but any already-filled position stays open

---

## 7. Quick troubleshooting reference

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError` | A pip package isn't installed — `python -m pip install <package>` |
| `ValueError: KALSHI_API_KEY_ID is not set` | `.env` isn't being found or read — check it's named exactly `.env`, not `.env.txt`, and sits in the same folder you're running `python` from |
| `FileNotFoundError` on the private key | `KALSHI_PRIVATE_KEY_PATH` points to the wrong filename, or contains the key text instead of a file path |
| `No watched_tickers configured` | `CONFIG.watched_tickers` is still empty — set it in `main.py` or `config.py` |
| `incomplete book, skipping` forever | Either the ticker is a series prefix (not a specific market), or the market genuinely has no two-sided quotes right now — use `find_liquid_markets_v3.py` to check |
| 401 Unauthorized | Auth signing issue — verify the signing payload format in `auth.py` against Kalshi's current docs |
