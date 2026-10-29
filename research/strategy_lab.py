"""
Strategy lab — RESEARCH ONLY. Nothing here is imported by the bot; it does
not change how the dashboard or main.py trade.

Replays a month of real, settled Kalshi BTC + ETH daily (5pm ET) markets
minute by minute and runs several strategy variants on identical data, so
they can be compared fairly against the current bot. Uses the bot's real
fair_value model, strategy.plan_quotes and RiskManager; a variant only
changes parameters and which strikes are watched / how quotes are leaned.

    python research/strategy_lab.py fetch --days 30     # download + cache data (slow, once)
    python research/strategy_lab.py run                 # replay all variants on the cache
    python research/strategy_lab.py holds               # live takes: hold to settlement vs exit after N min

Fill model (same as backtest.py): takes fill at the minute's touch; resting
quotes fill from real trades that went through our price, or traded at our
price when our quote had improved the touch, during the minute AFTER the
snapshot the quote was priced from. Positions settle at the real result.
Fees are fees.py's (makers pay 0 on these series). See research/README.md.
"""
import argparse
import gzip
import json
import logging
import math
import statistics
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from config import CONFIG, RiskConfig  # noqa: E402
from fair_value import CRYPTO_MIN_HOURS_TO_TRADE, FairValue, _crypto_prob  # noqa: E402
from fees import order_fee_cents, per_contract_fee_cents  # noqa: E402
from kalshi_client import build_client, dollars_to_cents  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from strategy import plan_quotes  # noqa: E402

logging.disable(logging.CRITICAL)

CACHE = Path(__file__).parent / "cache"
PRODUCTS = {"KXBTCD": "BTC-USD", "KXETHD": "ETH-USD"}
FIT_EVERY_MIN = 15
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
CB = requests.Session()


def ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def ticker_close(ev):
    """Approximate close (UTC) from an event ticker like KXBTCD-26OCT1917."""
    code = ev.split("-")[1]
    return datetime(2000 + int(code[:2]), MONTHS[code[2:5]], int(code[5:7]), 21,
                    tzinfo=timezone.utc).timestamp()


# ---- data download -----------------------------------------------------------

def settled_daily_events(client, series, days, skip_days=0):
    cutoff, out, cursor = time.time() - (days + skip_days) * 86400, [], None
    newest = time.time() - skip_days * 86400
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = client._request("GET", "/events", params=params, authenticated=False)
        evs = r.get("events", [])
        out += [e["event_ticker"] for e in evs if e["event_ticker"].endswith(("17", "17B"))]
        cursor = r.get("cursor")
        if not cursor or not evs or (out and ticker_close(out[-1]) < cutoff):
            break
    # Some days have two events for the same 5pm close: on 2026-10-28,
    # KXBTCD-26OCT2817 opened two days ahead (41 strikes) and the daily
    # KXBTCD-26OCT2817B (81 strikes) is the one the bot traded. Keep the B.
    by_day = {}
    for e in sorted(set(out)):
        by_day[e.rstrip("B")] = e
    return sorted(e for e in by_day.values() if cutoff <= ticker_close(e) < newest)


