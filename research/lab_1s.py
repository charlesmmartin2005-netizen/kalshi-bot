"""
1-second fill models for the strategy lab — RESEARCH ONLY, not used by the bot.

The same replay as strategy_lab.run_day (the bot's own plan_quotes and
RiskManager, rolling ladder, delta lean on resting quotes, the take cap) with
four fill models for resting quotes, which bracket reality:

  honest   1-minute: trades in minute i fill the quotes resting since the
           end of minute i-1 (strategy_lab's "normal")
  between  1-minute: ...and only if they'd also reach the quote repriced at
           the end of minute i (strategy_lab's "strict"; README "mid")
  sec      1-second: each real trade meets the quote the bot would have had
           up at that second, priced from Binance's 1-second coin price
           (scaled to Coinbase) latency_s earlier. sec_clamp_book="prev"
           clamps to the last Kalshi book the bot knew (README "pess");
           "next" peeks at the end of the trade's minute ("opt").

Takes fill at the end-of-minute touch in every model. Needs research/cache*
from strategy_lab.py fetch, and for "sec" Binance's 1-second klines
(data.binance.vision, BTCUSDT-1s-YYYY-MM-DD.zip) in research/cache_binance/.

    python research/lab_1s.py grid --cache cache --model pess [--only NAME ...]
    python research/lab_1s.py report

Every fill is kept (kind, size, price, markouts, settlement), so P&L
decomposes exactly into fills: sum(signed size * (settle - price)) - fees.
"""
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))
import strategy_lab as L  # noqa: E402
import fees  # noqa: E402
from config import RiskConfig  # noqa: E402
from fair_value import CRYPTO_MIN_HOURS_TO_TRADE, FairValue, _crypto_prob  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from strategy import plan_quotes  # noqa: E402

ET = timezone(timedelta(hours=-4))   # EDT for Jul-Oct
# The grid's baseline: live settings 2026-10-22..28 (quotes 6¢, no take
# window, the model stopped 2h before close). "live since 10-29" below is
# today's config.py.
LIVE = replace(RiskConfig(), min_edge_cents=6.0, max_take_hours=None)


@dataclass
class V:
    name: str
    cfg: RiskConfig = field(default_factory=lambda: LIVE)
    strikes: dict = field(default_factory=lambda: {"KXBTCD": 6})
    roll_min: int = 15
    min_hours: float = 2.0
    hedge_cents: float = LIVE.delta_lean_cents
    hedge_ref_usd: float = LIVE.delta_lean_ref_usd
    strict_fills: bool = True
    maker_fee_rate: float = 0.0    # Kalshi charges makers nothing on KXBTCD (fees.py)
    fv_mode: str = "model"          # model | offset | shrink
    halflife_min: float = 30.0      # offset: EMA half-life of (model - mid)
    shrink: float = 1.0             # shrink: fv = mid + shrink * (model - mid)
    no_open_hours: frozenset = frozenset()   # ET hours: no position-increasing trades
    stop_adding_hours: float = 0.0  # hours_left below which only reducing sides (until min_hours)
    takes: bool = True
    max_take_hours: float = 99.0    # no takes with more than this many hours to close
    exit_after_min: int | None = None   # close each take this many minutes later at the touch
    exit_renew: bool = True         # ...unless the model would take that side again then
    take_sizes: tuple = ()          # ((gap, contracts), ...) high to low: take size by the model's gap after fee
    rest: str = "both"              # resting quotes: "both" sides, "reduce" (only the side that shrinks
                                    # the position), or "none" (takes only)
    quote_max_gap: float | None = None   # no resting quote on the side that bets with the model when
                                         # |model - mid| is wider than this (the market disagrees)
    # "lab": strategy_lab's matching (trades in minute i-1 vs quotes priced
    # from end-of-minute data, i.e. after those trades). "honest": trades in
    # minute i fill only the quotes resting since the end of minute i-1.
    fill_model: str = "between"
    at_price_fills: bool = True     # honest: also fill at our price if we improved the touch
    latency_s: int = 1              # sec: our quote reflects the coin price this many seconds before the trade
    sec_clamp_book: str = "prev"    # sec: Kalshi book for the touch clamps: "prev" (known) or "next" (end of
                                    # the trade's minute — look-ahead, a bound on how much book staleness matters)


