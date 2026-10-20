"""
Structural arbitrage finder. These trades need no forecast — the payoff is
locked in by how the contracts relate to each other:

  SELL-ALL (mutually exclusive event): at most one outcome can be YES, so
    buying NO on every outcome pays at least (n-1) × $1. If the YES bids
    add up to more than $1 (+ fees), that's guaranteed profit. Riskless for
    any mutually exclusive event.

  BUY-ALL (mutually exclusive AND exhaustive event): exactly one outcome is
    YES, so buying YES on every outcome pays exactly $1. If the asks add up
    to less than $1 (− fees), that's guaranteed profit — but only if the
    outcomes really cover everything. Marked riskless only when the strikes
    provably cover the whole number line; otherwise flagged "verify".

  LADDER (strike ladders like "BTC above $X" or "high above X°"):
    P(above lower strike) can never be less than P(above higher strike).
    If YES on the lower strike is offered below the bid on the higher one,
    buy YES low + buy NO high: you collect the gap whatever happens.

Prices come from the catalog snapshot, then every candidate is re-checked
against the live order book (with real sizes) before it's reported, so
stale snapshot quotes don't produce phantom opportunities. Every confirmed
opportunity is appended to arb_log.jsonl, so over time you can see how
often they really appear and how big they are.

Execution is NOT automated: legs fill separately, and a half-filled arb is
just a position. Use this to measure whether the opportunities are real
and frequent before deciding to automate them.

Usage:
    python arb.py
"""
import json
import os
import re
import time

import catalog
from config import CONFIG
from fees import order_fee_cents
from kalshi_client import build_client, dollars_to_cents, normalize_orderbook

ARB_LOG = os.path.join(os.path.dirname(__file__), "arb_log.jsonl")
MIN_EDGE_CENTS = 1.0      # per set, after taker fees
STRIKE_SUFFIX = re.compile(r"-T\d+(\.\d+)?$")


def _quotes(m):
    bid = dollars_to_cents(m.get("yes_bid_dollars"))
    ask = dollars_to_cents(m.get("yes_ask_dollars"))
    return (bid if bid else None), (ask if ask and ask < 100 else None)


def _exhaustive(markets) -> bool:
    """True when strikes provably cover every outcome: one 'less' tail, one
    'greater' tail, and 'between' brackets that tile the gap with no holes
    (integer strikes, as in temperature markets)."""
    less = [m for m in markets if m.get("strike_type") == "less"]
    greater = [m for m in markets if m.get("strike_type") == "greater"]
    between = sorted((m for m in markets if m.get("strike_type") == "between"),
                     key=lambda m: m["floor_strike"])
    if len(less) != 1 or len(greater) != 1 or len(less) + len(greater) + len(between) != len(markets):
        return False
    edge = less[0]["cap_strike"]                 # covers < cap
    for b in between:
        if b["floor_strike"] != edge:
            return False
        edge = b["cap_strike"] + 1               # inclusive integer brackets
    return greater[0]["floor_strike"] == edge - 1


def _fee(price, n):
    return order_fee_cents(price, n, maker=False)


def find_candidates(events) -> list[dict]:
    out = []
    for e in events:
        ms = [m for m in (e.get("markets") or []) if m.get("status") in ("active", "open")]
        if len(ms) < 2:
            continue
        q = {m["ticker"]: _quotes(m) for m in ms}

        if e.get("mutually_exclusive"):
            bids = [q[m["ticker"]][0] for m in ms]
            if all(b is not None for b in bids):
                gross = sum(bids) - 100
                fees = sum(_fee(100 - b, 1) for b in bids)
                if gross - fees >= MIN_EDGE_CENTS:
                    out.append({"type": "sell-all", "event": e["event_ticker"], "title": e.get("title", ""),
                                "legs": [("buy NO", m["ticker"]) for m in ms], "riskless": True})
            # Buy-all is only arbitrage if exactly one outcome MUST win. Lists
            # of candidates ("next Pope", "next COO") usually omit "someone
            # else", so all of them can resolve NO — not arbitrage. Only take
            # events whose strikes provably cover every outcome.
            asks = [q[m["ticker"]][1] for m in ms]
            if _exhaustive(ms) and all(a is not None for a in asks):
                gross = 100 - sum(asks)
                fees = sum(_fee(a, 1) for a in asks)
                if gross - fees >= MIN_EDGE_CENTS:
                    out.append({"type": "buy-all", "event": e["event_ticker"], "title": e.get("title", ""),
                                "legs": [("buy YES", m["ticker"]) for m in ms], "riskless": True})

        for kind in ("greater", "less"):
            # A real ladder = same underlying, same close, different price
            # strikes: tickers end in -T<strike> (KXBTCD-…-T81999.99,
            # KXHIGHNY-…-T72). This excludes look-alikes that broke the naive
            # version: "S&P above 10,000 by <date>" (same strike, different
            # deadlines) and per-team seed over/unders (different teams).
            key = "floor_strike" if kind == "greater" else "cap_strike"
            ladder = [m for m in ms if m.get("strike_type") == kind and m.get(key) is not None
                      and STRIKE_SUFFIX.search(m["ticker"])]
            ladder.sort(key=lambda m: m[key])
            for i, lo in enumerate(ladder):
                for hi in ladder[i + 1:]:
                    if hi[key] <= lo[key] or hi.get("close_time") != lo.get("close_time"):
                        continue
                    # "greater": P(lo) >= P(hi) → buy YES lo, buy NO hi.
                    # "less":    P(hi) >= P(lo) → buy YES hi, buy NO lo.
                    rich, cheap = (hi, lo) if kind == "greater" else (lo, hi)
                    bid_rich, ask_cheap = q[rich["ticker"]][0], q[cheap["ticker"]][1]
                    if bid_rich is None or ask_cheap is None:
                        continue
                    if bid_rich - ask_cheap - _fee(ask_cheap, 1) - _fee(100 - bid_rich, 1) >= MIN_EDGE_CENTS:
                        out.append({"type": "ladder", "event": e["event_ticker"], "title": e.get("title", ""),
                                    "legs": [("buy YES", cheap["ticker"]), ("buy NO", rich["ticker"])],
                                    "riskless": True})
    return out


