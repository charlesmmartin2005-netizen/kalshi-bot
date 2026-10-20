"""
Minute-by-minute backtest of the fair-value strategy on real, already
settled Kalshi Bitcoin daily markets (KXBTCD, 5pm ET close).

simulate.py can't test this strategy: its edge is reacting to BTC moves
faster than resting Kalshi quotes, and hourly resampling erases that. Here
every minute of each market's life is replayed from real data:

  - Kalshi 1-minute candles (best bid/ask) for every strike near the money
  - Kalshi's public trade tape (for maker fills)
  - Coinbase 1-minute BTC prices (the model's spot input) and hourly
    candles (its realized-vol input), exactly as fair_value.py uses them
  - each market's real settlement result

Each minute the bot's real code decides: fair_value's model (with the same
60-second ladder vol fit, done here every 15 minutes on the minute candles),
strategy.plan_quotes for quotes and takes, RiskManager for limits. Takes
fill at the touch; resting quotes fill from real trades they'd have been
first in line for. Positions settle at the real result.

Limits of a 1-minute replay — read results with these in mind:
  - Kalshi's candle close and Coinbase's minute close can be seconds apart,
    which can look like a "lag" that wasn't really there (or hide one that
    was). The settlement result is real, so systematic phantom edge would
    show up as losses, not gains.
  - Take sizes assume the touch had at least take_size contracts (BTC
    ladder books usually show thousands).
  - Kalshi settles on CF Benchmarks' BRTI, not Coinbase.

Usage:
    python backtest.py [--days 14] [--strikes 4]
"""
import argparse
import math
import statistics
import time
from datetime import datetime, timezone

import requests

import fair_value as fvmod
from config import CONFIG
from fair_value import FairValue, _crypto_prob, CRYPTO_MIN_HOURS_TO_TRADE
from kalshi_client import build_client, dollars_to_cents
from risk_manager import RiskManager
from strategy import plan_quotes

CB = requests.Session()
SERIES = "KXBTCD"
VOL_REFIT_MIN = 15


def ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# ---- data ------------------------------------------------------------------

def settled_daily_events(client, days):
    """Settled 5pm-ET daily events that closed within the last `days`."""
    cutoff, out, cursor = time.time() - days * 86400 - 3600, [], None
    while True:
        params = {"series_ticker": SERIES, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = client._request("GET", "/events", params=params, authenticated=False)
        for e in r.get("events", []):
            if e["event_ticker"].endswith("17"):
                out.append(e["event_ticker"])
        cursor = r.get("cursor")
        ms = client._request("GET", "/markets", params={"event_ticker": out[-1], "limit": 1},
                             authenticated=False)["markets"] if out else []
        oldest_close = ts(ms[0]["close_time"]) if ms else time.time()
        if not cursor or oldest_close < cutoff:
            break
    events = []
    for ev in out:
        ms = client._request("GET", "/markets", params={"event_ticker": ev, "limit": 200},
                             authenticated=False)["markets"]
        if ms and ts(ms[0]["close_time"]) >= cutoff:
            events.append((ev, ms))
    return sorted(events, key=lambda e: e[1][0]["close_time"])


def coinbase_minutes(start, end):
    """{minute_ts: close} for BTC-USD."""
    out, t = {}, start
    while t < end:
        stop = min(end, t + 300 * 60)
        r = CB.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                   params={"granularity": 60, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                           "end": datetime.fromtimestamp(stop, timezone.utc).isoformat()}, timeout=15)
        r.raise_for_status()
        for c in r.json():
            out[int(c[0])] = float(c[4])
        t = stop
        time.sleep(0.15)
    return out


def coinbase_hours(start, end):
    out, t = {}, start
    while t < end:
        stop = min(end, t + 300 * 3600)
        r = CB.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                   params={"granularity": 3600, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                           "end": datetime.fromtimestamp(stop, timezone.utc).isoformat()}, timeout=15)
        r.raise_for_status()
        for c in r.json():
            out[int(c[0])] = float(c[4])
        t = stop
        time.sleep(0.15)
    return out


def sigma_at(hour_closes, t):
    """Realized vol per sqrt(hour) as fair_value computes it, using only
    hours before t (max of last-24h and last-7d)."""
    hrs = sorted(h for h in hour_closes if h < t)[-169:]
    closes = [hour_closes[h] for h in hrs]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 24:
        return None
    return max(statistics.pstdev(rets[-24:]), statistics.pstdev(rets))


def kalshi_minute_books(client, ticker, start, end):
    """{minute_ts: (bid, ask)} forward-filled across [start, end)."""
    candles = []
    for s in range(start, end, 4800 * 60):      # Kalshi caps a request at 5000 candles
        r = client._request("GET", f"/series/{SERIES}/markets/{ticker}/candlesticks",
                            params={"start_ts": s, "end_ts": min(end, s + 4800 * 60), "period_interval": 1},
                            authenticated=False)
        candles += r.get("candlesticks", [])
    raw = {}
    for c in candles:
        b = dollars_to_cents(c.get("yes_bid", {}).get("close_dollars"))
        a = dollars_to_cents(c.get("yes_ask", {}).get("close_dollars"))
        raw[c["end_period_ts"] - 60] = (b or None, a if a and a < 100 else None)
    out, last = {}, (None, None)
    for m in range(start - start % 60, end, 60):
        last = raw.get(m, last)
        out[m] = last
    return out