def precompute(days):
    """Model probability per strike-minute, for every strike that traded, and
    trades by minute with their timestamps (for the 1-second fill model)."""
    for evs in days:
        for ev in evs:
            if "pm" in ev:
                continue
            n = len(ev["spots"])
            ev["pm"] = {t: [_crypto_prob(ev["mkt"][t], ev["spots"][i], ev["sd"][i]) * 100 for i in range(n)]
                        for t in ev["trades"]}
            ev["by_min_ts"] = {}
            for t, rows in ev["trades"].items():
                d = {}
                for when, yes, px, cnt in rows:
                    i = (when - ev["open"]) // 60
                    if 0 <= i < n:
                        d.setdefault(i, []).append((when, yes, px, cnt))
                ev["by_min_ts"][t] = d
    return days


# ---- 1-second coin prices (Binance archive) ----------------------------------

BINANCE = REPO / "research" / "cache_binance"
_BN: dict = {}


def _bn_day(symbol, day):
    """86,400 one-second closes for a UTC day, forward-filled (None if missing)."""
    key = (symbol, day)
    if key not in _BN:
        import array
        import zipfile
        path = BINANCE / f"{symbol}-1s-{day}.zip"
        if not path.exists():
            _BN[key] = None
            return None
        out = array.array("d", [0.0]) * 86400
        start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
        with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as f:
            for line in f:
                parts = line.split(b",", 5)
                t = int(parts[0])
                t = t // 1_000_000 if t > 10**14 else t // 1000      # µs or ms
                k = int(t - start)
                if 0 <= k < 86400:
                    out[k] = float(parts[4])
        last = next((x for x in out if x), 0.0)
        for k in range(86400):
            if out[k]:
                last = out[k]
            else:
                out[k] = last
        _BN[key] = out
    return _BN[key]


def bn_price(series, t):
    symbol = "BTCUSDT" if series == "KXBTCD" else "ETHUSDT"
    dt = datetime.fromtimestamp(t, timezone.utc)
    arr = _bn_day(symbol, dt.strftime("%Y-%m-%d"))
    return arr[dt.hour * 3600 + dt.minute * 60 + dt.second] if arr else None


def mid_of(b, a, max_spread=6):
    return (a + b) / 2 if b and a and a - b <= max_spread else None


def _rest_filter(v, plan, net, model, mid):
    """Apply V.rest and V.quote_max_gap to a plan's resting quotes."""
    if v.rest == "none" or (v.rest == "reduce" and net == 0):
        plan.buy = plan.sell = None
    elif v.rest == "reduce":
        if net > 0:
            plan.buy = None
        else:
            plan.sell = None
    if v.quote_max_gap is not None and mid is not None and abs(model - mid) > v.quote_max_gap:
        if model > mid:
            plan.buy = None
        else:
            plan.sell = None
    return plan


