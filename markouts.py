"""
Markouts: how far the market mid moved in a fill's favour 1, 5 and 60
minutes later, per contract — (mid - price) for a buy, (price - mid) for a
sell. Net of the fee it is the edge the fill actually kept. Unlike daily
P&L, a week of fills is enough to see it: P&L at settlement swings on one
BTC close per day, a 5-minute markout doesn't.

Used by the dashboard's Edge view and research/paper_markouts.py.
"""
from collections import defaultdict

from fees import per_contract_fee_cents
from kalshi_client import dollars_to_cents

HORIZONS = (1, 5, 60)


def minute_mids(client, ticker, start, end):
    """{minute_start_ts: mid_cents} from 1-minute candles (close of each minute)."""
    series = ticker.split("-")[0]
    out = {}
    for s in range(start, end, 4800 * 60):
        r = client._request("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                            params={"start_ts": s, "end_ts": min(end, s + 4800 * 60), "period_interval": 1},
                            authenticated=False)
        for c in r.get("candlesticks", []):
            b = dollars_to_cents(c.get("yes_bid", {}).get("close_dollars"))
            a = dollars_to_cents(c.get("yes_ask", {}).get("close_dollars"))
            if b and a and a < 100:
                out[c["end_period_ts"] - 60] = (a + b) / 2
    return out


def mid_after(mids, ts, h):
    """Mid at the close of the minute h minutes after the fill (None if no quote by then)."""
    m = int(ts) // 60 * 60 + h * 60
    for back in range(0, 5):            # tolerate a few quiet minutes
        if m - back * 60 in mids:
            return mids[m - back * 60]
    return None


def score(client, fills, settled=None):
    """Return the fills as dicts with "mo" ({h: cents or None}), "fee" (cents
    per contract) and "settle" (cents per contract, or None while open).
    settled: optional {ticker: 100 | 0 | None} cache, filled as it goes."""
    settled = {} if settled is None else settled
    by_t = defaultdict(list)
    for f in fills:
        by_t[f["ticker"]].append(f)
    out = []
    for t, fs in by_t.items():
        start = int(min(f["ts"] for f in fs)) // 60 * 60 - 60
        end = int(max(f["ts"] for f in fs)) + (max(HORIZONS) + 6) * 60
        mids = minute_mids(client, t, start, end)
        if settled.get(t) is None:
            m = client.get_market(t).get("market", {})
            settled[t] = (100 if m.get("result") == "yes" else 0) if m.get("status") == "finalized" else None
        for f in fs:
            sgn = 1 if f["action"] == "buy" else -1
            price = f["price_cents"] if f["side"] == "yes" else 100 - f["price_cents"]
            sgn = sgn if f["side"] == "yes" else -sgn
            out.append({**f,
                        "mo": {h: (sgn * (mid - price) if (mid := mid_after(mids, f["ts"], h)) is not None else None)
                               for h in HORIZONS},
                        "fee": per_contract_fee_cents(f["price_cents"], maker=f.get("liquidity", "maker") == "maker"),
                        "settle": sgn * (settled[t] - price) if settled[t] is not None else None})
    return out


def summarize(scored):
    """Contract-weighted averages for a group of scored fills."""
    n = sum(f["count"] for f in scored)
    if not n:
        return {"fills": 0, "contracts": 0, "mo": {h: None for h in HORIZONS}, "fee": None, "settle": None}

    def avg(get):
        xs = [(get(f), f["count"]) for f in scored if get(f) is not None]
        c = sum(k for _, k in xs)
        return round(sum(x * k for x, k in xs) / c, 2) if c else None
    return {"fills": len(scored), "contracts": n,
            "mo": {h: avg(lambda f, h=h: f["mo"][h]) for h in HORIZONS},
            "fee": round(sum(f["fee"] * f["count"] for f in scored) / n, 2),
            "settle": avg(lambda f: f["settle"])}


def by(scored, key):
    groups = defaultdict(list)
    for f in scored:
        groups[key(f)].append(f)
    return {k: summarize(v) for k, v in sorted(groups.items())}
