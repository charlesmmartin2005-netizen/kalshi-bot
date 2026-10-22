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
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import markouts  # noqa: E402
from config import CONFIG  # noqa: E402
from kalshi_client import build_client  # noqa: E402


def row(label, s):
    cells = "  ".join(f"{h:>2}m {s['mo'][h]:+6.2f}" if s["mo"][h] is not None else f"{h:>2}m    n/a"
                      for h in markouts.HORIZONS)
    settle = f"settle {s['settle']:+6.2f}" if s["settle"] is not None else "settle    n/a"
    print(f"  {label:<22} {s['fills']:>5} fills {s['contracts']:>7} contracts | {cells} | fee {s['fee']:4.2f} | {settle}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only fills on or after this date (YYYY-MM-DD, UTC)")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() if args.since else 0
    fills = [f for f in ledger.read_all_fills() if f.get("mode", "paper") == "paper" and f["ts"] >= since]
    if not fills:
        print("No paper fills to score.")
        return
    scored = markouts.score(build_client(CONFIG), fills)

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
    days = len({datetime.fromtimestamp(f["ts"], timezone.utc).date() for f in fills})
    if days < 5:
        print(f"\nOnly {days} day(s) of fills: treat these numbers as anecdotes, not evidence.")


if __name__ == "__main__":
    main()
