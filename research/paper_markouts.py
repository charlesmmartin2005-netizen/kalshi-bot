"""
Is the paper bot's edge real? Markouts for every fill in fills_log.jsonl.

A markout is how far the market mid moved in the fill's favour h minutes
later, per contract: (mid - price) for a buy, (price - mid) for a sell. Net
of the fee it is the edge the fill actually captured. Unlike daily P&L, a
week of fills is enough to see it: P&L at settlement swings on one BTC close
per day, a 5-minute markout doesn't. (The scoring lives in markouts.py; the
dashboard's Edge view shows today's.)

    python research/paper_markouts.py              # all paper fills
    python research/paper_markouts.py --since 2026-10-22
    python research/paper_markouts.py --since 2026-10-22 --ledger fills_log.server.jsonl

The last table scores each take as if it were closed at the touch N minutes
later (paying the spread and the taker fee both ways) against holding it to
settlement — the live check on research/strategy_lab.py's `holds`.
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import markouts  # noqa: E402
from config import CONFIG  # noqa: E402
from fees import order_fee_cents  # noqa: E402
from kalshi_client import build_client, dollars_to_cents  # noqa: E402

EXIT_MINUTES = (1, 5, 15, 60, 120)


def minute_touches(client, ticker, start, end):
    """{minute_start_ts: (yes_bid, yes_ask)} from 1-minute candles (close of
    each minute); either side None when the book was empty there."""
    series, out = ticker.split("-")[0], {}
    for s in range(start, end, 4800 * 60):
        r = client._request("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                            params={"start_ts": s, "end_ts": min(end, s + 4800 * 60), "period_interval": 1},
                            authenticated=False)
        for c in r.get("candlesticks", []):
            b = dollars_to_cents(c.get("yes_bid", {}).get("close_dollars"))
            a = dollars_to_cents(c.get("yes_ask", {}).get("close_dollars"))
            out[c["end_period_ts"] - 60] = (b or None, a if a and a < 100 else None)
    return out


def take_exits(client, scored):
    """Each settled take, in cents after fees: held to settlement, and closed
    at the touch N minutes later (bid for a long, ask for a short; taker fee
    both ways). A take with no touch by then (market closed) settles."""
    by_t, out = defaultdict(list), []
    for f in scored:
        if f.get("liquidity") == "taker" and f["settle"] is not None:
            by_t[f["ticker"]].append(f)
    for t, fs in by_t.items():
        start = int(min(f["ts"] for f in fs)) // 60 * 60
        touches = minute_touches(client, t, start, int(max(f["ts"] for f in fs)) + (max(EXIT_MINUTES) + 6) * 60)
        for f in fs:
            buy = (f["action"] == "buy") == (f["side"] == "yes")      # long YES
            price = f["price_cents"] if f["side"] == "yes" else 100 - f["price_cents"]
            n, fee = f["count"], order_fee_cents(f["price_cents"], f["count"], maker=False)
            row = {**f, "pnl": {"settle": f["settle"] * n - fee}}
            for h in EXIT_MINUTES:
                m, px = int(f["ts"]) // 60 * 60 + h * 60, None
                for back in range(0, 5):            # tolerate a few quiet minutes, like markouts.mid_after
                    px = touches.get(m - back * 60, (None, None))[0 if buy else 1]
                    if px is not None:
                        break
                row["pnl"][h] = (row["pnl"]["settle"] if px is None else
                                 (px - price if buy else price - px) * n - fee - order_fee_cents(px, n, maker=False))
            out.append(row)
    return out


def exit_table(rows):
    """Per-contract and per-day P&L of the takes for each exit rule."""
    n = sum(r["count"] for r in rows)
    days = defaultdict(list)
    for r in rows:
        days[r["ticker"].split("-")[1][:7]].append(r)   # 26OCT2817 and 26OCT2817B are one day's event
    print(f"\ntakes closed at the touch N minutes later vs held to settlement, after fees "
          f"({len(rows)} settled takes, {n} contracts, {len(days)} days)")
    print(f"  {'':<14} {'¢/contract':>10} {'total':>9} {'sd/day':>7} {'worst day':>9}   by day: "
          + " ".join(f"{d[2:7]:>7}" for d in sorted(days)))
    for key in ("settle",) + EXIT_MINUTES:
        day = {d: sum(r["pnl"][key] for r in rs) / 100 for d, rs in days.items()}
        tot = sum(day.values())
        sd = (sum((x - tot / len(day)) ** 2 for x in day.values()) / len(day)) ** 0.5
        label = "held to settle" if key == "settle" else f"exit {key:>3}m"
        print(f"  {label:<14} {tot * 100 / n:>+9.2f}¢ {tot:>+8.2f}$ {sd:>6.2f}$ {min(day.values()):>+8.2f}$            "
              + " ".join(f"{day[d]:>+7.2f}" for d in sorted(days)))


def row(label, s):
    cells = "  ".join(f"{h:>2}m {s['mo'][h]:+6.2f}" if s["mo"][h] is not None else f"{h:>2}m    n/a"
                      for h in markouts.HORIZONS)
    settle = f"settle {s['settle']:+6.2f}" if s["settle"] is not None else "settle    n/a"
    print(f"  {label:<22} {s['fills']:>5} fills {s['contracts']:>7} contracts | {cells} | fee {s['fee']:4.2f} | {settle}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only fills on or after this date (YYYY-MM-DD, UTC)")
    ap.add_argument("--ledger", help="fills log to score (default: this checkout's fills_log.jsonl; the "
                                     "bot runs on the server, so copy its log down first)")
    args = ap.parse_args()
    if args.ledger:
        ledger.LEDGER_PATH = args.ledger
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() if args.since else 0
    fills = [f for f in ledger.read_all_fills() if f.get("mode", "paper") == "paper" and f["ts"] >= since]
    if not fills:
        print("No paper fills to score.")
        return
    client = build_client(CONFIG)
    scored = markouts.score(client, fills)

    print(f"{len(fills)} paper fills, {datetime.fromtimestamp(fills[0]['ts'], timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(fills[-1]['ts'], timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("Markouts in ¢ per contract (positive = the market moved our way). Edge after fees = markout − fee.")
    row("all", markouts.summarize(scored))
    for key, label in (("liquidity", "by liquidity"), ("strategy", "by strategy")):
        print(label)
        for k, s in markouts.by(scored, lambda f, key=key: f.get(key) or "?").items():
            row(k, s)
    print("by coin")
    for k, s in markouts.by(scored, lambda f: f["ticker"].split("-")[0]).items():
        row(k, s)
    gapped = [f for f in scored if f.get("edge_cents") is not None]
    if gapped:
        for kind, label in (("taker", "takes"), ("maker", "resting quotes")):
            fs = [f for f in gapped if f.get("liquidity", "maker") == kind]
            if fs:
                print(f"{label} by the model's gap after fee")
                for k, s in markouts.by(fs, markouts.gap_bucket).items():
                    row(k, s)
    exits = take_exits(client, scored)
    if exits:
        exit_table(exits)
    days = len({datetime.fromtimestamp(f["ts"], timezone.utc).date() for f in fills})
    if days < 5:
        print(f"\nOnly {days} day(s) of fills: treat these numbers as anecdotes, not evidence.")


if __name__ == "__main__":
    main()
