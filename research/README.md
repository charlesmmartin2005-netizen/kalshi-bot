# Strategy lab (research only — not used by the bot)

`strategy_lab.py` replays real settled Kalshi BTC + ETH daily (5pm ET) markets
minute by minute and runs strategy variants on identical data, using the bot's
own fair_value / plan_quotes / RiskManager code.

    python research/strategy_lab.py fetch --days 31                                   # Sep 19 – Oct 19 → cache/
    python research/strategy_lab.py --cache cache_holdout fetch --days 31 --skip-days 31   # Aug 19 – Sep 18
    python research/strategy_lab.py run [--only A P Q]
    python research/strategy_lab.py --cache cache_holdout run --only A P Q

    python research/paper_markouts.py --since 2026-10-22    # score the live paper bot's fills

Kalshi's API returns these markets without strike data before ~Aug 17, so two
months is all the history there is.

## 2026-10-22: the earlier results looked ahead

The lab's books and coin prices are end-of-minute snapshots, but it filled
resting quotes against trades from *during* that minute, i.e. against a quote
priced after those trades happened. `backtest.py` had the same bug. Both now
fill a quote only from trades after the snapshot it was priced from. Fees
now follow Kalshi's schedule: makers pay nothing on KXBTCD/KXETHD (the lab
had charged them the taker rate, ~$600 a month at these volumes).

Corrected, per month (real fees). *Normal* fills freeze each quote for the
whole minute, which is harsher than the live bot, which requotes on every coin
tick. *Strict* fills assume it always requotes before a taker reacts, which
is kinder than reality.

| Variant | Sep–Oct normal | Sep–Oct strict | Aug–Sep normal | Aug–Sep strict |
|---|---|---|---|---|
| A original settings | −$188 | −$33 | −$197 | −$30 |
| P rolling ladder + delta lean (live until 10-22) | **−$1,616** | +$393 | **−$2,118** | +$213 |
| Q live since 10-22: P, BTC only, quotes 6¢ from fair value | −$179 | +$172 | −$341 | +$146 |

Before the fix the lab reported P at +$498 / +$548 (and +$842 / +$838 with
strict fills). `results*.json` are from that run.

Where reality sits between those bounds depends on how fast the bot's quotes
follow Kalshi's book when it moves for reasons the coin price doesn't
explain, and 1-minute history can't show that. So the tests below use
Binance's public 1-second BTC/ETH archive (data.binance.vision, scaled to
Coinbase's level; 1-minute returns correlated 0.98–0.99 with Coinbase) against
every real Kalshi trade. Those scripts and the archive (~280 MB) are not in
the repo.

### Takes: the edge is speed on big gaps

For each real trade (strikes 10–90¢, >2h to close), the model priced 1s
earlier says how cheap that price was after the taker fee. Taking the same
price, per contract, after 5 / 60 minutes (BTC):

| Model gap after fee | Sep–Oct | Aug–Sep | Oct 19–21 |
|---|---|---|---|
| 0–2¢ | −0.7 / −1.3¢ | −0.5 / −0.9¢ | −0.2 / +0.4¢ |
| 2–4¢ | +0.9 / −0.4¢ | +0.2 / −0.1¢ | +0.7 / +3.0¢ |
| 4–6¢ (`take_edge_cents`) | +1.3 / +2.3¢ | +0.4 / +0.3¢ | +0.5 / −1.0¢ |
| 6–8¢ | **+2.8 / +4.6¢** | **+1.4 / +4.1¢** | (3 trades) |

Monotonic in both months, and negative gaps lose (the market is right when
the model agrees with it). Delay kills it: at 6–8¢ gaps (Sep–Oct, net 5 min)
+5.2¢ at 0s, +2.8¢ at 1s, +1.9¢ at 3s, +0.5¢ at 10s. ETH samples are small
and mixed. The model alone is no better than the market at predicting
settlement (Brier 0.1405 vs 0.1412, 0.1609 vs 0.1606); its only advantage is
reacting first.

### Resting quotes are about breakeven

A quote at floor/ceil(fair value ± w), filled by every real trade that
reaches it, per contract after 5 / 60 minutes (BTC, maker fee 0):