def run_day(v: V, evs):
    fees.MAKER_RATE = v.maker_fee_rate
    risk = RiskManager(v.cfg)
    resting, fills, watched = {}, [], {}
    unwinds, renewed = [], 0
    offset = {}                     # ticker -> EMA of (model - mid), up to the previous minute
    alpha = 1 - 0.5 ** (1 / v.halflife_min)
    n = min(len(e["spots"]) for e in evs)
    for i in range(n):
        marks = {}
        hour_et = datetime.fromtimestamp(evs[0]["open"] + i * 60, ET).hour
        for ev in evs:
            s = ev["series"]
            if s not in v.strikes:
                continue
            spot, sd = ev["spots"][i], ev["sd"][i]
            hours_left = (ev["close"] - ev["open"]) / 3600 - i / 60
            tradeable = hours_left >= v.min_hours and ev["fit_ok"][i]
            if s not in watched or (v.roll_min and i % v.roll_min == 0):
                cands = sorted((ev["mkt"][t] for t in ev["trades"]), key=lambda m: abs(m["floor_strike"] - spot))
                watched[s] = {m["ticker"] for m in cands[:v.strikes[s]]}
            managed = watched[s] | {t for t in risk.positions if t in ev["mkt"] and risk.net(t) != 0}
            # offsets track every candidate strike all day, so a strike that
            # rolls on already has a warmed-up estimate
            if v.fv_mode == "offset":
                for t in ev["trades"]:
                    b, a = ev["books"][t][i]
                    m = mid_of(b, a)
                    if m is None:
                        continue
                    d = ev["pm"][t][i] - m
                    prev = offset.get(t)
                    offset[t] = d if prev is None else prev + alpha * (d - prev)
            if not managed:
                continue
            lean = {}
            if v.hedge_cents:
                delta = {t: _crypto_prob(ev["mkt"][t], spot * 1.005, sd) - _crypto_prob(ev["mkt"][t], spot * 0.995, sd)
                         for t in managed}
                port = sum(risk.net(t) * delta[t] for t in managed)
                dmax = max(delta.values()) or 1
                for t in managed:
                    lean[t] = -v.hedge_cents * L._clip(port / v.hedge_ref_usd) * delta[t] / dmax
            for t in sorted(managed):
                bid, ask = ev["books"][t][i]
                if bid is not None and ask is not None:
                    marks[t] = (bid + ask) / 2
                pm = ev["pm"][t][i] if t in ev["pm"] else _crypto_prob(ev["mkt"][t], spot, sd) * 100
                mid = mid_of(bid, ask)
                if v.fill_model == "sec" and i > 0:
                    # Each real trade meets the quote the bot would have had
                    # up at that second: model from the coin price latency_s
                    # earlier (Binance, scaled to Coinbase's level at the start
                    # of the minute), the bot's position at that moment, and
                    # the last Kalshi book it knew (end of minute i-1).
                    pb, pa = ev["books"][t][i - 1] if v.sec_clamp_book == "prev" else ev["books"][t][i]
                    prev_book = {"yes_bid": pb, "yes_ask": pa}
                    bn0 = bn_price(s, ev["open"] + i * 60 - 1)
                    ratio = ev["spots"][i - 1] / bn0 if bn0 else None
                    sd_q = ev["sd"][i - 1]
                    ok_q = hours_left >= v.min_hours and ev["fit_ok"][i - 1]
                    used = {}
                    for when, yes, px, cnt in (ev["by_min_ts"].get(t, {}).get(i, ()) if ratio else ()):
                        bq = bn_price(s, when - v.latency_s)
                        if not bq:
                            continue
                        pm_q = _crypto_prob(ev["mkt"][t], bq * ratio, sd_q) * 100
                        net_q = risk.net(t)
                        sides_q, _ = risk.allowed_sides(t)
                        if (t not in watched[s]) or (hour_et in v.no_open_hours) or \
                                (v.stop_adding_hours and hours_left < v.stop_adding_hours):
                            sides_q = sides_q & ({"sell"} if net_q > 0 else {"buy"} if net_q < 0 else set())
                        fvq = FairValue(min(99, max(1, pm_q + lean.get(t, 0))), "crypto", "", ok_q)
                        plan_q = _rest_filter(v, plan_quotes(prev_book, net_q, v.cfg, sides_q, fvq, anchor=pm_q),
                                              net_q, pm_q, mid_of(pb, pa))
                        action = "sell" if yes else "buy"
                        q = plan_q.sell if yes else plan_q.buy
                        if not q:
                            continue
                        qpx, qsize = q
                        avail = qsize - used.get((when, action), 0)
                        if avail <= 0:
                            continue
                        touch = pb if action == "buy" else pa
                        improved = touch is None or (qpx > touch if action == "buy" else qpx < touch)
                        through = px > qpx if action == "sell" else px < qpx
                        if through or (v.at_price_fills and px == qpx and improved):
                            fill = max(1, int(min(cnt, avail)))
                            risk.record_fill(t, "yes", action, fill, qpx, maker=True)
                            fills.append((i, ev, t, action, qpx, fill, "maker", pm_q, mid_of(pb, pa), hour_et, hours_left))
                            used[(when, action)] = used.get((when, action), 0) + fill
                if v.fill_model == "honest":
                    # Trades during minute i hit what was resting since the
                    # end of minute i-1 — before we see this minute's close.
                    r0 = resting.get(t, {})
                    for yes, px, cnt in ev["by_min"].get(t, {}).get(i, ()):
                        action = "sell" if yes else "buy"
                        q = r0.get(action)
                        if not q:
                            continue
                        qpx, qsize, improved = q[:3]
                        through = px > qpx if action == "sell" else px < qpx
                        if through or (v.at_price_fills and px == qpx and improved):
                            fill = max(1, int(min(cnt, qsize)))
                            risk.record_fill(t, "yes", action, fill, qpx, maker=True)
                            fills.append((i, ev, t, action, qpx, fill, "maker", q[3], q[4], hour_et, hours_left))
                            if qsize - fill > 0:
                                r0[action] = (qpx, qsize - fill, improved, q[3], q[4])
                            else:
                                r0.pop(action)
                if v.fv_mode == "offset":
                    # last minute's offset (this minute's gap is the signal)
                    off = prev_off.get(t) if (prev_off := ev.setdefault("_prev_off", {})) else None
                    raw = pm - off if off is not None else (mid if mid is not None else pm)
                elif v.fv_mode == "shrink" and mid is not None:
                    raw = mid + v.shrink * (pm - mid)
                else:
                    raw = pm
                for u in [u for u in unwinds if u[1] == t and u[0] <= i]:
                    _, _, action, size = u
                    net_u = risk.net(t)
                    size = min(size, -net_u if action == "buy" else net_u)
                    px = ask if action == "buy" else bid
                    if size <= 0:
                        unwinds.remove(u)
                        continue
                    if px is None:
                        continue
                    again = bid if action == "buy" else ask
                    if v.exit_renew and tradeable and again is not None and                             (again - raw if action == "buy" else raw - again)                             - fees.per_contract_fee_cents(again, maker=False) >= v.cfg.take_edge_cents:
                        u[0] = i + v.exit_after_min
                        renewed += 1
                        continue
                    risk.record_fill(t, "yes", action, size, px, maker=False)
                    fills.append((i, ev, t, action, px, size, "unwind", raw, mid, hour_et, hours_left))
                    unwinds.remove(u)
                old = dict(resting.get(t, {}))
                sides, _ = risk.allowed_sides(t)
                net = risk.net(t)
                reduce_only =(t not in watched[s]) or (hour_et in v.no_open_hours) or \
                              (v.stop_adding_hours and hours_left < v.stop_adding_hours)
                if reduce_only:
                    sides = sides & ({"sell"} if net > 0 else {"buy"} if net < 0 else set())
                book = {"yes_bid": bid, "yes_ask": ask}
                fv = FairValue(min(99, max(1, raw + lean.get(t, 0))), "crypto", "", tradeable, hours_left=hours_left)
                plan = plan_quotes(book, net, v.cfg, sides, fv, anchor=raw)
                plan.takes = plan_quotes(book, net, v.cfg, sides,
                                         FairValue(min(99, max(1, raw)), "crypto", "", tradeable,
                                                   hours_left=hours_left)).takes \
                    if v.takes and hours_left <= v.max_take_hours else []
                plan = _rest_filter(v, plan, net, raw, mid)
                if v.take_sizes and plan.takes:
                    sized = []
                    for action, price, size in plan.takes:
                        gap = (raw - price if action == "buy" else price - raw) - fees.per_contract_fee_cents(price, maker=False)
                        size = min(size, next((k for g, k in v.take_sizes if gap >= g), 0))
                        if size > 0:
                            sized.append((action, price, size))
                    plan.takes = sized
                cap = v.cfg.max_take_book_delta_usd
                if cap is not None and plan.takes and v.hedge_cents:
                    # same rule as MarketMaker._cap_take_delta
                    port_now, kept = sum(risk.net(x) * delta[x] for x in managed), []
                    for action, price, size in plan.takes:
                        step = delta[t] if action == "buy" else -delta[t]
                        while size > 0 and abs(port_now + step * size) > max(cap, abs(port_now)):
                            size -= 1
                        if size > 0:
                            kept.append((action, price, size))
                            port_now += step * size
                    plan.takes = kept
                for action, price, size in plan.takes:
                    risk.record_fill(t, "yes", action, size, price, maker=False)
                    fills.append((i, ev, t, action, price, size, "take", raw, mid, hour_et, hours_left))
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
                        r[action] = (target[0], target[1], improved, raw, mid)
                if v.fill_model == "between":
                    # Trades in minute i at the quote resting since i-1, but
                    # only if they'd also have hit the quote repriced at the
                    # end of minute i (assumes we requote before a taker
                    # reacts to a coin move — optimistic about speed).
                    for yes, px, cnt in ev["by_min"].get(t, {}).get(i, ()):
                        action = "sell" if yes else "buy"
                        q, nq = old.get(action), r.get(action)
                        if not q or not nq:
                            continue
                        qpx, qsize = q[0], q[1]
                        if (px > qpx and px > nq[0]) if action == "sell" else (px < qpx and px < nq[0]):
                            fill = max(1, int(min(cnt, qsize)))
                            risk.record_fill(t, "yes", action, fill, qpx, maker=True)
                            fills.append((i, ev, t, action, qpx, fill, "maker", q[3], q[4], hour_et, hours_left))
                            if qsize - fill > 0:
                                old[action] = (qpx, qsize - fill) + tuple(q[2:])
                            else:
                                old.pop(action)
                if v.fill_model in ("honest", "between", "sec"):
                    continue
                for yes, px, cnt in ev["by_min"].get(t, {}).get(i - 1 if v.strict_fills else i, ()):
                    action = "sell" if yes else "buy"
                    q = (prev if v.strict_fills else r).get(action)
                    if not q:
                        continue
                    qpx, qsize, improved = q[:3]
                    through = px > qpx if action == "sell" else px < qpx
                    if v.strict_fills:
                        nq = r.get(action)
                        through = through and nq is not None and (px > nq[0] if action == "sell" else px < nq[0])
                        improved = False
                    if through or (px == qpx and improved):
                        fill = max(1, int(min(cnt, qsize)))
                        risk.record_fill(t, "yes", action, fill, qpx, maker=True)
                        fills.append((i, ev, t, action, qpx, fill, "maker", raw, mid, hour_et, hours_left))
                        book_q = prev if v.strict_fills else r
                        if qsize - fill > 0:
                            book_q[action] = (qpx, qsize - fill, improved, raw, mid)
                        else:
                            book_q.pop(action)
            if v.fv_mode == "offset":
                ev["_prev_off"] = dict(offset)
        risk.update_marks(marks)
    for ev in evs:
        ev.pop("_prev_off", None)

    total = 0.0
    for ev in evs:
        for t, pos in risk.positions.items():
            if t not in ev["mkt"]:
                continue
            if pos.net_contracts:
                pos.settle(100 if ev["mkt"][t]["result"] == "yes" else 0, "")
            total += (pos.realized_pnl_cents - pos.total_fees_cents) / 100
    recs = []
    for i, ev, t, action, price, size, kind, fv, mid, hr, hl in fills:
        sgn = 1 if action == "buy" else -1
        settle = 100 if ev["mkt"][t]["result"] == "yes" else 0
        bk = ev["books"][t]
        m60 = mid_of(*bk[min(i + 60, len(bk) - 1)], max_spread=99)
        m5 = mid_of(*bk[min(i + 5, len(bk) - 1)], max_spread=99)
        m1 = mid_of(*bk[min(i + 1, len(bk) - 1)], max_spread=99)
        fee = fees.per_contract_fee_cents(price, maker=(kind == "maker")) * size
        recs.append({"kind": kind, "action": action, "series": ev["series"], "size": size, "hour": hr, "hours_left": hl,
                     "edge": sgn * (fv - price), "gap": (fv - mid) if mid is not None else None,
                     "settle_mo": sgn * (settle - price) * size,
                     "mo1": sgn * (m1 - price) * size if m1 is not None else None,
                     "mo5": sgn * (m5 - price) * size if m5 is not None else None,
                     "mo60": sgn * (m60 - price) * size if m60 is not None else None,
                     "fee": fee, "price": price, "date": ev["event"]})
    return {"pnl": total, "fills": recs, "renewed": renewed,
            "date": datetime.fromtimestamp(evs[0]["close"], timezone.utc).strftime("%m-%d")}