def coinbase(product, gran, start, end):
    out, t = {}, start
    while t < end:
        stop = min(end, t + 300 * gran)
        for attempt in range(4):
            r = CB.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                       params={"granularity": gran,
                               "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                               "end": datetime.fromtimestamp(stop, timezone.utc).isoformat()}, timeout=20)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            break
        for c in r.json():
            out[int(c[0])] = float(c[4])
        t = stop
        time.sleep(0.15)
    return out


def minute_books(client, series, ticker, start, n):
    end = start + n * 60
    candles = []
    for s in range(start, end, 4800 * 60):
        r = client._request("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                            params={"start_ts": s, "end_ts": min(end, s + 4800 * 60), "period_interval": 1},
                            authenticated=False)
        candles += r.get("candlesticks", [])
    raw = {}
    for c in candles:
        b = dollars_to_cents(c.get("yes_bid", {}).get("close_dollars"))
        a = dollars_to_cents(c.get("yes_ask", {}).get("close_dollars"))
        raw[c["end_period_ts"] - 60] = (b or None, a if a and a < 100 else None)
    out, last = [], (None, None)
    for i in range(n):
        last = raw.get(start + i * 60, last)
        out.append(list(last))
    return out


def fetch_trades(client, ticker):
    rows, cursor = [], None
    while True:
        params = {"ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = client._request("GET", "/markets/trades", params=params, authenticated=False)
        for t in r.get("trades", []):
            rows.append([ts(t["created_time"]), t["taker_side"] == "yes",
                         round(float(t["yes_price_dollars"]) * 100), float(t["count_fp"])])
        cursor = r.get("cursor")
        if not cursor:
            return sorted(rows)


def fetch_event(client, series, ev):
    path = CACHE / f"{ev}.json.gz"
    if path.exists():
        return "cached"
    ms = client._request("GET", "/markets", params={"event_ticker": ev, "limit": 300},
                         authenticated=False)["markets"]
    ladder = [m for m in ms if m.get("strike_type") == "greater" and m.get("floor_strike")]
    if not ladder:
        return "no ladder"
    open_t, close_t = ts(ladder[0]["open_time"]), ts(ladder[0]["close_time"])
    open_t -= open_t % 60
    n = (close_t - open_t) // 60
    raw = coinbase(PRODUCTS[series], 60, open_t - 120, close_t + 60)
    if not raw:
        return "no spot data"
    spots, last = [], raw.get(open_t) or raw[min(raw)]
    for i in range(n):
        last = raw.get(open_t + i * 60, last)
        spots.append(last)
    near = [m for m in ladder if abs(m["floor_strike"] / spots[0] - 1) < 0.06]
    books = {m["ticker"]: minute_books(client, series, m["ticker"], open_t, n) for m in near}
    # Trades only for strikes that were ever priced 10..90 — the only ones a
    # strategy here would ever quote.
    active = [t for t, bk in books.items()
              if any(b and a and 10 <= (a + b) / 2 <= 90 for b, a in bk)]
    trades = {t: fetch_trades(client, t) for t in active}
    keep = ("ticker", "strike_type", "floor_strike", "cap_strike", "result")
    data = {"series": series, "event": ev, "open": open_t, "close": close_t,
            "markets": [{k: m.get(k) for k in keep} for m in near],
            "spots": spots, "books": books, "trades": trades}
    CACHE.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(data, f)
    return f"{len(near)} strikes, {len(active)} with trades, {sum(len(v) for v in trades.values())} trades"


def cmd_fetch(days, skip_days=0):
    client = build_client(CONFIG)
    jobs = [(s, ev) for s in PRODUCTS for ev in settled_daily_events(client, s, days, skip_days)]
    print(f"{len(jobs)} settled daily events in the last {days} days")
    for i, (s, ev) in enumerate(jobs, 1):
        t0 = time.time()
        try:
            msg = fetch_event(client, s, ev)
        except Exception as e:      # keep going; a missing day just drops out
            msg = f"FAILED: {e}"
        print(f"[{i}/{len(jobs)}] {ev}: {msg} ({time.time() - t0:.0f}s)", flush=True)
    first = min(ticker_close(ev) for _, ev in jobs) - 2 * 86400
    for s, product in PRODUCTS.items():
        hours = coinbase(product, 3600, int(first - 9 * 86400), int(time.time()))
        (CACHE / f"hours_{product}.json").write_text(json.dumps(hours))
    print("done")


# ---- shared precompute (identical for every variant) -------------------------

def sigma_at(hour_keys, hour_closes, t):
    hrs = [h for h in hour_keys if h < t][-169:]
    closes = [hour_closes[h] for h in hrs]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if len(rets) < 24:
        return None
    return max(statistics.pstdev(rets[-24:]), statistics.pstdev(rets))


def prepare(ev):
    """Per-minute model inputs shared by all variants: spot, total sd for the
    model (realized vol × ladder vol-fit multiplier), whether the fit is valid."""
    hour_closes = {int(k): v for k, v in
                   json.loads((CACHE / f"hours_{PRODUCTS[ev['series']]}.json").read_text()).items()}
    keys = sorted(hour_closes)
    n = len(ev["spots"])
    sd, fit_ok = [0.0] * n, [False] * n
    sigma, mult, n_fit = None, 1.0, 0
    near = ev["markets"]
    for i in range(n):
        minute = ev["open"] + i * 60
        spot = ev["spots"][i]
        if sigma is None or minute % 3600 == 0:
            sigma = sigma_at(keys, hour_closes, minute) or sigma
        hours_left = (ev["close"] - minute) / 3600
        base = sigma * math.sqrt(max(hours_left, 1 / 60))
        if i % FIT_EVERY_MIN == 0:
            pts = []
            for m in near:
                b, a = ev["books"][m["ticker"]][i]
                if b and a and a - b <= 4 and 10 <= (a + b) / 2 <= 90:
                    pts.append((m, (a + b) / 2))
            if len(pts) >= 3:
                _, mult = min((sum((_crypto_prob(m, spot, base * k) * 100 - mid) ** 2 for m, mid in pts), k)
                              for k in [x / 20 for x in range(6, 61)])
            n_fit = len(pts)
        sd[i], fit_ok[i] = base * mult, n_fit >= 3
    ev["sd"], ev["fit_ok"] = sd, fit_ok
    ev["mkt"] = {m["ticker"]: m for m in near}
    ev["by_min"] = {}
    for t, rows in ev["trades"].items():
        d = {}
        for when, yes, px, cnt in rows:
            i = (when - ev["open"]) // 60
            if 0 <= i < n:
                d.setdefault(i, []).append((yes, px, cnt))
        ev["by_min"][t] = d
    return ev


def load_days():
    days = {}
    for p in sorted(CACHE.glob("*.json.gz")):
        with gzip.open(p, "rt") as f:
            ev = json.load(f)
        days.setdefault(ev["close"], []).append(prepare(ev))
    return [days[k] for k in sorted(days)]


# ---- variants ----------------------------------------------------------------

@dataclass
class Variant:
    name: str
    cfg: RiskConfig
    strikes: dict                  # series -> how many strikes to watch
    roll_min: int | None = None    # re-center the watched strikes every N minutes (None = fixed at open)
    min_hours: float = 2.0         # stop model trading this close to settlement
    hedge_cents: float = 0.0       # max lean from portfolio delta (0 = off)
    hedge_ref_usd: float = 15.0    # $ per 1% move at which the full lean applies
    flatten_hours: float = 0.0     # cross the spread to close every position this long before settlement
    lean_takes: bool = True        # False = the delta lean moves resting quotes only, never triggers takes
    strict_fills: bool = False     # maker fills must survive the next reprice (see run_day)
    spot_delay_min: int = 0        # model sees the coin price this many minutes late (speed test)
    exit_after_min: int | None = None   # close each take this many minutes later at the touch (None = hold)
    exit_renew: bool = True        # ...unless the model would take that side again now (restarts its clock)


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


EXIT_MINUTES = (5, 15, 60, 120)


def take_exits(fills):
    """Each take on its own, in cents after fees: held to settlement
    ("settle"), and closed at the touch N minutes later for each N in
    EXIT_MINUTES (taker fee both ways). Also the mid N minutes later before
    fees ("mid N"), the markout the paper report shows. A take with no touch
    left before the close settles."""
    out = []
    for i, ev, t, action, price, size, kind in fills:
        if kind != "take":
            continue
        sgn = 1 if action == "buy" else -1
        fee = order_fee_cents(price, size, maker=False)
        held = sgn * ((100 if ev["mkt"][t]["result"] == "yes" else 0) - price) * size - fee
        row = {"size": size, "settle": held}
        later = ev["books"][t][i:]
        for h in EXIT_MINUTES:
            px = next((b if sgn > 0 else a for b, a in later[h:] if (b if sgn > 0 else a) is not None), None)
            row[h] = held if px is None else sgn * (px - price) * size - fee - order_fee_cents(px, size, maker=False)
            b, a = later[h] if h < len(later) else (None, None)
            row[f"mid {h}"] = sgn * ((a + b) / 2 - price) * size if b is not None and a is not None else None
        out.append(row)
    return out


def run_day(v: Variant, evs):
    risk = RiskManager(v.cfg)
    resting, fills = {}, []
    watched = {}
    unwinds, renewed = [], 0       # [due minute, ticker, action that closes it, contracts] per take
    cap = v.cfg.max_take_book_delta_usd
    n = min(len(e["spots"]) for e in evs)
    peak_exposure, fill_hours, model_hours = 0.0, set(), set()
    for i in range(n):
        marks = {}
        for ev in evs:
            s, spot, sd = ev["series"], ev["spots"][max(0, i - v.spot_delay_min)], ev["sd"][i]
            hours_left = (ev["close"] - ev["open"]) / 3600 - i / 60
            tradeable = hours_left >= v.min_hours and ev["fit_ok"][i]
            if tradeable:
                model_hours.add(i // 60)
            # choose strikes: nearest the money (≈50¢), among those that traded
            if s not in watched or (v.roll_min and i % v.roll_min == 0):
                cands = [ev["mkt"][t] for t in ev["trades"]]
                cands.sort(key=lambda m: abs(m["floor_strike"] - spot))
                watched[s] = {m["ticker"] for m in cands[:v.strikes.get(s, 0)]}
            managed = watched[s] | {t for t in risk.positions
                                    if t in ev["mkt"] and risk.net(t) != 0}
            if not managed:
                continue

            probs = {t: _crypto_prob(ev["mkt"][t], spot, sd) for t in managed}
            lean = {}
            if v.hedge_cents or cap is not None:
                delta = {t: _crypto_prob(ev["mkt"][t], spot * 1.005, sd) - _crypto_prob(ev["mkt"][t], spot * 0.995, sd)
                         for t in managed}
            if v.hedge_cents:
                port = sum(risk.net(t) * delta[t] for t in managed)        # $ per 1% move
                dmax = max(delta.values()) or 1
                for t in managed:
                    lean[t] = -v.hedge_cents * _clip(port / v.hedge_ref_usd) * delta[t] / dmax

            # sorted: a set's order changes with each process's hash seed, and
            # the order decides which takes the cap and exposure limits let through
            for t in sorted(managed):
                bid, ask = ev["books"][t][i]
                if bid is not None and ask is not None:
                    marks[t] = (bid + ask) / 2
                net = risk.net(t)
                if v.flatten_hours and hours_left <= v.flatten_hours:
                    resting.pop(t, None)
                    if net > 0 and bid:
                        risk.record_fill(t, "yes", "sell", net, bid, maker=False)
                        fills.append((i, ev, t, "sell", bid, net, "flatten"))
                    elif net < 0 and ask:
                        risk.record_fill(t, "yes", "buy", -net, ask, maker=False)
                        fills.append((i, ev, t, "buy", ask, -net, "flatten"))
                    continue
                # exit_after_min: what's left of each take is closed N minutes
                # later at the touch, paying the taker fee. If the model would
                # take that side again right now, unwinding would only pay the
                # round trip to re-enter, so the take's clock restarts instead.
                for u in [u for u in unwinds if u[1] == t and u[0] <= i]:
                    _, _, action, size = u
                    size = min(size, -net if action == "buy" else net)
                    px = ask if action == "buy" else bid
                    if size <= 0:                # resting quotes already worked it down
                        unwinds.remove(u)
                        continue
                    if px is None:               # no touch this minute: try the next
                        continue
                    f = probs[t] * 100 + (lean.get(t, 0) if v.lean_takes else 0)
                    again = bid if action == "buy" else ask      # price the original take would get now
                    if v.exit_renew and tradeable and again is not None and \
                            (again - f if action == "buy" else f - again) \
                            - per_contract_fee_cents(again, maker=False) >= v.cfg.take_edge_cents:
                        u[0] = i + v.exit_after_min
                        renewed += 1
                        continue
                    risk.record_fill(t, "yes", action, size, px, maker=False)
                    fills.append((i, ev, t, action, px, size, "unwind"))
                    unwinds.remove(u)
                    net = risk.net(t)
                fv =FairValue(min(99, max(1, probs[t] * 100 + lean.get(t, 0))), "crypto", "", tradeable,
                              hours_left=hours_left)
                sides, _ = risk.allowed_sides(t)
                if t not in watched[s]:          # rolled off: only work the position down
                    net = risk.net(t)
                    sides = sides & ({"sell"} if net > 0 else {"buy"})
                book = {"yes_bid": bid, "yes_ask": ask}
                plan = plan_quotes(book, risk.net(t), v.cfg, sides, fv, anchor=probs[t] * 100)
                if lean and not v.lean_takes:
                    # takes judged on the raw model; the lean only moves resting quotes
                    raw = FairValue(min(99, max(1, probs[t] * 100)), "crypto", "", tradeable, hours_left=hours_left)
                    plan.takes = plan_quotes(book, risk.net(t), v.cfg, sides, raw).takes
                if cap is not None and plan.takes:
                    # MarketMaker._cap_take_delta: no take past cap $ per 1% of
                    # the book's coin exposure; takes that reduce it always go
                    port, kept = sum(risk.net(x) * delta[x] for x in managed), []
                    for action, price, size in plan.takes:
                        step = delta[t] if action == "buy" else -delta[t]
                        while size > 0 and abs(port + step * size) > max(cap, abs(port)):
                            size -= 1
                        if size > 0:
                            kept.append((action, price, size))
                            port += step * size
                    plan.takes = kept
                for action, price, size in plan.takes:
                    risk.record_fill(t, "yes", action, size, price, maker=False)
                    fills.append((i, ev, t, action, price, size, "take"))
                    fill_hours.add(i // 60)
                    if v.exit_after_min:
                        unwinds.append([i + v.exit_after_min, t, "sell" if action == "buy" else "buy", size])
                r = resting.setdefault(t, {})
                prev = dict(r)
                for action in ("buy", "sell"):
                    target = getattr(plan, action)
                    if target is None:
                        r.pop(action, None)
                    else:
                        touch = bid if action == "buy" else ask
                        improved = touch is None or (target[0] > touch if action == "buy" else target[0] < touch)
                        r[action] = (target[0], target[1], improved)
                # Maker fills: trades during minute i hit the quotes resting
                # since the end of minute i-1 (prev). Books and spot are
                # end-of-minute snapshots, so the quote just repriced (r) was
                # priced from data AFTER those trades. Until 2026-10-22 this
                # lab filled against it, peeking up to a minute ahead — that
                # was most of the maker profit it reported.
                # strict: the trade must also reach the repriced quote, i.e.
                # assume the live bot requotes before a taker reacts to a coin
                # move (optimistic about speed, but no peeking at fill prices).
                for yes, px, cnt in ev["by_min"].get(t, {}).get(i, ()):
                    action = "sell" if yes else "buy"
                    q = prev.get(action)
                    if not q:
                        continue
                    qpx, qsize, improved = q
                    through = px > qpx if action == "sell" else px < qpx
                    if v.strict_fills:
                        nq = r.get(action)
                        through = through and nq is not None and (px > nq[0] if action == "sell" else px < nq[0])
                        improved = False
                    if through or (px == qpx and improved):
                        fill = max(1, int(min(cnt, qsize)))
                        risk.record_fill(t, "yes", action, fill, qpx, maker=True)
                        fills.append((i, ev, t, action, qpx, fill, "maker"))
                        fill_hours.add(i // 60)
                        if qsize - fill > 0:
                            prev[action] = (qpx, qsize - fill, improved)
                        else:
                            prev.pop(action)
        risk.update_marks(marks)
        peak_exposure = max(peak_exposure, risk.total_exposure_usd())

    by_series, settle_pnl = {}, 0.0
    for ev in evs:
        for t, pos in risk.positions.items():
            if t not in ev["mkt"]:
                continue
            if pos.net_contracts:
                pos.settle(100 if ev["mkt"][t]["result"] == "yes" else 0, ev["mkt"][t]["result"])
            pnl = (pos.realized_pnl_cents - pos.total_fees_cents) / 100
            by_series[ev["series"]] = by_series.get(ev["series"], 0.0) + pnl
    markouts = []
    for i, ev, t, action, price, size, kind in fills:
        j = i + 60
        if j < len(ev["books"][t]):
            b, a = ev["books"][t][j]
            if b is not None and a is not None:
                mid = (a + b) / 2
                markouts.append((kind, size * ((mid - price) if action == "buy" else (price - mid))))
    return {"pnl": sum(by_series.values()), "by_series": by_series, "halted": risk.halted,
            "takes": sum(1 for f in fills if f[6] == "take"),
            "unwinds": sum(1 for f in fills if f[6] == "unwind"), "renewed": renewed,
            "take_exits": take_exits(fills),
            "makers": sum(1 for f in fills if f[6] == "maker"),
            "contracts": sum(f[5] for f in fills),
            "fees": sum(p.total_fees_cents for p in risk.positions.values()) / 100,
            "fill_hours": len(fill_hours), "model_hours": len(model_hours),
            "peak_exposure": peak_exposure, "markouts": markouts,
            "fill_times": [(ev["open"] + i * 60, kind) for i, ev, _t, _a, _p, _s, kind in fills],
            "btc_move": next((abs(e["spots"][-1] / e["spots"][0] - 1) * 100 for e in evs
                              if e["series"] == "KXBTCD"), 0.0),
            "date": datetime.fromtimestamp(evs[0]["close"], timezone.utc).strftime("%m-%d")}


# Current bot = autopick's 4 BTC + 2 ETH strikes nearest 50¢, chosen once,
# with today's config.py defaults.
# (Pinned: config.py now defaults to the proposed strategy's sizes.)
BASE = RiskConfig(max_position_per_market=25, max_total_exposure_usd=200.0, max_daily_loss_usd=25.0,
                  max_single_market_loss_usd=15.0, quote_size=5, take_size=5, delta_lean_cents=0.0,
                  min_edge_cents=2.0,   # pinned: config.py's default is now 6¢
                  min_reduce_edge_cents=None, max_take_book_delta_usd=None,   # added 2026-10-22
                  max_take_hours=None)                                         # added 2026-10-29
HIGH = replace(BASE, max_position_per_market=75, max_total_exposure_usd=900.0,
               max_daily_loss_usd=150.0, max_single_market_loss_usd=60.0,
               quote_size=15, take_size=15)

VARIANTS = [
    Variant("A current", BASE, {"KXBTCD": 4, "KXETHD": 2}),
    Variant("B current x3 size", HIGH, {"KXBTCD": 4, "KXETHD": 2}),
    Variant("C rolling strikes", BASE, {"KXBTCD": 4, "KXETHD": 2}, roll_min=15),
    Variant("D rolling + size", HIGH, {"KXBTCD": 6, "KXETHD": 3}, roll_min=15),
    Variant("E D + aggressive edges",
            replace(HIGH, take_edge_cents=2.5, min_edge_cents=1.0, inventory_skew_cents=5.0),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15),
    Variant("F E + delta lean",
            replace(HIGH, take_edge_cents=2.5, min_edge_cents=1.0, inventory_skew_cents=5.0),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15, hedge_cents=4.0),
    Variant("G F + trade to 1h",
            replace(HIGH, take_edge_cents=2.5, min_edge_cents=1.0, inventory_skew_cents=5.0),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15, hedge_cents=4.0, min_hours=1.0),
    Variant("H F + flatten at 2h",
            replace(HIGH, take_edge_cents=2.5, min_edge_cents=1.0, inventory_skew_cents=5.0),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15, hedge_cents=4.0, flatten_hours=2.0),
    Variant("I D + flatten at 2h", HIGH, {"KXBTCD": 6, "KXETHD": 3}, roll_min=15, flatten_hours=2.0),
    Variant("J D + lean quotes only", HIGH, {"KXBTCD": 6, "KXETHD": 3}, roll_min=15,
            hedge_cents=4.0, lean_takes=False),
    Variant("K D + bigger takes",
            replace(HIGH, take_size=30, max_position_per_market=100),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15),
    Variant("L D + picky takes x30",
            replace(HIGH, take_edge_cents=6.0, take_size=30, max_position_per_market=100),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15),
    Variant("M D + wider quotes",
            replace(HIGH, min_edge_cents=3.0),
            {"KXBTCD": 6, "KXETHD": 3}, roll_min=15),
    Variant("N D + 8 BTC strikes", HIGH, {"KXBTCD": 8, "KXETHD": 3}, roll_min=15),
    Variant("O D + roll 5min", HIGH, {"KXBTCD": 6, "KXETHD": 3}, roll_min=5),
    # The proposal: middle of the plateau the lean-strength grid found
    # (2–6¢ lean at a $15–30/1% reference all did well; stronger leans bled).
    Variant("P PROPOSED rolling ladder + delta lean", HIGH, {"KXBTCD": 6, "KXETHD": 3}, roll_min=15,
            hedge_cents=4.0, hedge_ref_usd=30.0, lean_takes=False),
    # Live settings 2026-10-22..28: P's ladder and lean, BTC only, quotes
    # 6¢ from fair value, config.py's limits. (Pinned: `holds` and the
    # README's 10-28 numbers use it.)
    Variant("Q LIVE BTC only, quotes 6c", replace(RiskConfig(), min_edge_cents=6.0, max_take_hours=None),
            {"KXBTCD": 6}, roll_min=15, hedge_cents=4.0, hedge_ref_usd=30.0, lean_takes=False),
    # Live since 2026-10-29: Q with quotes 8¢ out, no takes more than 14h
    # before close, and the model stopped 3h before close (config.py).
    Variant("R LIVE since 10-29", RiskConfig(), {"KXBTCD": 6}, roll_min=15,
            hedge_cents=4.0, hedge_ref_usd=30.0, lean_takes=False, min_hours=CRYPTO_MIN_HOURS_TO_TRADE),
]


def summarize(v, rows):
    pnl = [r["pnl"] for r in rows]
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in pnl:
        cum += x
        peak, dd = max(peak, cum), max(dd, max(peak, cum) - cum)
    sd = statistics.pstdev(pnl) if len(pnl) > 1 else 0.0
    half = len(pnl) // 2
    take_mo = [m for r in rows for k, m in r["markouts"] if k == "take"]
    make_mo = [m for r in rows for k, m in r["markouts"] if k == "maker"]
    contracts_take = sum(r["contracts"] for r in rows) or 1
    return {
        "name": v.name, "days": len(pnl), "total": sum(pnl), "avg": statistics.mean(pnl), "sd": sd,
        "sharpe": statistics.mean(pnl) / sd * math.sqrt(365) if sd else 0.0,
        "win": sum(x > 0 for x in pnl), "lose": sum(x < 0 for x in pnl),
        "worst": min(pnl), "best": max(pnl), "maxdd": dd,
        "h1": sum(pnl[:half]), "h2": sum(pnl[half:]),
        "fills_day": statistics.mean(r["takes"] + r["makers"] for r in rows),
        "takes_day": statistics.mean(r["takes"] for r in rows),
        "contracts_day": statistics.mean(r["contracts"] for r in rows),
        "fill_hour_pct": sum(r["fill_hours"] for r in rows) / max(1, sum(r["model_hours"] for r in rows)),
        "fees": sum(r["fees"] for r in rows),
        "peak_exp": max(r["peak_exposure"] for r in rows),
        "halts": sum(r["halted"] for r in rows),
        "btc": sum(r["by_series"].get("KXBTCD", 0) for r in rows),
        "eth": sum(r["by_series"].get("KXETHD", 0) for r in rows),
        "take_mo": sum(take_mo) / max(1, len(take_mo)), "maker_mo": sum(make_mo) / max(1, len(make_mo)),
        "daily": [(r["date"], round(r["pnl"], 2), round(r["btc_move"], 2)) for r in rows],
        "per_contract": sum(pnl) / contracts_take * 100,
    }


def cmd_run(only):
    t0 = time.time()
    days = load_days()
    print(f"Loaded {len(days)} days ({days[0][0]['event']} .. {days[-1][0]['event']}) in {time.time() - t0:.0f}s\n")
    results = []
    for v in VARIANTS:
        if only and not any(v.name.startswith(o) for o in only):
            continue
        rows = [run_day(v, d) for d in days]
        s = summarize(v, rows)
        results.append(s)
        print(f"{s['name']:<24} total ${s['total']:>+8.2f}  avg/day ${s['avg']:>+6.2f} ±{s['sd']:5.2f}  "
              f"win {s['win']:>2}/{s['days']}  worst ${s['worst']:>+7.2f}  maxDD ${s['maxdd']:>6.2f}  "
              f"fills/day {s['fills_day']:>5.1f}  hrs w/ fills {s['fill_hour_pct']:4.0%}  "
              f"peak exp ${s['peak_exp']:>6.0f}  halves {s['h1']:+.0f}/{s['h2']:+.0f}", flush=True)
    out = Path(__file__).parent / ("results.json" if CACHE.name == "cache" else f"results_{CACHE.name}.json")
    merged = {s["name"]: s for s in (json.loads(out.read_text()) if out.exists() else [])}
    merged.update({s["name"]: s for s in results})
    out.write_text(json.dumps(sorted(merged.values(), key=lambda s: s["name"]), indent=1))
    print(f"\nsaved {out}")


def cmd_holds():
    """Live settings (Q): takes held to settlement vs each take closed at the
    touch after N minutes, under both 1-minute fill models. Prints only."""
    days = load_days()
    live = next(v for v in VARIANTS if v.name.startswith("Q"))
    print(f"{len(days)} days ({days[0][0]['event']} .. {days[-1][0]['event']}), {live.name}\n")
    for strict in (False, True):
        print("strict fills" if strict else "normal fills")
        hold = None
        for n in (None,) + EXIT_MINUTES:
            v = replace(live, strict_fills=strict, exit_after_min=n)
            rows = [run_day(v, d) for d in days]
            pnl = [r["pnl"] for r in rows]
            hold = hold or rows
            print(f"  {'hold to settlement' if n is None else f'exit after {n:>3}m':<19} total ${sum(pnl):>+8.2f}  "
                  f"sd/day ${statistics.pstdev(pnl):5.2f}  worst ${min(pnl):>+7.2f}  "
                  f"takes/day {statistics.mean(r['takes'] for r in rows):4.1f}  "
                  f"unwinds/day {statistics.mean(r['unwinds'] for r in rows):4.1f}  "
                  f"renewed/day {statistics.mean(r['renewed'] for r in rows):4.1f}  "
                  f"fees ${sum(r['fees'] for r in rows):6.2f}", flush=True)
        # the same takes scored one by one (positions not netted, no
        # interaction with resting quotes): what each exit rule alone is worth
        c = sum(x["size"] for r in hold for x in r["take_exits"]) or 1
        print(f"  each take on its own ({c} contracts), after fees:")
        for key in ("settle",) + EXIT_MINUTES:
            day = [sum(x[key] for x in r["take_exits"]) / 100 for r in hold]
            mids = [(x[f"mid {key}"], x["size"]) for r in hold for x in r["take_exits"]
                    if key != "settle" and x[f"mid {key}"] is not None]
            mid = (f"  (mid markout {sum(m for m, _ in mids) / sum(k for _, k in mids):+5.2f}¢ before fees)"
                   if mids else "")
            print(f"    {'settle' if key == 'settle' else f'{key:>3}m at touch':<13} {sum(day) * 100 / c:+6.2f}¢/contract  "
                  f"total ${sum(day):>+8.2f}  sd/day ${statistics.pstdev(day):5.2f}  worst ${min(day):>+7.2f}{mid}")
        print()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--days", type=int, default=30)
    f.add_argument("--skip-days", type=int, default=0, help="end the window this many days ago")
    r = sub.add_parser("run")
    r.add_argument("--only", nargs="*")
    sub.add_parser("holds", help="hold takes to settlement vs close them after N minutes")
    ap.add_argument("--cache", help="cache folder name under research/ (default: cache)")
    args = ap.parse_args()
    if args.cache:
        global CACHE
        CACHE = Path(__file__).parent / args.cache
    if args.cmd == "fetch":
        cmd_fetch(args.days, args.skip_days)
    elif args.cmd == "holds":
        cmd_holds()
    else:
        cmd_run(args.only)


if __name__ == "__main__":
    main()
