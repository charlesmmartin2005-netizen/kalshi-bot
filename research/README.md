# Strategy lab (research only — not used by the bot)

`strategy_lab.py` replays real settled Kalshi BTC + ETH daily (5pm ET) markets
minute by minute and runs strategy variants on identical data, using the bot's
own fair_value / plan_quotes / RiskManager code.

    python research/strategy_lab.py fetch --days 31                                   # Sep 19 – Oct 19 → cache/
    python research/strategy_lab.py --cache cache_holdout fetch --days 31 --skip-days 31   # Aug 19 – Sep 18
    python research/strategy_lab.py run [--only A P Q]
    python research/strategy_lab.py --cache cache_holdout run --only A P Q
    python research/strategy_lab.py [--cache cache_holdout] holds   # takes: hold vs exit after N min

    python research/paper_markouts.py --since 2026-10-22    # score the live paper bot's fills
    scp kalshi-bot:kalshi-bot/fills_log.jsonl fills_log.server.jsonl   # the bot's ledger is on the server
    python research/paper_markouts.py --since 2026-10-22 --ledger fills_log.server.jsonl

    python research/lab_1s.py grid --cache cache --model pess [--only NAME ...] [--part 1/2]
    python research/lab_1s.py report        # every change vs live, day by day, per fill model

Kalshi's API returns these markets without strike data before ~Aug 17, so two
months is all the history there is.

## 2026-10-29: the live week, and 30 changes under four fill models

Sources: the server's ledger 10-22 → 10-28 (353 paper fills), its logs, the
same week replayed (`research/cache_live`), and `lab_1s.py grid`: 30 changes
to the live settings, each under four fill models on both months, paired day
by day against live. `lab_1s.py` is the 1-second harness, now in the repo; it
needs Binance's 1-second klines (data.binance.vision) in
`research/cache_binance/` (272 MB, gitignored).

**What happened live**

- The week's biggest loss wasn't the strategy. 33 resting fills booked when
  the PC woke from sleep (10-26 14:47 CT after 4¼ h, 10-27 11:03), at
  pre-sleep prices, lost $115, more than the week's net (−$52). Without them
  the week made +$64. The server doesn't sleep, but real orders left resting
  while nothing watches them do the same, and nothing cancels a previous
  run's orders at startup (`kalshi_client.cancel_all_orders` is never called).
- The server bot was stopped by the OOM killer four times on 10-28
  (12:06–14:35 CDT). Each time a ~685 MB child process was killed (the
  journal has no "main process exited"), and the sweep subprocess was timing
  out on every run then (since 02:37, on a 909 MB box). The unit's
  `OOMPolicy=stop` then stops the whole service. Since the 14:35 restart,
  sweeps finish in ~41 s at ~340 MB.
- Takes made +4.1¢ a contract a minute after the fill (fee 1.4¢). Those with
  more than 14 h to close made +1.5¢ (+0.2¢ after the fee); those within 14 h
  made +5.1¢.
- Resting fills outside the wake-ups: −0.1¢ after a minute, −1.3¢ after five.

**How far the lab is from live** (the four days the bot traded, replayed)

- The lab sees a fifth of the takes: 342 contracts against 1,873 live. Most
  live takes catch gaps that open and close within a minute, which
  end-of-minute snapshots miss, so take rules are under-tested here.
- Live resting fills sit between the normal and pess models (normal: more
  fills, −1.9¢ after a minute; mid +3.8¢ on these days). Judge resting-quote
  changes on those two. On them the live settings make −$251 and +$72 a
  month; mid and opt say +$137 and +$216.

**The grid**: $ a month against live, both months pooled, t in brackets, ✓ =
better in both months; then sd/day and worst day under normal / pess / mid /
opt:

| Change | normal | pess | mid | opt | sd/day | Worst day |
|---|---|---|---|---|---|---|
| Live settings | | | | | $26 / 30 / 30 / 34 | −$110 / −61 / −81 / −45 |
| **No takes with >14 h to close** | +30 (0.7) ✓ | +25 (0.5) ✓ | +64 (1.2) ✓ | +65 (1.5) ✓ | $23 / 26 / 23 / 32 | −$78 / −42 / −34 / −29 |
| No takes with >18 h to close | +22 (0.6) ✓ | +42 (0.9) ✓ | +74 (1.6) ✓ | +73 (2.1) ✓ | $23 / 26 / 24 / 32 | −$78 / −42 / −34 / −29 |
| >14 h rule + stop 3 h before close | +129 (2.0) ✓ | +28 (0.5) ✓ | +63 (1.0) ✓ | +68 (1.4) ✓ | $21 / 25 / 23 / 31 | −$42 / −42 / −37 / −35 |
| >14 h rule + 3 h stop + quotes 8¢ | +253 (3.2) ✓ | +121 (1.9) ✓ | +58 (0.8) ✓ | +45 (0.9) ✓ | $22 / 28 / 24 / 29 | −$47 / −50 / −41 / −39 |
| Quotes 8¢ from the model | +138 (2.1) ✓ | +13 (0.2) | −59 (−1.4) | −54 (−0.8) | $30 / 38 / 35 / 38 | −$123 / −141 / −141 / −136 |
| Stop 3 h before close | +106 (2.5) ✓ | +4 (0.2) | −4 (−0.2) | +6 (0.3) | $24 / 30 / 29 / 33 | −$59 / −58 / −72 / −39 |
| Resting quotes only to reduce | +250 (3.9) ✓ | −39 (−0.6) | −98 (−3.0) | −169 (−1.7) | $20 / 23 / 26 / 22 | −$64 / −65 / −91 / −63 |
| Takes 5 at 4–6¢ gaps, 15 at 6¢+ | −10 (−0.4) | −36 (−0.9) | −49 (−1.2) | −22 (−0.6) | $25 / 25 / 22 / 30 | −$123 / −54 / −68 / −44 |
| Take edge 6¢ | −7 (−0.2) | −29 (−0.5) | −20 (−0.3) | −3 (−0.1) | $23 / 23 / 18 / 29 | −$100 / −51 / −57 / −47 |
| No takes | −8 (−0.1) | −43 (−0.5) | −28 (−0.3) | −12 (−0.2) | $19 / 20 / 12 / 27 | −$67 / −39 / −21 / −30 |
| Exit takes after 60 min | −12 (−0.2) | −45 (−0.6) | −23 (−0.3) | −26 (−0.4) | $19 / 21 / 16 / 26 | −$69 / −42 / −38 / −33 |
| Quotes 4¢ from the model | −377 (−6.1) | −49 (−0.9) | +29 (0.6) ✓ | +115 (2.1) ✓ | $29 / 26 / 26 / 37 | −$130 / −60 / −51 / −34 |
| Quote size 5 | +144 (2.5) ✓ | −150 (−2.3) | −147 (−2.2) | −179 (−2.5) | $16 / 19 / 16 / 19 | −$73 / −63 / −63 / −54 |

Only the take window is better in both months under every fill model, and it
also lowers sd/day and the worst day under all four. 14–18 h is a plateau;
8–12 h cut profitable takes on Sep–Oct. The gain comes from the takes made far
from the close: take P&L goes from about −$105 to about +$77 a month, but
resting quotes had been earning $110–160 of that back by unwinding those
positions. It helps
on 16–23 of the ~30 days it changes and hurts on 7–14; under normal fills
the gain rests on its three best days. For a daily market, hours to close
and time of day are the same thing: >14 h is the 5pm CT open until ~2am.

Wider quotes and stopping earlier pay only under the harsh fill models.
Quotes 8¢ from the model, on their own, are worse on Aug–Sep under 3 of 4
models and make the worst day −$123 to −$141. With the take window they're
better in all 8 cells, but that combination was picked from these same two
months out of 30 candidates; the live week's replay is neutral (+$32 normal,
−$35 mid over 7 days).

Ruled out: smaller takes on 4–6¢ gaps, a higher take edge, no takes, exits
after N minutes, 4¢ quotes, and dropping the delta lean or inventory skew
(both make the worst day −$118 to −$185). No call possible on resting quotes
only to reduce, or quote size 5: big wins under normal fills, big losses
under the other three. A guard against quoting when the model and mid
disagree by 8–12¢ almost never triggers. And `take_size` above `quote_size`
does nothing: `plan_quotes` caps a take at the quote size.

**Recommendations, in order**

1. Before trading real money: cancel the account's resting orders at
   startup, and set `OOMPolicy=continue` in `kalshi-bot.service`, so a
   runaway sweep can't stop trading.
