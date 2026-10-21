"""
Speed and accuracy tests for the live strategy (P: rolling ladder + delta
lean, live config.py settings) on both cached months.

ACCURACY  (minutes > 2h before close — where the bot trades)
  - Brier score vs the real outcome: model vs Kalshi mid (lower = better)
  - average |model − mid|, and calibration: when the model says X%, how
    often did YES actually happen?

SPEED
  - How fast does Kalshi catch up to the coin? Regress each strike's
    1-minute mid change on the model's fair-value change in the same minute
    and the minutes before. Weight on earlier minutes = Kalshi lagging.
  - What is speed worth? Re-run the strategy with the model seeing the coin
    1, 2, 5 minutes late. The P&L lost is the value of being fast (at
    1-minute resolution — the live bot reprices in ~15ms, finer than this
    data can show).

FILLS BY TIME OF DAY  — what fill rate to expect at each hour (ET).

    python research/speed_accuracy.py
"""
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_lab as L  # noqa: E402
from config import RiskConfig  # noqa: E402
from fair_value import _crypto_prob  # noqa: E402

LIVE = RiskConfig()
ET = timezone(timedelta(hours=-4))        # EDT (both months are before Nov 1)


def live_variant(name="P live", **kw):
    return L.Variant(name, LIVE, {"KXBTCD": 6, "KXETHD": 3}, roll_min=15,
                     hedge_cents=LIVE.delta_lean_cents, hedge_ref_usd=LIVE.delta_lean_ref_usd,
                     lean_takes=False, strict_fills=True, **kw)


def accuracy(days):
    rows = []        # (model, mid, outcome)
    for evs in days:
        for ev in evs:
            n = len(ev["spots"])
            for i in range(0, n, 5):
                h = (ev["close"] - ev["open"]) / 3600 - i / 60
                if h < 2 or not ev["fit_ok"][i]:
                    continue
                for t, m in ev["mkt"].items():
                    b, a = ev["books"][t][i]
                    if not (b and a and a - b <= 4 and 5 <= (a + b) / 2 <= 95):
                        continue
                    pm = _crypto_prob(m, ev["spots"][i], ev["sd"][i]) * 100
                    rows.append((pm, (a + b) / 2, 1.0 if m["result"] == "yes" else 0.0))
    brier = lambda k: statistics.mean((r[k] / 100 - r[2]) ** 2 for r in rows)
    print(f"  samples {len(rows):,}  Brier model {brier(0):.4f}  market {brier(1):.4f}  "
          f"avg |model−mid| {statistics.mean(abs(r[0] - r[1]) for r in rows):.2f}¢")
    print("  calibration (model says → actually happened):")
    buckets = defaultdict(list)
    for pm, mid, y in rows:
        buckets[min(int(pm // 10), 9)].append((pm, mid, y))
    for b in sorted(buckets):
        xs = buckets[b]
        print(f"    {b * 10:>2}-{b * 10 + 10:<3}%  model {statistics.mean(x[0] for x in xs):5.1f}%  "
              f"market {statistics.mean(x[1] for x in xs):5.1f}%  real {100 * statistics.mean(x[2] for x in xs):5.1f}%  "
              f"(n={len(xs):,})")


def lead_lag(days, lags=(0, 1, 2, 3, 5)):
    """OLS of Δmid(t) on Δfv(t-k) for each k, near-the-money strikes."""
    X, Y = [], []
    for evs in days:
        for ev in evs:
            n = len(ev["spots"])
            for t, m in ev["mkt"].items():
                fv, mid = [], []
                for i in range(n):
                    b, a = ev["books"][t][i]
                    fv.append(_crypto_prob(m, ev["spots"][i], ev["sd"][i]) * 100)
                    mid.append((a + b) / 2 if b and a and a - b <= 4 else None)
                for i in range(max(lags) + 1, n):
                    h = (ev["close"] - ev["open"]) / 3600 - i / 60
                    if h < 2 or not ev["fit_ok"][i] or mid[i] is None or mid[i - 1] is None:
                        continue
                    if not 20 <= mid[i] <= 80:
                        continue
                    X.append([fv[i - k] - fv[i - k - 1] for k in lags])
                    Y.append(mid[i] - mid[i - 1])
    # normal equations (small k) without numpy
    k = len(lags)
    xtx = [[sum(x[a] * x[b] for x in X) for b in range(k)] for a in range(k)]
    xty = [sum(x[a] * y for x, y in zip(X, Y)) for a in range(k)]
    for c in range(k):                       # Gauss-Jordan
        p = xtx[c][c]
        for j in range(k):
            xtx[c][j] /= p
        xty[c] /= p
        for r in range(k):
            if r != c:
                f = xtx[r][c]
                for j in range(k):
                    xtx[r][j] -= f * xtx[c][j]
                xty[r] -= f * xty[c]
    total = sum(xty)
    print(f"  {len(Y):,} strike-minutes. Share of a fair-value move that shows up in the Kalshi mid:")
    for lag, b in zip(lags, xty):
        print(f"    {'same minute' if lag == 0 else f'{lag} min later':<12} {b:+.2f}")
    print(f"    total {total:+.2f} (1.00 = Kalshi fully follows the model)")


def speed_value(days):
    print("  model sees the coin price…")
    base = None
    for d in (0, 1, 2, 5):
        v = live_variant(f"delay {d}m", spot_delay_min=d)
        rows = [L.run_day(v, x) for x in days]
        s = L.summarize(v, rows)
        base = base if base is not None else s["total"]
        print(f"    {('on time' if d == 0 else f'{d} min late'):<11} total ${s['total']:+8.2f}  "
              f"win {s['win']:>2}/{s['days']}  takes/day {s['takes_day']:5.1f}  "
              f"take markout {s['take_mo']:+6.2f}¢/fill  ({(s['total'] / base * 100) if base else 0:5.1f}% of on-time)")
        if d == 0:
            by_hour = defaultdict(int)
            for r in rows:
                for ts, kind in r["fill_times"]:
                    by_hour[datetime.fromtimestamp(ts, ET).hour] += 1
            fills_by_hour = {h: c / len(rows) for h, c in by_hour.items()}
    return fills_by_hour


def main():
    for label, d in (("Sep 19 – Oct 19", "cache"), ("Aug 19 – Sep 18", "cache_holdout")):
        L.CACHE = Path(__file__).parent / d
        days = L.load_days()
        print(f"\n================ {label} ({len(days)} days) ================")
        print("ACCURACY (trading window, >2h before close)")
        accuracy(days)
        print("SPEED: how fast Kalshi follows the coin")
        lead_lag(days)
        print("SPEED: what being fast is worth (strict fills, live settings)")
        fbh = speed_value(days)
        print("FILLS BY HOUR (ET), average per day:")
        print("   " + "  ".join(f"{h:02d}h:{fbh.get(h, 0):4.1f}" for h in range(24)))


if __name__ == "__main__":
    main()
