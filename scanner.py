"""
Finds markets worth trading, across ALL of Kalshi (~13k events, ~128k
markets) instead of the original 8 hand-picked series.

Each market that traded in the last 24h is matched to the strategy that
fits it, and ranked by a rough estimate of daily edge:

  FAIR-VALUE  a tradeable pricing model exists (crypto, >2h from close).
              Edge comes from quoting around the model and taking when the
              book is mispriced vs it — so tight spreads are fine.
  BOOK        no model, but the spread pays for both fees + min_edge and
              the market really trades (24h volume ≥ MIN_VOLUME_24H).
  —           not suitable; `reasons` says why.

Lessons baked in:
  - Rank by real 24h volume, not book depth (stale Fed-market depth gave 0
    fills in 5 hours).
  - Skip markets closing within MIN_HOURS_TO_CLOSE (the first paper fill
    went 53¢ → 0 an hour from close).
  - One-way order flow (the Fed-market problem) isn't visible in the
    catalog; check it on a shortlist with simulate.py.

The "est. edge/day" column is a ranking heuristic, not a forecast: it
assumes the bot captures CAPTURE_SHARE of 24h volume at its quoted edge.
Run simulate.py on a shortlisted market for a real estimate.

Usage:
    python scanner.py [--top 40] [--category Crypto]
"""
import argparse
from datetime import datetime, timezone

import catalog
import fair_value
from config import CONFIG
from fees import per_contract_fee_cents
from kalshi_client import build_client, dollars_to_cents

MIN_HOURS_TO_CLOSE = 2.0
MIN_VOLUME_24H = 100          # contracts
# A 2¢/96¢ book isn't a 94¢ opportunity — it means nobody knows the price,
# and the only takers who'd hit a quote inside it know more than we do.
MAX_BOOK_SPREAD = 15
CAPTURE_SHARE = 0.10          # share of 24h volume the bot might trade
MAX_DAILY_CONTRACTS = 200     # cap on that, per market


def _hours_until(iso_ts):
    if not iso_ts:
        return None
    try:
        close = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (close - datetime.now(timezone.utc)).total_seconds() / 3600


def evaluate(m: dict, event: dict, cfg=CONFIG.risk, with_fv: bool = True) -> dict:
    bid = dollars_to_cents(m.get("yes_bid_dollars")) or None
    ask = dollars_to_cents(m.get("yes_ask_dollars"))
    ask = ask if ask and ask < 100 else None
    spread = (ask - bid) if bid is not None and ask is not None else None
    mid = (bid + ask) / 2 if spread is not None else None
    vol24 = float(m.get("volume_24h_fp") or 0)
    hours = _hours_until(m.get("close_time"))
    contracts_day = min(vol24 * CAPTURE_SHARE, MAX_DAILY_CONTRACTS)

    row = {
        "ticker": m["ticker"], "event": event.get("event_ticker"), "category": event.get("category") or "",
        "title": event.get("title") or m.get("title") or "", "subtitle": m.get("yes_sub_title") or "",
        "yes_bid": bid, "yes_ask": ask, "spread": spread, "volume_24h": vol24,
        "open_interest": float(m.get("open_interest_fp") or 0),
        "hours_to_close": round(hours, 1) if hours is not None else None,
        "strategy": None, "fv": None, "fv_edge": None, "edge_per_contract": None,
        "est_edge_day_usd": 0.0, "reasons": [],
    }
    reasons = row["reasons"]
    if hours is not None and hours < MIN_HOURS_TO_CLOSE:
        reasons.append(f"closes in {hours:.1f}h")
    if vol24 <= 0:
        reasons.append("no 24h volume")

    fv = fair_value.fair_value(m) if with_fv and fair_value.supported(m) and not reasons else None
    if fv is not None:
        row["fv"] = round(fv.cents, 1)
        if fv.tradeable:
            take_buy = (fv.cents - ask - per_contract_fee_cents(ask, maker=False)) if ask is not None else -99
            take_sell = (bid - fv.cents - per_contract_fee_cents(bid, maker=False)) if bid is not None else -99
            row["fv_edge"] = round(max(take_buy, take_sell), 1)
            row["strategy"] = "fair-value"
            # Maker quotes sit min_edge beyond fees around the model price.
            row["edge_per_contract"] = cfg.min_edge_cents
            row["est_edge_day_usd"] = round(contracts_day * cfg.min_edge_cents / 100
                                            + max(0, row["fv_edge"]) * cfg.take_size / 100, 2)
            row["tradeable"] = not reasons
            return row
        reasons.append(f"{fv.model} model display-only")

    if spread is None:
        reasons.append("one-sided book")
    elif mid is not None:
        need = 2 * per_contract_fee_cents(mid) + cfg.min_edge_cents
        if spread < need:
            reasons.append(f"spread {spread}¢ < {need:.1f}¢")
        elif spread > MAX_BOOK_SPREAD:
            reasons.append(f"spread {spread}¢ > {MAX_BOOK_SPREAD}¢ (no consensus price)")
        elif vol24 < MIN_VOLUME_24H:
            reasons.append(f"24h vol {vol24:.0f} < {MIN_VOLUME_24H}")
        else:
            captured = (spread - 2 * cfg.quote_inset_cents) / 2 - per_contract_fee_cents(mid)
            row["edge_per_contract"] = round(captured, 2)
            if captured > 0 and not reasons:
                row["strategy"] = "book"
                row["est_edge_day_usd"] = round(contracts_day * captured / 100, 2)
            elif captured <= 0:
                reasons.append("no edge after fees")
    row["tradeable"] = row["strategy"] is not None and not reasons
    return row