2. No takes with more than 14 h to close. This needs a new setting (the bot
   has no take window; the 2-hour stop is `CRYPTO_MIN_HOURS_TO_TRADE` in
   `fair_value.py`). The lab expects +$25–65 a month while seeing a fifth of
   the live takes, and the live markouts point the same way. Paper-trade it
   and watch the take markouts by hours to close.
3. Only if 2 holds up: quotes 8¢ from the model and a 3-hour stop. On top of
   the take window they added $96–223 a month under the harsh fill models
   (−$6 to −$20 under the optimistic ones), but were fitted on the same two
   months.

**Done the same day (in the code, not yet deployed):** all three.
`engine.start` cancels resting orders on the coin series it trades, and on
watched markets, before trading live; it refuses to start if it can't list
them. `kalshi-bot.service` has `OOMPolicy=continue`, which
`deploy-server.ps1` installs. `config.py` has `min_edge_cents` 8 and
`max_take_hours` 14 (checked in `plan_quotes`), `fair_value.py` stops at 3h,
and auto-pick follows that constant.

The lab's "live since 10-29" row runs `config.py` through the bot's own rules.
It matches the combined row in 5 of 8 cells and is $2–11 a month lower in
the other three: a take the bot doesn't plan no longer shrinks the resting
quote on that side. Against the old live settings, per month: normal −$3 vs
−$251 (+$248, t 3.1), pess +$193 vs +$72 (+$121, t 1.9), mid +$190 vs +$137
(+$53), opt +$261 vs +$216 (+$45). It's better in both months under all four,
with lower sd/day and a smaller worst day in each (normal −$47 vs −$110).
`strategy_lab`'s Q and the lab's baseline stay pinned to the old settings
(R is the new one), so earlier tables still reproduce.

Lab fixes alongside: `strategy_lab.py fetch` kept only event tickers ending
in "17", so it dropped 10-28's daily event (`KXBTCD-26OCT2817B`, 81 strikes)
for a two-day one opened 10-26. It now keeps the B event.

## 2026-10-28: close takes after a fixed hold? No, keep holding them

In the 24 hours to 10-28 14:22 CT, paper takes marked well at mid (+2.4¢ a
contract after 1 minute, +5.7¢ after 60) but lost money held, mostly on two
short bursts held for hours while BTC rose $300–600 (10-28's takes settled at
−$99). `holds` replays the live settings with what's left of each take closed
N minutes later at the touch, paying the spread and the taker fee. If the
model would make the same take again at that moment, the take's clock
restarts instead (unwinding would only pay the round trip to get back in;
without that rule 5-minute exits lose another $60–90 a month). The lab now
also applies the live $50 take cap, and walks strikes in sorted order: set
order varied with the hash seed and moved totals by ~$5 between runs.

Per month after fees, live settings (BTC only, 6¢ quotes, 0¢ unwind floor,
$50 cap), pess / mid / opt fills as in "What holds up across fill models"
(the 1-second pess and opt runs need the scratch harness and Binance archive,
which aren't in the repo; `holds`' strict fills match mid on Aug–Sep and
differ by up to $19 on Sep–Oct):

| Takes | P&L Sep–Oct | P&L Aug–Sep | sd/day Sep–Oct | sd/day Aug–Sep | Worst day Sep–Oct | Worst day Aug–Sep |
|---|---|---|---|---|---|---|
| **Held to settlement (live)** | +$115 / +$160 / +$270 | +$28 / +$115 / +$161 | $32 / $27 / $39 | $28 / $33 / $28 | −$56 / −$33 / −$40 | −$61 / −$81 / −$45 |
| Exit after 5 min | −$71 / −$47 / +$118 | −$76 / +$42 / +$46 | $21 / $11 / $33 | $18 / $11 / $15 | −$43 / −$28 / −$27 | −$39 / −$18 / −$31 |
| Exit after 15 min | −$47 / −$31 / +$139 | −$50 / +$66 / +$66 | $20 / $12 / $32 | $19 / $14 / $17 | −$44 / −$35 / −$24 | −$39 / −$16 / −$32 |
| Exit after 60 min | +$66 / +$124 / +$267 | −$11 / +$104 / +$113 | $20 / $13 / $31 | $21 / $18 / $20 | −$42 / −$11 / −$17 | −$41 / −$38 / −$33 |
| Exit after 120 min | +$52 / +$105 / +$232 | +$7 / +$116 / +$134 | $21 / $15 / $31 | $23 / $21 / $23 | −$45 / −$19 / −$18 | −$44 / −$42 / −$37 |