| w | Sep–Oct | Aug–Sep | Oct 19–21 |
|---|---|---|---|
| 2¢ | +0.14 / −0.18¢ | +0.31 / +0.31¢ | +0.42 / −0.18¢ |
| 4¢ | +0.49 / −0.06¢ | +0.42 / +0.04¢ | −1.73 / −6.53¢ |
| 8¢ | +2.5 / +4.4¢ | +3.9 / +7.5¢ | +4.5 / +6.5¢ (few) |

Quotes near fair value earn a little in the first minutes and give it back;
paper trading on 10-20..22 saw maker fills lose 1.8–5.6¢ after 5 minutes.

### What holds up across fill models

Each candidate under three maker-fill models (1-second fills with the
previous minute's Kalshi book for the touch clamps = pessimistic; with the
end-of-minute book = optimistic, it peeks; and the strict 1-minute model),
both months, real fees, per month:

| Candidate | Worst of 6 | Best of 6 |
|---|---|---|
| P (live until 10-22) | −$694 | +$1,152 |
| Quotes 6¢ from fair value | −$25 | +$249 |
| BTC only | −$72 | +$886 |
| BTC only, quotes 4¢ | +$13 | +$437 |
| **BTC only, quotes 6¢ (Q, live)** | **+$20** | +$273 |
| BTC only, quotes 6¢, takes only on 6¢+ gaps | +$23 | +$267 |
| No takes | −$638 | +$1,054 |

ETH's maker fills drove most of the downside (−$350 to −$620 a month in the
pessimistic model). Removing takes made every model worse. Blocking takes
with >20h to close made no difference.

### Unwind floor and take exposure cap (2026-10-22, after a day of paper fills)

Oct 22's paper fills: takes +4.27¢ a contract 5 minutes later (fee 1.50¢),
+$53 after fees; resting fills -2.33¢, -$56 at settlement, mostly unwind
bids pulled up to fair value by skew and lean while quotes were still 2¢
wide; 94% of takes were sells into a BTC drop. Two rules were tested on the
live settings (BTC only, 6¢ quotes), per month:

| Variant | Sep–Oct pess / mid / opt | Aug–Sep pess / mid / opt | Worst | Mean |
|---|---|---|---|---|
| No floor, no cap | +$122 / +$152 / +$273 | +$20 / +$146 / +$159 | +$20 | +$145 |
| Unwind floor 0¢ (live) | +$116 / +$157 / +$269 | +$27 / +$135 / +$159 | +$27 | +$144 |
| Unwind floor 2¢ | +$136 / +$194 / +$313 | −$19 / +$69 / +$85 | −$19 | +$130 |
| Unwind floor 4¢ | +$166 / +$172 / +$312 | −$100 / +$49 / +$33 | −$100 | +$105 |
| Take cap $50 per 1% (live) | +$120 / +$157 / +$271 | +$21 / +$126 / +$162 | +$21 | +$143 |

Wider floors help one month and hurt the other (slower unwinding rides more
into settlement), so the floor only stops unwind quotes crossing the model.
The cap costs nothing here; what it's for, fast same-direction take bursts,
1-minute data can't reproduce. Fills now log the model's price and gap
(`model_cents`, `edge_cents`), so the markout report can check live whether
bigger gaps pay more.

### Still unknown

- Whether these trades are winnable: each print was taken by someone, maybe
  a faster bot. The live bot polls Kalshi's book every 0.4s without an API key.
- Queue position: the tests assume our quote would be first at its price.
- Sizing takes by the gap (e.g. 5 contracts at 4–6¢, 15 at 6¢+) is backed
  by the per-trade numbers but hasn't been run as a strategy.
- Settlement P&L over two months is ~62 independent outcomes (one BTC close
  a day), too few to see a few-cent edge. Judge changes by markouts.

## 2026-10-20 result (superseded: look-ahead fills, taker-rate maker fees)

Proposed strategy **P — rolling ladder + delta lean**: the 6 BTC + 3 ETH
strikes nearest the money re-centred every 15 min, quote 15 / take 15, max 75
per market, resting quotes leaned up to 4¢ against the portfolio's coin
delta (full lean at $30 per 1% move). The lab reported +$498.06 (Sep 19–Oct
19) and +$548.16 (Aug 19–Sep 18) against +$16.76 / −$48.15 for the original
settings. Corrected numbers are above.

Related: [README](../README.md): the live settings and a summary of each round of testing
