# Strategy lab (research only — not used by the bot)

`strategy_lab.py` replays real settled Kalshi BTC + ETH daily (5pm ET) markets
minute by minute and runs strategy variants on identical data, using the bot's
own fair_value / plan_quotes / RiskManager code.

    python research/strategy_lab.py fetch --days 31                                   # Sep 19 – Oct 19 → cache/
    python research/strategy_lab.py --cache cache_holdout fetch --days 31 --skip-days 31   # Aug 19 – Sep 18
    python research/strategy_lab.py run [--only A P]
    python research/strategy_lab.py --cache cache_holdout run --only A P

## Result (2026-10-20)

Proposed strategy **P — rolling ladder + delta lean**:
- watch the 6 BTC + 3 ETH strikes nearest the money, re-centred every 15 min
  (strikes that roll off with a position go reduce-only)
- quote 15 / take 15, max 75 per market
- lean resting quotes (never takes) against the portfolio's BTC/ETH delta:
  up to 4¢ on the highest-delta strike, full lean at $30 per 1% move

| | Current (A) Sep 19–Oct 19 | P Sep 19–Oct 19 (tuned) | Current (A) Aug 19–Sep 18 | P Aug 19–Sep 18 (untouched) |
|---|---|---|---|---|
| Total | +$16.76 | +$498.06 | −$48.15 | +$548.16 |
| Avg / day | +$0.54 ± 9.91 | +$16.07 ± 27.35 | −$1.55 ± 6.41 | +$17.68 ± 31.51 |
| Winning days | 16/31 | 22/31 | 13/31 | 24/31 |
| Worst day | −$14.57 | −$18.17 | −$14.66 | −$44.26 |
| Max drawdown | $59.73 | $23.29 | $68.40 | $57.14 |
| Fills / day | 82 | 281 | 74 | 247 |
| Hours with a fill | 32% | 47% | 31% | 41% |
| Peak capital used | $60 | $406 | $53 | $519 |

Findings: fixed strikes drift away from the money (rolling fixes fills); size
alone just amplifies variance; lower take/quote edges and flattening before
settlement lose money; a delta lean that also triggers takes loses money
(pays spread + fee to hedge); a strong lean (≥6¢ at $8–15) bleeds.

Caveats: 1-minute data; takes assume ≥15 contracts at the touch; maker fills
assume front-of-queue on trades through our price; no market impact; P was
picked from ~25 variants on the Sep–Oct month (Aug–Sep is the honest test).
