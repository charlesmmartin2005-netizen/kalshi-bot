"""
Is the crypto model usable in the last 2 hours before settlement?

For every minute in the last 2h of every cached BTC/ETH daily event, and
every strike priced 5..95¢ with a ≤4¢ spread, compare model variants with
the market mid against the real outcome (Brier score, lower = better), and
simulate taking whenever the model disagrees with the book by more than the
fee + an edge threshold (held to settlement).

Model variants differ only in the volatility used:
  fit-now     the live rule: ladder vol fit on the current minute (needs ≥3
              strikes 10..90¢ — usually fails late, which is why trading stops)
  carry       the last valid ladder fit, carried forward into the last 2h
  carry+avg   carry, plus the settlement averaging window: the payoff is the
              mean of 60 one-second prints, so the last minute only adds a
              third of its variance (time = max(h-1m,0) + 1m/3)

    python research/late_window.py
"""
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_lab as L  # noqa: E402
from fair_value import _crypto_prob  # noqa: E402
from fees import per_contract_fee_cents  # noqa: E402

WINDOW_H = 1 / 60


def sigma_series(ev):
    hour_closes = {int(k): v for k, v in json.loads(
        (L.CACHE / f"hours_{L.PRODUCTS[ev['series']]}.json").read_text()).items()}
    keys = sorted(hour_closes)
    out, sigma = [], None
    for i in range(len(ev["spots"])):
        minute = ev["open"] + i * 60
        if sigma is None or minute % 3600 == 0:
            sigma = L.sigma_at(keys, hour_closes, minute) or sigma
        out.append(sigma)
    return out


def fit(ev, i, spot, base):
    pts = []
    for m in ev["markets"]:
        b, a = ev["books"][m["ticker"]][i]
        if b and a and a - b <= 4 and 10 <= (a + b) / 2 <= 90:
            pts.append((m, (a + b) / 2))
    if len(pts) < 3:
        return None
    return min((sum((_crypto_prob(m, spot, base * k) * 100 - mid) ** 2 for m, mid in pts), k)
               for k in [x / 20 for x in range(6, 61)])[1]


def analyse(cache_dirs, edge_cents=(2, 4, 6)):
    stats = {}
    for d in cache_dirs:
        L.CACHE = Path(__file__).parent / d
        for p in sorted(L.CACHE.glob("*.json.gz")):
            with gzip.open(p, "rt") as f:
                ev = json.load(f)
            n = len(ev["spots"])
            sig = sigma_series(ev)
            carry = 1.0
            for i in range(n):
                h = (ev["close"] - ev["open"]) / 3600 - i / 60
                spot = ev["spots"][i]
                if h >= 2:
                    if i % 15 == 0:
                        k = fit(ev, i, spot, sig[i] * math.sqrt(h))
                        carry = k or carry
                    continue
                if h <= 1 / 60:
                    continue
                k_now = fit(ev, i, spot, sig[i] * math.sqrt(h)) if i % 5 == 0 else None
                h_avg = max(h - WINDOW_H, 0) + WINDOW_H / 3
                sds = {"fit-now": sig[i] * math.sqrt(h) * k_now if k_now else None,
                       "carry": sig[i] * math.sqrt(h) * carry,
                       "carry+avg": sig[i] * math.sqrt(h_avg) * carry}
                bucket = "60-120m" if h > 1 else "15-60m" if h > 0.25 else "1-15m"
                for m in ev["markets"]:
                    t = m["ticker"]
                    b, a = ev["books"][t][i]
                    if not (b and a and a - b <= 4 and 5 <= (a + b) / 2 <= 95):
                        continue
                    y = 1.0 if m["result"] == "yes" else 0.0
                    mid = (a + b) / 2
                    row = stats.setdefault(bucket, {}).setdefault("market", [])
                    row.append(((mid / 100 - y) ** 2, 0.0))
                    for name, sd in sds.items():
                        if not sd:
                            continue
                        pm = _crypto_prob(m, spot, sd) * 100
                        s = stats[bucket].setdefault(name, [])
                        s.append(((pm / 100 - y) ** 2, abs(pm - mid)))
                        for e in edge_cents:
                            key = f"{name} take≥{e}¢"
                            tk = stats[bucket].setdefault(key, [])
                            if pm - a - per_contract_fee_cents(a, maker=False) >= e:
                                tk.append(100 * y - a - per_contract_fee_cents(a, maker=False))
                            elif b - pm - per_contract_fee_cents(b, maker=False) >= e:
                                tk.append(b - 100 * y - per_contract_fee_cents(b, maker=False))
    return stats


def main():
    for label, dirs in (("Sep 19–Oct 19", ["cache"]), ("Aug 19–Sep 18", ["cache_holdout"])):
        stats = analyse(dirs)
        print(f"\n===== {label} =====")
        for bucket in ("60-120m", "15-60m", "1-15m"):
            s = stats.get(bucket, {})
            mk = statistics.mean(x[0] for x in s["market"])
            print(f"  {bucket} before close   market-mid Brier {mk:.4f}  (n={len(s['market'])})")
            for name in ("fit-now", "carry", "carry+avg"):
                if s.get(name):
                    print(f"    {name:<10} Brier {statistics.mean(x[0] for x in s[name]):.4f}   "
                          f"avg |model−mid| {statistics.mean(x[1] for x in s[name]):5.2f}¢  (n={len(s[name])})")
            for name in ("carry", "carry+avg"):
                for e in (2, 4, 6):
                    tk = s.get(f"{name} take≥{e}¢", [])
                    if tk:
                        se = statistics.pstdev(tk) / math.sqrt(len(tk))
                        print(f"      {name:<10} take≥{e}¢: {len(tk):>5} takes  "
                              f"avg {statistics.mean(tk):+5.2f}¢/contract ±{se:.2f}")


if __name__ == "__main__":
    main()