def summarize(name, rows):
    pnl = [r["pnl"] for r in rows]
    recs = [f for r in rows for f in r["fills"]]
    cum = peak = dd = 0.0
    for x in pnl:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    sd = statistics.pstdev(pnl) if len(pnl) > 1 else 0.0
    def part(pred):
        fs = [f for f in recs if pred(f)]
        c = sum(f["size"] for f in fs) or 1
        return {"n": len(fs), "contracts": sum(f["size"] for f in fs),
                "settle": sum(f["settle_mo"] for f in fs) / 100, "fees": sum(f["fee"] for f in fs) / 100,
                **{f"{k}_c": sum(f[k] for f in fs if f[k] is not None)
                   / (sum(f["size"] for f in fs if f[k] is not None) or 1) for k in ("mo1", "mo5", "mo60")},
                "settle_c": sum(f["settle_mo"] for f in fs) / c}
    return {"name": name, "days": len(pnl), "total": sum(pnl), "avg": statistics.mean(pnl), "sd": sd,
            "sharpe": statistics.mean(pnl) / sd * math.sqrt(365) if sd else 0.0,
            "win": sum(x > 0 for x in pnl), "worst": min(pnl), "maxdd": dd,
            "take": part(lambda f: f["kind"] == "take"), "maker": part(lambda f: f["kind"] == "maker"),
            "unwind": part(lambda f: f["kind"] == "unwind"),
            "btc": part(lambda f: f["series"] == "KXBTCD"), "eth": part(lambda f: f["series"] == "KXETHD"),
            "recs": recs, "daily": pnl}


