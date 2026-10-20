"""
Chooses which markets the bot should watch, following what testing showed
works for the fair-value strategy:

  - BTC and ETH daily markets (5pm ET close), the setup the backtests tested.
  - The soonest event with at least MIN_HOURS left (the model isn't trusted
    in the last 2h), up to MAX_HOURS out.
  - Strikes priced nearest 50¢, where prices react most to spot moves and
    so where Kalshi quotes lag most. Deep in-the-money strikes (~95¢) barely
    move and give the bot almost nothing to catch.
  - Only strikes that traded in the last 24h, have a two-sided book, and get
    a tradeable model price (the ladder vol fit worked).
  - 6 BTC + 3 ETH, the "rolling ladder" strategy
    (research/README.md). The engine re-runs this every 15 minutes
    (auto-rotate) so the watched strikes follow the coin as it moves —
    fixed strikes drift to 95¢/5¢ where nothing trades; rolling roughly
    tripled fills in the backtest.

Usage:
    python autopick.py          # print today's picks
"""
from datetime import datetime, timezone

import fair_value
from config import CONFIG
from kalshi_client import build_client, dollars_to_cents

PLAN = (("KXBTCD", 6), ("KXETHD", 3))
# Daily events open 25h before close; the backtests only traded that
# window. Between ~3 and 4pm ET (today's event inside its last 2h, tomorrow's
# not open yet) there is deliberately nothing to pick.
MIN_HOURS, MAX_HOURS = 2.0, 26.0
MAX_SPREAD = 4
PRICE_BAND = (10, 90)


def _hours(iso):
    return (datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 3600


def _choose_event(client, series):
    """Soonest 5pm-ET event (ticker ends in 17) closing within the window."""
    events = client._request("GET", "/events", params={
        "series_ticker": series, "status": "open", "limit": 200, "with_nested_markets": "true"},
        authenticated=False).get("events", [])
    scored = []
    for e in events:
        ms = e.get("markets") or []
        if not ms or not ms[0].get("close_time"):
            continue
        h = _hours(ms[0]["close_time"])
        if not MIN_HOURS <= h <= MAX_HOURS:
            continue
        daily = e["event_ticker"].endswith("17")
        scored.append(((not daily, h), e, h))
    if not scored:
        return None, None
    _, e, h = min(scored, key=lambda s: s[0])
    return e, h


def pick(client, plan=PLAN) -> dict:
    picks, notes = [], []
    for series, n in plan:
        event, hours = _choose_event(client, series)
        if not event:
            notes.append(f"{series}: no event closing in {MIN_HOURS:.0f}–{MAX_HOURS:.0f}h "
                         f"(next daily opens 4pm ET)")
            continue
        cands = []
        for m in event.get("markets") or []:
            if m.get("strike_type") != "greater" or m.get("status") not in ("active", "open"):
                continue
            bid = dollars_to_cents(m.get("yes_bid_dollars"))
            ask = dollars_to_cents(m.get("yes_ask_dollars"))
            vol = float(m.get("volume_24h_fp") or 0)
            if not bid or not ask or ask >= 100 or ask - bid > MAX_SPREAD or vol <= 0:
                continue
            mid = (bid + ask) / 2
            if not PRICE_BAND[0] <= mid <= PRICE_BAND[1]:
                continue
            cands.append((abs(mid - 50), -vol, m, mid, vol))
        cands.sort(key=lambda c: c[:2])
        chosen = []
        for _, _, m, mid, vol in cands:
            fv = fair_value.fair_value(m)
            if not fv or not fv.tradeable:
                continue
            chosen.append({"ticker": m["ticker"], "series": series, "mid": mid, "volume_24h": vol,
                           "fv": round(fv.cents, 1), "subtitle": m.get("yes_sub_title", ""),
                           "hours_to_close": round(hours, 1)})
            if len(chosen) == n:
                break
        if len(chosen) < n:
            notes.append(f"{series}: only {len(chosen)} of {n} strikes passed the filters")
        picks += chosen
    return {"picks": picks, "notes": notes, "ts": datetime.now(timezone.utc).timestamp()}


def main():
    res = pick(build_client(CONFIG))
    for p in res["picks"]:
        print(f"{p['ticker']:<34} mid {p['mid']:>5.1f}¢  model {p['fv']:>5.1f}¢  "
              f"vol24h {p['volume_24h']:>8,.0f}  closes in {p['hours_to_close']}h  {p['subtitle']}")
    for n in res["notes"]:
        print("note:", n)


if __name__ == "__main__":
    main()
