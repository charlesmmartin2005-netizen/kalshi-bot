"""
Is the paper bot's edge real? Markouts for every fill in fills_log.jsonl.

A markout is how far the market mid moved in the fill's favour h minutes
later, per contract: (mid - price) for a buy, (price - mid) for a sell. Net
of the fee it is the edge the fill actually captured. Unlike daily P&L, a
week of fills is enough to see it: P&L at settlement swings on one BTC close
per day, a 5-minute markout doesn't.

    python research/paper_markouts.py              # all paper fills
    python research/paper_markouts.py --since 2026-10-22
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
from config import CONFIG  # noqa: E402
from fees import per_contract_fee_cents  # noqa: E402
from kalshi_client import build_client, dollars_to_cents  # noqa: E402

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only fills on or after this date (YYYY-MM-DD, UTC)")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() if args.since else 0
    fills = [f for f in ledger.read_all_fills() if f.get("mode", "paper") == "paper" and f["ts"] >= since]
    if not fills:
        print("No paper fills to score.")
        return
    client = build_client(CONFIG)
    by_t = defaultdict(list)
    for f in fills:
        by_t[f["ticker"]].append(f)
    settled = {}
    for t, fs in by_t.items():
        start = int(min(f["ts"] for f in fs)) // 60 * 60 - 60
        end = int(max(f["ts"] for f in fs)) + (max(HORIZONS) + 6) * 60
        mids = minute_mids(client, t, start, end)
        m = client.get_market(t).get("market", {})
        settled[t] = (100 if m.get("result") == "yes" else 0) if m.get("status") == "finalized" else None
        for f in fs:
            sgn = 1 if f["action"] == "buy" else -1
            price = f["price_cents"] if f["side"] == "yes" else 100 - f["price_cents"]
            sgn = sgn if f["side"] == "yes" else -sgn
            f["_mo"] = {h: (sgn * (mid - price) if (mid := mid_after(mids, f["ts"], h)) is not None else None)
                        for h in HORIZONS}
            f["_fee"] = per_contract_fee_cents(f["price_cents"], maker=f.get("liquidity", "maker") == "maker")
            f["_settle"] = sgn * (settled[t] - price) if settled[t] is not None else None

    def row(label, fs):
        n = sum(f["count"] for f in fs)
        cells = []
        for h in HORIZONS:
            xs = [(f["_mo"][h], f["count"]) for f in fs if f["_mo"][h] is not None]
            c = sum(k for _, k in xs)
            cells.append(f"{h:>2}m {sum(x * k for x, k in xs) / c:+6.2f}" if c else f"{h:>2}m    n/a")
        fee = sum(f["_fee"] * f["count"] for f in fs) / n
        st = [(f["_settle"], f["count"]) for f in fs if f["_settle"] is not None]
        sc = sum(k for _, k in st)
        settle = f"settle {sum(x * k for x, k in st) / sc:+6.2f}" if sc else "settle    n/a"
        print(f"  {label:<22} {len(fs):>5} fills {n:>7} contracts | " + "  ".join(cells)
              + f" | fee {fee:4.2f} | {settle}")

    print(f"{len(fills)} paper fills, {datetime.fromtimestamp(fills[0]['ts'], timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(fills[-1]['ts'], timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("Markouts in ¢ per contract (positive = the market moved our way). Edge after fees = markout − fee.")
    row("all", fills)
    for key, label in (("liquidity", "by liquidity"), ("strategy", "by strategy")):
        print(label)
        g = defaultdict(list)
        for f in fills:
            g[f.get(key) or "?"].append(f)
        for k in sorted(g):
            row(k, g[k])
    print("by coin")
    g = defaultdict(list)
    for f in fills:
        g[f["ticker"].split("-")[0]].append(f)
    for k in sorted(g):
        row(k, g[k])
    days = len({datetime.fromtimestamp(f["ts"], timezone.utc).date() for f in fills})
    if days < 5:
        print(f"\nOnly {days} day(s) of fills: treat these numbers as anecdotes, not evidence.")


if __name__ == "__main__":
    main()