def confirm(client, cand) -> dict | None:
    """Re-price a candidate on live order books with real sizes."""
    books = {}
    for _, t in cand["legs"]:
        books[t] = normalize_orderbook(client.get_orderbook(t, depth=1))
    cost, size, legs = 0, None, []
    for action, t in cand["legs"]:
        b = books[t]
        if action == "buy YES":
            price = b["yes_ask"]
            lvl = b["raw_no_levels"]               # YES asks come from NO bids
        else:
            price = None if b["yes_bid"] is None else 100 - b["yes_bid"]
            lvl = b["raw_yes_levels"]              # NO asks come from YES bids
        if price is None:
            return None
        avail = max(lvl, key=lambda l: l[0])[1] if lvl else 0
        size = avail if size is None else min(size, avail)
        cost += price
        legs.append({"action": action, "ticker": t, "price": price})
    n = int(size or 0)
    if n < 1:
        return None
    if cand["type"] == "sell-all":
        payout = (len(legs) - 1) * 100
    elif cand["type"] == "buy-all":
        payout = 100
    else:
        payout = 100                               # ladder: worst case collects the gap
    fees = sum(_fee(l["price"], n) for l in legs)
    edge_total = (payout - cost) * n - fees
    if edge_total < MIN_EDGE_CENTS * n:
        return None
    return {**cand, "legs": legs, "size": n, "cost_per_set": cost, "payout_per_set": payout,
            "fees_cents": fees, "profit_usd": round(edge_total / 100, 2),
            "edge_per_set_cents": round(edge_total / n, 2), "ts": time.time()}


def scan(client, force: bool = False) -> dict:
    events = catalog.load_events(client, force=force)
    cands = find_candidates(events)
    confirmed = []
    for c in cands[:40]:          # each candidate costs one order book call per leg
        try:
            r = confirm(client, c)
        except Exception:
            r = None
        if r:
            confirmed.append(r)
    confirmed.sort(key=lambda r: -r["profit_usd"])
    if confirmed:
        with open(ARB_LOG, "a") as f:
            for r in confirmed:
                f.write(json.dumps(r) + "\n")
    return {"ts": time.time(), "events_scanned": len(events), "candidates": len(cands),
            "confirmed": confirmed}


def main():
    client = build_client(CONFIG)
    print("Sweeping every open event...")
    res = scan(client, force=True)
    print(f"{res['events_scanned']:,} events · {res['candidates']} snapshot candidates · "
          f"{len(res['confirmed'])} confirmed on live books\n")
    for r in res["confirmed"]:
        tag = "RISKLESS" if r["riskless"] else "VERIFY EXHAUSTIVE"
        print(f"[{r['type']}] {r['event']} — {r['title'][:60]}  ({tag})")
        for l in r["legs"]:
            print(f"    {l['action']:<8} {l['ticker']:<40} @ {l['price']}¢")
        print(f"    size {r['size']} sets · cost {r['cost_per_set']}¢ → pays ≥ {r['payout_per_set']}¢ · "
              f"fees {r['fees_cents']}¢ · profit ${r['profit_usd']:.2f}\n")
    if not res["confirmed"]:
        print("No arbitrage right now. That's normal — run it again later, and check")
        print("arb_log.jsonl over a few days to see how often gaps actually appear.")


if __name__ == "__main__":
    main()
