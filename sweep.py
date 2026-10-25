"""
One sweep for the dashboard's Scanner view: every open market ranked by
strategy (scanner.py) plus confirmed arbitrage (arb.py), printed as one line
of JSON. The dashboard runs this in a process of its own every few minutes.
Inside the trading process, the ~150k-market catalog took ~600 MB and
freeing each old one froze every thread for 1-5s (2026-10-25: books went
stale and quotes were pulled at the end of each sweep). Here it goes away
with the process.

Usage:
    python sweep.py
"""
import json
import logging
import time

import arb
import scanner
from config import CONFIG
from kalshi_client import build_client


def run(client) -> dict:
    t0 = time.time()
    rows = scanner.scan(client, force=True)
    suitable = [r for r in rows if r["tradeable"]]
    scan = {
        "ts": time.time(), "markets_active": len(rows),
        "counts": {s: sum(1 for r in suitable if r["strategy"] == s) for s in ("fair-value", "book")},
        # top of each strategy list + the best rejected ones (for "why not")
        "rows": ([r for r in suitable if r["strategy"] == "fair-value"][:40]
                 + [r for r in suitable if r["strategy"] == "book"][:40]
                 + [r for r in rows if not r["tradeable"]][:20]),
    }
    found = arb.scan(client)        # reuses the catalog just loaded
    return {"scan": scan, "arb": found, "seconds": round(time.time() - t0, 1)}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    client = build_client(CONFIG)
    # It shares Kalshi's rate limit with the trading loop in the other
    # process, so it keeps to the low-priority rate.
    client.low_priority = True
    print(json.dumps(run(client), default=str))


if __name__ == "__main__":
    main()