def kalshi_trades(client, ticker):
    trades, cursor = [], None
    while True:
        params = {"ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = client._request("GET", "/markets/trades", params=params, authenticated=False)
        trades += r.get("trades", [])
        cursor = r.get("cursor")
        if not cursor:
            return trades


# ---- replay -------------------------------------------------------------------

def replay_event(client, ev, markets, n_strikes, hour_closes, log_lines):
    cfg = CONFIG.risk
    open_t, close_t = ts(markets[0]["open_time"]), ts(markets[0]["close_time"])
    open_t -= open_t % 60
    spots = coinbase_minutes(open_t - 60, close_t + 60)
    if not spots:
        return None
    spot0 = spots.get(open_t) or spots[min(spots)]

    ladder = [m for m in markets if m.get("strike_type") == "greater" and m.get("floor_strike")]
    # Strikes the vol fit can use: within ±6% of spot at open.
    near = [m for m in ladder if abs(m["floor_strike"] / spot0 - 1) < 0.06]
    books = {m["ticker"]: kalshi_minute_books(client, m["ticker"], open_t, close_t) for m in near}
    # Strikes the bot watches: the n nearest the money at open.
    watched = sorted(near, key=lambda m: abs(m["floor_strike"] - spot0))[:n_strikes]
    trades = {}
    for m in watched:
        rows = []
        for t in kalshi_trades(client, m["ticker"]):
            rows.append((ts(t["created_time"]), t["taker_side"] == "yes",
                         round(float(t["yes_price_dollars"]) * 100), float(t["count_fp"])))
        trades[m["ticker"]] = sorted(rows)

    risk = RiskManager(cfg)
    fills = []                                          # (minute, ticker, action, price, size, kind)
    resting = {m["ticker"]: {} for m in watched}       # ticker -> {action: (price, size, improved)}
    mult, n_fit, last_fit = 1.0, 0, -10 ** 9
    stats = {"takes": 0, "maker_fills": 0, "tradeable_min": 0}
    spot = spot0
    sigma = sigma_at(hour_closes, open_t)
    for minute in range(open_t, close_t, 60):
        spot = spots.get(minute, spot)
        hours_left = (close_t - minute) / 3600
        if minute % 3600 == 0:
            sigma = sigma_at(hour_closes, minute) or sigma
        base_sd = sigma * math.sqrt(max(hours_left, 1 / 60))

        if minute - last_fit >= VOL_REFIT_MIN * 60:      # ladder vol fit (live: every 60s)
            pts = []
            for m in near:
                b, a = books[m["ticker"]].get(minute, (None, None))
                if b and a and a - b <= 4 and 10 <= (a + b) / 2 <= 90:
                    pts.append((m, (a + b) / 2))
            if len(pts) >= 3:
                _, mult = min((sum((_crypto_prob(m, spot, base_sd * k) * 100 - mid) ** 2 for m, mid in pts), k)
                              for k in [x / 20 for x in range(6, 61)])
            n_fit, last_fit = len(pts), minute

        marks = {}
        for m in watched:
            t = m["ticker"]
            bid, ask = books[t].get(minute, (None, None))
            if bid is not None and ask is not None:
                marks[t] = (bid + ask) / 2
            p = _crypto_prob(m, spot, base_sd * mult)
            fv = FairValue(min(99, max(1, p * 100)), "crypto", "",
                           tradeable=hours_left >= CRYPTO_MIN_HOURS_TO_TRADE and n_fit >= 3)
            stats["tradeable_min"] += fv.tradeable
            sides, _ = risk.allowed_sides(t)
            plan = plan_quotes({"yes_bid": bid, "yes_ask": ask}, risk.net(t), cfg, sides, fv)

            for action, price, size in plan.takes:
                risk.record_fill(t, "yes", action, size, price, maker=False)
                stats["takes"] += 1
                fills.append((minute, t, action, price, size, "take"))
                log_lines.append(f"{datetime.fromtimestamp(minute, timezone.utc):%m-%d %H:%M} TAKE {action} "
                                 f"{size}@{price}¢ {t[-9:]} (model {fv.cents:.1f}, BTC {spot:,.0f})")
            for action in ("buy", "sell"):
                target = getattr(plan, action)
                if target is None:
                    resting[t].pop(action, None)
                else:
                    touch = bid if action == "buy" else ask
                    improved = touch is None or (target[0] > touch if action == "buy" else target[0] < touch)
                    resting[t][action] = (target[0], target[1], improved)

            # maker fills from real trades in the next minute
            for when, taker_buys, price, n in trades[t]:
                if when < minute or when >= minute + 60:
                    continue
                action = "sell" if taker_buys else "buy"
                q = resting[t].get(action)
                if not q:
                    continue
                qpx, qsize, improved = q
                through = price > qpx if action == "sell" else price < qpx
                if through or (price == qpx and improved):
                    fill = max(1, int(min(n, qsize)))
                    risk.record_fill(t, "yes", action, fill, qpx, maker=True)
                    stats["maker_fills"] += 1
                    fills.append((minute, t, action, qpx, fill, "maker"))
                    left = qsize - fill
                    resting[t][action] = (qpx, left, improved) if left > 0 else None
                    if left <= 0:
                        resting[t].pop(action)
        risk.update_marks(marks)

    result = {}
    for m in watched:
        t = m["ticker"]
        pos = risk.positions.get(t)
        if pos and pos.net_contracts:
            pos.settle(100 if m.get("result") == "yes" else 0, m.get("result", "?"))
        result[t] = {"pnl": (pos.realized_pnl_cents - pos.total_fees_cents) / 100 if pos else 0.0,
                     "fills": pos.fill_count if pos else 0, "result": m.get("result")}
    total = sum(r["pnl"] for r in result.values())

    # Markouts: how the Kalshi mid moved after each fill, in the fill's
    # favor (cents per contract, before fees). Real speed edge shows up as
    # positive take markouts regardless of BTC's overall trend.
    markouts = []
    for minute, t, action, price, size, kind in fills:
        row = {"kind": kind}
        for h in (5, 30, 60):
            b, a = books[t].get(minute + h * 60, (None, None))
            if b is not None and a is not None and minute + h * 60 < close_t:
                mid = (b + a) / 2
                row[h] = (mid - price) if action == "buy" else (price - mid)
        markouts.append(row)
    return {"event": ev, "close": close_t, "spot_open": spot0, "spot_close": spot, "per": result,
            "total": total, "halted": risk.halted, "markouts": markouts, **stats}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--strikes", type=int, default=4)
    ap.add_argument("--show-takes", action="store_true")
    args = ap.parse_args()
    client = build_client(CONFIG)

    print(f"Finding settled {SERIES} 5pm markets from the last {args.days} days...")
    events = settled_daily_events(client, args.days)
    if not events:
        raise SystemExit("No settled events found.")
    first_open = ts(events[0][1][0]["open_time"])
    hour_closes = coinbase_hours(first_open - 8 * 86400, int(time.time()))
    print(f"Replaying {len(events)} events minute by minute ({args.strikes} strikes nearest the money each)...\n")

    print(f"{'EVENT':<20} {'BTC open→close':>22} {'takes':>6} {'maker':>6} {'P&L':>9}  per strike (result)")
    print("-" * 110)
    results, takes_log = [], []
    for ev, ms in events:
        r = replay_event(client, ev, ms, args.strikes, hour_closes, takes_log)
        if not r:
            print(f"{ev:<20} (no price data)")
            continue
        results.append(r)
        per = "  ".join(f"{t.split('-T')[-1][:6]}:{v['pnl']:+.2f}({v['result']})" for t, v in r["per"].items())
        print(f"{ev:<20} {r['spot_open']:>10,.0f}→{r['spot_close']:<10,.0f} {r['takes']:>6} {r['maker_fills']:>6} "
              f"{r['total']:>+9.2f}  {per}{'  HALTED' if r['halted'] else ''}")

    tot = [r["total"] for r in results]
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in tot:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    print("-" * 110)
    print(f"{len(results)} days · total P&L ${sum(tot):+.2f} · avg/day ${statistics.mean(tot):+.2f} · "
          f"winning days {sum(x > 0 for x in tot)} · losing days {sum(x < 0 for x in tot)} · "
          f"flat {sum(x == 0 for x in tot)} · max drawdown ${dd:.2f}")
    print(f"takes {sum(r['takes'] for r in results)} · maker fills {sum(r['maker_fills'] for r in results)}")

    print("\nMarkouts — Kalshi mid move in the fill's favor after N minutes (¢/contract, before fees):")
    all_mo = [m for r in results for m in r["markouts"]]
    for kind in ("take", "maker"):
        rows = [m for m in all_mo if m["kind"] == kind]
        parts = []
        for h in (5, 30, 60):
            xs = [m[h] for m in rows if h in m]
            if xs:
                se = statistics.pstdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0
                parts.append(f"+{h}m {statistics.mean(xs):+.2f} ±{se:.2f} (n={len(xs)}, {sum(x > 0 for x in xs) / len(xs):.0%} favorable)")
        print(f"  {kind:<6} " + " · ".join(parts))
    print("  (± = standard error; an edge is credible when the mean is > ~2× its ± and above the fee)")
    if args.show_takes:
        print("\nTakes:")
        print("\n".join(takes_log[-60:]))


if __name__ == "__main__":
    main()
