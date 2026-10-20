"""
How well does each exchange (and a blend) match what Kalshi actually
settled on? Kalshi's crypto dailies settle on the simple average of 60
one-second CF Benchmarks RTI prints before 5pm ET; settled markets report
it as `expiration_value`. For every settled BTC/ETH daily event in the
cached months, compare it with each exchange over that same minute.

    python research/index_check.py
"""
import gzip
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import CONFIG  # noqa: E402
from kalshi_client import build_client  # noqa: E402

H = requests.Session()
PAIRS = {"KXBTCD": {"coinbase": "BTC-USD", "bitstamp": "btcusd", "kraken": "XBTUSD", "gemini": "btcusd"},
         "KXETHD": {"coinbase": "ETH-USD", "bitstamp": "ethusd", "kraken": "ETHUSD", "gemini": "ethusd"}}


def coinbase_min(product, t):
    r = H.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
              params={"granularity": 60, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                      "end": datetime.fromtimestamp(t + 60, timezone.utc).isoformat()}, timeout=15).json()
    c = [x for x in r if int(x[0]) == t]
    return (c[0][1] + c[0][2] + c[0][3] + c[0][4]) / 4 if c else None    # (L+H+O+C)/4


def bitstamp_min(pair, t):
    r = H.get(f"https://www.bitstamp.net/api/v2/ohlc/{pair}/",
              params={"step": 60, "limit": 1, "start": t}, timeout=15).json()
    rows = [x for x in r.get("data", {}).get("ohlc", []) if int(x["timestamp"]) == t]
    if not rows:
        return None
    x = rows[0]
    return sum(float(x[k]) for k in ("open", "high", "low", "close")) / 4


def kraken_min(pair, t):
    """Time-weighted last-trade price sampled each second of the minute."""
    r = H.get("https://api.kraken.com/0/public/Trades", params={"pair": pair, "since": (t - 120) * 10 ** 9},
              timeout=15).json()
    if r.get("error"):
        return None
    trades = sorted((float(x[2]), float(x[0])) for x in next(iter(r["result"].values())) if isinstance(x, list))
    if not trades or trades[-1][0] < t + 59:
        return None
    px, out, i = None, [], 0
    for s in range(t - 120, t + 60):
        while i < len(trades) and trades[i][0] <= s:
            px = trades[i][1]
            i += 1
        if s >= t and px:
            out.append(px)
    return statistics.mean(out) if out else None


def events():
    out = []
    for d in ("cache", "cache_holdout"):
        for p in sorted((Path(__file__).parent / d).glob("*.json.gz")):
            with gzip.open(p, "rt") as f:
                ev = json.load(f)
            out.append((ev["series"], ev["event"], ev["close"]))
    return out


def main():
    client = build_client(CONFIG)
    rows = []
    for series, ev, close in events():
        for attempt in range(6):
            try:
                m = client._request("GET", "/markets", params={"event_ticker": ev, "limit": 1},
                                    authenticated=False)["markets"][0]
                break
            except Exception:
                time.sleep(5 * (attempt + 1))
        truth = float(m["expiration_value"])
        t = close - 60
        p = PAIRS[series]
        vals = {"coinbase": coinbase_min(p["coinbase"], t), "bitstamp": bitstamp_min(p["bitstamp"], t),
                "kraken": kraken_min(p["kraken"], t)}
        time.sleep(0.4)
        live = [v for v in vals.values() if v]
        vals["median"] = statistics.median(live) if live else None
        vals["mean"] = statistics.mean(live) if live else None
        rows.append((series, ev, truth, vals))
        print(ev, f"truth {truth:,.2f}", " ".join(f"{k} {((v / truth - 1) * 1e4) if v else float('nan'):+.2f}bp"
                                                   for k, v in vals.items()), flush=True)
    print("\nError vs settlement, basis points (1bp = 0.01%; BTC 1bp ≈ $8):")
    for series in PAIRS:
        print(f"  {series}")
        for k in ("coinbase", "bitstamp", "kraken", "median", "mean"):
            e = [(v[k] / truth - 1) * 1e4 for s, _, truth, v in rows if s == series and v.get(k)]
            if e:
                print(f"    {k:<9} n={len(e):>2}  mean {statistics.mean(e):+.2f}  "
                      f"mean |err| {statistics.mean(abs(x) for x in e):.2f}  worst {max(e, key=abs):+.2f}")
    (Path(__file__).parent / "index_check.json").write_text(json.dumps(rows))


if __name__ == "__main__":
    main()