def line(s):
    t, m = s["take"], s["maker"]
    return (f"{s['name']:<34} total ${s['total']:>+8.2f}  avg ${s['avg']:>+6.2f} ±{s['sd']:5.2f}  sharpe {s['sharpe']:4.1f}  "
            f"win {s['win']:>2}/{s['days']}  worst ${s['worst']:>+7.2f}  maxDD ${s['maxdd']:>6.2f} | "
            f"takes {t['contracts'] / s['days']:5.0f}c/d {t['settle_c']:+5.2f}¢ mo60 {t['mo60_c']:+5.2f}¢ | "
            f"maker {m['contracts'] / s['days']:5.0f}c/d {m['settle_c']:+5.2f}¢ mo60 {m['mo60_c']:+5.2f}¢ | "
            f"fees ${(t['fees'] + m['fees']):6.0f}")


_DAYS = {}


def days_for(cache):
    if cache not in _DAYS:
        L.CACHE = REPO / "research" / cache
        _DAYS[cache] = precompute(L.load_days())
    return _DAYS[cache]


def run(v, cache):
    days = days_for(cache)
    return summarize(v.name, [run_day(v, d) for d in days])


# ---- the candidate grid ---------------------------------------------------------

MODELS = {
    "normal": dict(fill_model="honest"),
    "pess": dict(fill_model="sec", latency_s=1, sec_clamp_book="prev"),
    "mid": dict(fill_model="between"),
    "opt": dict(fill_model="sec", latency_s=1, sec_clamp_book="next"),
}
# Each a change to the live settings (config.py's RiskConfig, BTC only).
CANDIDATES = {
    "live": {},
    "no takes": dict(takes=False),
    "takes only, no resting quotes": dict(rest="none"),
    "resting quotes only to reduce": dict(rest="reduce"),
    "quotes 4c from model": dict(cfg=replace(LIVE, min_edge_cents=4.0)),
    "quotes 8c from model": dict(cfg=replace(LIVE, min_edge_cents=8.0)),
    "quotes 10c from model": dict(cfg=replace(LIVE, min_edge_cents=10.0)),
    "quote size 5": dict(cfg=replace(LIVE, quote_size=5)),
    "no quote where model-mid > 8c": dict(quote_max_gap=8.0),
    "no quote where model-mid > 12c": dict(quote_max_gap=12.0),
    "take edge 5c": dict(cfg=replace(LIVE, take_edge_cents=5.0)),
    "take edge 6c": dict(cfg=replace(LIVE, take_edge_cents=6.0)),
    "takes 5 at 4-6c gap, 15 at 6c+": dict(take_sizes=((6.0, 15), (4.0, 5))),
    # (No "bigger takes" here: plan_quotes caps a take at quote_size, so
    # take_size above 15 changes nothing unless the resting quotes grow too.)
    "takes only <=8h to close": dict(max_take_hours=8.0),
    "takes only <=10h to close": dict(max_take_hours=10.0),
    "takes only <=12h to close": dict(max_take_hours=12.0),
    "takes only <=14h to close": dict(max_take_hours=14.0),
    "takes only <=16h to close": dict(max_take_hours=16.0),
    "takes only <=18h to close": dict(max_take_hours=18.0),
    "takes only <=20h to close": dict(max_take_hours=20.0),
    "stop 3h before close": dict(min_hours=3.0),
    "stop 4h before close": dict(min_hours=4.0),
    "4 strikes": dict(strikes={"KXBTCD": 4}),
    "8 strikes": dict(strikes={"KXBTCD": 8}),
    "no delta lean": dict(hedge_cents=0.0),
    "no inventory skew": dict(cfg=replace(LIVE, inventory_skew_cents=0.0)),
    "inventory skew 6c": dict(cfg=replace(LIVE, inventory_skew_cents=6.0)),
    "max position 40": dict(cfg=replace(LIVE, max_position_per_market=40)),
    "exit takes after 60m": dict(exit_after_min=60),
    "<=14h takes + quotes 8c": dict(max_take_hours=14.0, cfg=replace(LIVE, min_edge_cents=8.0)),
    "<=14h takes + stop 3h before": dict(max_take_hours=14.0, min_hours=3.0),
    "quotes 8c + stop 3h before": dict(cfg=replace(LIVE, min_edge_cents=8.0), min_hours=3.0),
    "<=14h + quotes 8c + stop 3h": dict(max_take_hours=14.0, cfg=replace(LIVE, min_edge_cents=8.0), min_hours=3.0),
    # config.py since 2026-10-29: the row above, through the bot's own rules
    # (RiskConfig.max_take_hours in plan_quotes, fair_value's 3h stop)
    "live since 10-29": dict(cfg=RiskConfig(), min_hours=CRYPTO_MIN_HOURS_TO_TRADE),
}
GRID = REPO / "research" / "cache_grid"