Day by day against holding, across these and `holds`' normal and strict
fills (10 runs per exit): 5- and 15-minute exits lose in every run, $9–$235
a month. 60- and 120-minute exits lose $4–$84 in 16 of 20 runs and gain up
to $48 in 4 (Aug–Sep: both under normal fills, and +$1 at 120 minutes under
strict and mid), with a standard error of $65–$125 a month on the
difference, so neither direction is significant. Exits beat holding on only
7–16 of 31 days. What they buy is smaller swings: 20–50% less sd/day at 60
minutes, and a smaller worst day in every run.

Each take on its own (strict fills, not netted against other fills), ¢ per
contract after fees, with sd/day and worst day of the takes alone:

| | Sep–Oct | Aug–Sep |
|---|---|---|
| Held to settlement | +2.27¢ ($29, −$57) | −8.85¢ ($43, −$168) |
| Closed at the touch after 5 min | −2.24¢ ($5, −$20) | −3.64¢ ($6, −$23) |
| after 15 min | −1.83¢ ($4, −$14) | −2.98¢ ($6, −$24) |
| after 60 min | +0.87¢ ($8, −$10) | −2.21¢ ($13, −$58) |
| after 120 min | +0.90¢ ($11, −$16) | −2.55¢ ($16, −$76) |

By mid, takes have earned +1.1 / +1.5 / +4.2 / +4.0¢ after 5 / 15 / 60 / 120
minutes in Sep–Oct (+0.7 / +1.5 / +1.5 / +0.9¢ in Aug–Sep), before fees.
The entry fee, the exit fee and half the spread come to ~3.3¢ a contract, more
than a take has made after 5–15 minutes. On their own, Aug–Sep's takes would
have lost ~$200 less exited at 60 minutes, but the strategy doesn't get that,
because it never really holds its takes to settlement: resting quotes leaned
against the position work them off for free. In Aug–Sep, 3,141 contracts
were taken and only 1,418 net contracts reached settlement. A 60-minute exit
replaces those maker exits with taker exits (resting fills fall from 3,813
to 2,609), and the room it frees under the cap lets more takes in.

Live paper takes 10-22 → 10-28 (server ledger, `--ledger`; 164 takes, 1,873
contracts, 4 settlement days), each marked at the touch N minutes later with
fees both ways:

| Takes | ¢/contract | Total | sd/day | Worst day | 10-22 | 10-26 | 10-27 | 10-28 |
|---|---|---|---|---|---|---|---|---|
| **Held to settlement** | +1.09¢ | +$20 | $72 | −$99 | +$53 | +$89 | −$22 | −$99 |
| Exit after 1 min | +0.46¢ | +$9 | $10 | −$8 | +$14 | −$7 | +$10 | −$8 |
| Exit after 5 min | −0.89¢ | −$17 | $9 | −$20 | +$4 | +$0 | −$1 | −$20 |
| Exit after 15 min | −2.14¢ | −$40 | $8 | −$22 | −$22 | −$10 | −$6 | −$2 |
| Exit after 60 min | −1.46¢ | −$27 | $20 | −$24 | −$19 | −$11 | −$24 | +$27 |
| Exit after 120 min | −3.92¢ | −$73 | $13 | −$33 | −$33 | −$3 | −$29 | −$9 |

The same picture: holding made the most, and exits only shrink the swings. A
60-minute exit would have turned 10-28's −$99 into +$27 but lost on the other
three days. The 1-minute exit is the one live rule that ends up positive
($9 in four days); the lab's 1-minute books can't test it. (Tickers
`KXBTCD-26OCT2817-…` and `KXBTCD-26OCT2817B-…` are both the 10-28 event.)

**Recommendation: hold, i.e. keep the current behaviour.** 5- and 15-minute
exits lost to holding in all 20 backtest runs and in the live paper takes;
60- and 120-minute exits lost in 16 of 20 runs and live, and won only within
noise. A 60-minute exit is the only candidate, and only if smaller daily
swings are worth paying for: it cut sd/day by 20–50% and the worst day, and
cost up to $84 a month ($11–$36 under mid fills), a difference two months
can't tell apart from zero.

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