def scan(client, force: bool = False, category: str | None = None, with_fv: bool = True) -> list[dict]:
    rows = []
    for e in catalog.load_events(client, force=force):
        if category and (e.get("category") or "").lower() != category.lower():
            continue
        for m in e.get("markets") or []:
            if m.get("status") not in ("active", "open") or float(m.get("volume_24h_fp") or 0) <= 0:
                continue
            rows.append(evaluate(m, e, with_fv=with_fv))
    rows.sort(key=lambda r: (r["tradeable"], r["est_edge_day_usd"], r["volume_24h"]), reverse=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--category")
    args = ap.parse_args()
    client = build_client(CONFIG)
    print("Sweeping every open market (≈15s)...")
    rows = scan(client, force=True, category=args.category)

    tradeable = [r for r in rows if r["tradeable"]]
    # Strategies are listed separately: fair-value edge rests on a validated
    # model; book edge is theoretical spread capture that one-way flow can
    # erase (see simulate.py on the Fed markets). Don't rank them together.
    for strat, label in (("fair-value", "FAIR-VALUE — model-priced (crypto)"),
                         ("book", "BOOK — spread capture, no model")):
        subset = [r for r in tradeable if r["strategy"] == strat][:args.top]
        print(f"\n{label}")
        print(f"{'TICKER':<38} {'VOL 24H':>9} {'BID':>4} {'ASK':>4} {'FV':>5} {'TAKE':>5} "
              f"{'EDGE/C':>6} {'$/DAY*':>7} {'CLOSE':>6}  MARKET")
        print("-" * 118)
        for r in subset:
            h = r["hours_to_close"]
            close = "-" if h is None else f"{h / 24:.0f}d" if h > 48 else f"{h:.0f}h"
            print(f"{r['ticker'][:38]:<38} {r['volume_24h']:>9.0f} "
                  f"{str(r['yes_bid'] or '-'):>4} {str(r['yes_ask'] or '-'):>4} {str(r['fv'] or '-'):>5} "
                  f"{str(r['fv_edge'] if r['fv_edge'] is not None else '-'):>5} "
                  f"{str(r['edge_per_contract'] if r['edge_per_contract'] is not None else '-'):>6} "
                  f"{r['est_edge_day_usd']:>7.2f} {close:>6}  {(r['title'] + ' · ' + r['subtitle'])[:40]}")
        if not subset:
            print("  (none right now)")
    by = {}
    for r in tradeable:
        by[r["strategy"]] = by.get(r["strategy"], 0) + 1
    print(f"\n{len(rows):,} markets traded in the last 24h · {len(tradeable)} suitable "
          f"({', '.join(f'{v} {k}' for k, v in by.items()) or 'none'})")
    print("*$/day is a ranking heuristic (10% of 24h volume at the quoted edge), not a forecast —")
    print(" run simulate.py on a shortlist for a real estimate.")


if __name__ == "__main__":
    main()