def _key(cache, name, model):
    return GRID / f"{cache}__{''.join(ch if ch.isalnum() else '_' for ch in name)}__{model}.json"


def cmd_grid(cache, model, only, part):
    import json
    GRID.mkdir(exist_ok=True)
    names = [n for n in CANDIDATES if not only or n in only]
    k, n = (int(x) for x in part.split("/"))
    for name in names[k - 1::n]:
        path = _key(cache, name, model)
        if path.exists():
            continue
        s = run(V(name, **MODELS[model], **CANDIDATES[name]), cache)
        s.pop("recs")
        path.write_text(json.dumps({"cache": cache, "model": model, **s}))
        print(f"{cache:<14} {model:<6} {line(s)}", flush=True)


def cmd_report(caches):
    """Each candidate against live, day by day, in every (cache, model) cell."""
    import json
    res = {}
    for p in GRID.glob("*.json"):
        r = json.loads(p.read_text())
        res[(r["cache"], r["name"], r["model"])] = r
    cells = [(c, m) for c in caches for m in MODELS if (c, "live", m) in res]
    print("$ per 31 days vs live (paired by day); t = diff / its standard error\n")
    print(f"{'candidate':<32} " + " ".join(f"{c[6:] or 'main'}/{m:<6}" for c, m in cells))
    rows = []
    for name in CANDIDATES:
        diffs, ts = [], []
        for c, m in cells:
            a, b = res.get((c, name, m)), res[(c, "live", m)]
            if not a:
                diffs.append(None)
                ts.append(None)
                continue
            d = [x - y for x, y in zip(a["daily"], b["daily"])]
            se = statistics.pstdev(d) * math.sqrt(len(d)) if len(d) > 1 else 0
            diffs.append(sum(d) * 31 / len(d))
            ts.append(sum(d) / se if se else 0.0)
        rows.append((name, diffs, ts))
    for name, diffs, ts in rows:
        print(f"{name:<32} " + " ".join(f"{d:>+8.0f}({t:+4.1f})" if d is not None else f"{'':>14}"
                                        for d, t in zip(diffs, ts)))
    print("\nlive itself:")
    for c, m in cells:
        b = res[(c, "live", m)]
        print(f"  {c:<14} {m:<6} total ${b['total']:>+8.2f}  sd/day ${b['sd']:5.2f}  worst ${b['worst']:>+7.2f}  "
              f"takes {b['take']['contracts']:>5}c settle {b['take']['settle_c']:+5.2f}¢ mo1/5/60 "
              f"{b['take']['mo1_c']:+5.2f}/{b['take']['mo5_c']:+5.2f}/{b['take']['mo60_c']:+5.2f} | "
              f"resting {b['maker']['contracts']:>5}c settle {b['maker']['settle_c']:+5.2f}¢ mo1/5/60 "
              f"{b['maker']['mo1_c']:+5.2f}/{b['maker']['mo5_c']:+5.2f}/{b['maker']['mo60_c']:+5.2f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid")
    g.add_argument("--cache", default="cache")
    g.add_argument("--model", choices=list(MODELS), required=True)
    g.add_argument("--only", nargs="*")
    g.add_argument("--part", default="1/1", help="k/n: run every n-th candidate starting at k (parallel workers)")
    rp = sub.add_parser("report")
    rp.add_argument("--caches", nargs="*", default=["cache", "cache_holdout"])
    a = ap.parse_args()
    if a.cmd == "grid":
        cmd_grid(a.cache, a.model, a.only, a.part)
    else:
        cmd_report(a.caches)
