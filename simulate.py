"""
Monte Carlo forecast of how the bot would behave over the next N days on the
markets it's watching, calibrated on each market's own recent history.

Method (block bootstrap):
  1. Pull ~60 days of hourly order-book candles and every public trade for
     each watched market.
  2. Cut that history into whole days. A simulated future is N days drawn at
     random (with replacement) from those real days, stitched together as
     *changes* starting from today's live book — so volatility, spread
     regimes and trading activity all come from what actually happened,
     including the quiet days.
  3. Run the bot's rules over each path, hour by hour, filling quotes from
     the sampled real taker trades (the same rule paper mode now uses: a
     trade fills us if our quote would have been first in line for it).
     Average-cost P&L with Kalshi's fee formula; open positions are marked
     at the final mid.

Two rule sets are compared on the SAME sampled futures:
  - OLD: the first build — quote 1¢ inside when spread clears 4¢, and stop
    quoting entirely at the position limit (no inventory management, loss
    breakers never called).
  - NEW: strategy.plan_quotes + the current RiskManager — inventory skew,
    reduce-only at limits, exit quotes, working loss breakers.

Fair-value markets (crypto) can't be bootstrapped this way — their edge
comes from reacting to spot moves within seconds, which hourly history
can't replay. Paper-trade those instead.

Usage:
    python simulate.py                 # watched tickers from .env
    python simulate.py TICKER [TICKER ...] --days 14 --paths 2000
"""
import argparse
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from config import CONFIG
from kalshi_client import build_client, dollars_to_cents, normalize_orderbook
from pnl import MarketLedger
from risk_manager import RiskManager
from strategy import plan_quotes

HISTORY_DAYS = 60
HOUR = 3600


# ---- data ------------------------------------------------------------------

def fetch_trades(client, ticker, since_ts):
    trades, cursor = [], None
    while True:
        params = {"ticker": ticker, "limit": 1000, "min_ts": since_ts}
        if cursor:
            params["cursor"] = cursor
        resp = client._request("GET", "/markets/trades", params=params, authenticated=False)
        trades += resp.get("trades", [])
        cursor = resp.get("cursor")
        if not cursor:
            return trades


def fetch_candles(client, series, ticker, start_ts, end_ts):
    resp = client._request("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                           params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 60},
                           authenticated=False)
    return resp.get("candlesticks", [])


def build_history(client, ticker):
    """Hourly rows: {bid, ask, bid_hi, ask_lo, trades:[(taker_buys_yes, price, count)]}
    grouped into whole days. Hours with no candle carry the last book forward."""
    market = client.get_market(ticker)["market"]
    series = market["event_ticker"].split("-")[0]
    now = int(time.time())
    start = now - HISTORY_DAYS * 86400
    start -= start % HOUR

    candles = {}
    for c in fetch_candles(client, series, ticker, start, now):
        b, a = c.get("yes_bid", {}), c.get("yes_ask", {})
        if b.get("close_dollars") is None or a.get("close_dollars") is None:
            continue
        candles[c["end_period_ts"] - HOUR] = {
            "bid": dollars_to_cents(b["close_dollars"]), "ask": dollars_to_cents(a["close_dollars"]),
            "bid_hi": dollars_to_cents(b.get("high_dollars")), "ask_lo": dollars_to_cents(a.get("low_dollars")),
        }

    trades_by_hour = defaultdict(list)
    for t in fetch_trades(client, ticker, start):
        ts = datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
        trades_by_hour[int(ts) - int(ts) % HOUR].append(
            (t["taker_side"] == "yes", dollars_to_cents(t["yes_price_dollars"]), float(t["count_fp"])))

    # Days are keyed by calendar date (UTC) so different markets can be
    # sampled on the same historical day.
    days, last = defaultdict(list), None
    for h in range(start - start % 86400 + 86400, now - now % 86400, HOUR):
        c = candles.get(h)
        if c and c["bid"] and c["ask"] and c["ask"] < 100:
            last = c
        if last is None:
            continue
        row = {**last, "trades": trades_by_hour.get(h, [])}
        if c is None:   # carried-forward hour: no intra-hour extremes
            row.update(bid_hi=last["bid"], ask_lo=last["ask"])
        days[h - h % 86400].append(row)
    return market, {d: rows for d, rows in days.items() if len(rows) == 24}


def to_relative(days):
    """Express each hour relative to that hour's mid so blocks can be stitched
    onto any starting price: mid change, half-spread, extremes, trade offsets."""
    out = {}
    for key, day in days.items():
        rel, prev_mid = [], (day[0]["bid"] + day[0]["ask"]) / 2
        for r in day:
            mid = (r["bid"] + r["ask"]) / 2
            rel.append({
                "dmid": mid - prev_mid, "spread": r["ask"] - r["bid"],
                "bid_hi": (r["bid_hi"] or r["bid"]) - mid, "ask_lo": (r["ask_lo"] or r["ask"]) - mid,
                "trades": [(buy, p - mid, n) for buy, p, n in r["trades"]],
            })
            prev_mid = mid
        out[key] = rel
    return out


def sample_path(rel_days, start_mid):
    mid, path = start_mid, []
    for day in rel_days:
        for h in day:
            mid = min(99 - h["spread"] / 2, max(1 + h["spread"] / 2, mid + h["dmid"]))
            bid = max(1, round(mid - h["spread"] / 2))
            ask = min(99, bid + h["spread"])
            path.append({
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "bid_hi": min(ask - 1, round(mid + h["bid_hi"])), "ask_lo": max(bid + 1, round(mid + h["ask_lo"])),
                "trades": [(buy, round(mid + off), n) for buy, off, n in h["trades"]],
            })
    return path


# ---- the bot's rules -------------------------------------------------------

def _hits(taker_buys, price, action, quote_px):
    """Would this real trade have filled our quote? Our quote is placed 1¢
    inside the touch (or further back), so at an equal price we're first in
    line only because we improved the touch — treated as a fill, matching
    market_maker.process_trades for an improving quote."""
    if taker_buys and action == "sell":
        return price >= quote_px
    if not taker_buys and action == "buy":
        return price <= quote_px
    return False


def run_bot(paths: dict, rules: str) -> dict:
    """paths: ticker -> list of hourly books (same length). One RiskManager
    shared across markets, like the live engine. rules: "old" | "new"."""
    cfg = CONFIG.risk
    risk = RiskManager(cfg)
    quoting_hours = defaultdict(int)
    limit_hours = defaultdict(int)
    hours = len(next(iter(paths.values())))
    peak, max_dd = 0.0, 0.0

    def fill(t, action, n, price):
        risk.record_fill(t, "yes", action, max(1, int(n)), price, maker=True)

    for i in range(1, hours):
        for t, path in paths.items():
            prev, cur = path[i - 1], path[i]
            book = {"yes_bid": prev["bid"], "yes_ask": prev["ask"]}

            if rules == "old":
                net = risk.net(t)
                if risk.halted or abs(net) >= cfg.max_position_per_market \
                        or risk.total_exposure_usd() >= cfg.max_total_exposure_usd:
                    limit_hours[t] += abs(net) >= cfg.max_position_per_market
                    continue
                if prev["ask"] - prev["bid"] < 4:      # old flat-fee threshold
                    continue
                quotes = {"buy": prev["bid"] + 1, "sell": prev["ask"] - 1}
                if quotes["buy"] >= quotes["sell"]:
                    continue
                quoting_hours[t] += 1
                for taker_buys, price, n in cur["trades"]:
                    if abs(risk.net(t)) >= cfg.max_position_per_market:
                        break           # old bot froze both sides here
                    for action, px in quotes.items():
                        if _hits(taker_buys, price, action, px):
                            fill(t, action, min(n, cfg.quote_size), px)
                continue

            # NEW rules — re-plan after every fill (position, skew and the
            # allowed sides all change), as the live bot does each cycle.
            sides, why = risk.allowed_sides(t)
            if "limit" in why:
                limit_hours[t] += 1
            plan = plan_quotes(book, risk.net(t), cfg, sides)
            if plan.buy or plan.sell:
                quoting_hours[t] += 1
            for taker_buys, price, n in cur["trades"]:
                for action, q in (("buy", plan.buy), ("sell", plan.sell)):
                    if q and _hits(taker_buys, price, action, q[0]):
                        fill(t, action, min(n, q[1]), q[0])
                        sides, _ = risk.allowed_sides(t)
                        plan = plan_quotes(book, risk.net(t), cfg, sides)
                        break

        risk.update_marks({t: p[i]["mid"] for t, p in paths.items()})
        if rules == "old":      # the first build never called its loss breakers
            risk.halted, risk.reduce_only = False, {}
        mtm = risk.session_pnl_usd
        peak = max(peak, mtm)
        max_dd = max(max_dd, peak - mtm)

    per = {}
    for t in paths:
        pos = risk.positions.get(t)
        per[t] = {
            "pnl": risk.market_pnl_usd(t),
            "fills": pos.fill_count if pos else 0,
            "net": pos.net_contracts if pos else 0,
            "quoting_pct": quoting_hours[t] / (hours - 1),
            "limit_hours": limit_hours[t],
        }
    return {"per": per, "total": sum(p["pnl"] for p in per.values()), "max_dd": max_dd,
            "halted": risk.halted and rules == "new",
            "reduce_only": sum(1 for t in risk.reduce_only)}


# ---- report -----------------------------------------------------------------

def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(q * len(xs))))]


def describe_history(ticker, days):
    days = list(days.values())
    rows = [r for d in days for r in d]
    trades = [tr for r in rows for tr in r["trades"]]
    spreads = [r["ask"] - r["bid"] for r in rows]
    mids = [(r["bid"] + r["ask"]) / 2 for r in rows]
    vol = sum(n for _, _, n in trades)
    active_days = sum(1 for d in days if any(r["trades"] for r in d))
    return (f"  {ticker}: {len(days)} days of history · {len(trades)} trades ({vol:,.0f} contracts) · "
            f"trades on {active_days}/{len(days)} days · median spread {statistics.median(spreads):.0f}¢ · "
            f"mid range {min(mids):.0f}–{max(mids):.0f}¢")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--paths", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    tickers = args.tickers or CONFIG.watched_tickers
    if not tickers:
        sys.exit("No tickers — pass them as arguments or set KALSHI_WATCHED_TICKERS.")

    client = build_client(CONFIG)
    rng = random.Random(args.seed)
    print(f"Calibrating on the last {HISTORY_DAYS} days...")
    rel, start_mid = {}, {}
    for t in tickers:
        market, days = build_history(client, t)
        if len(days) < 5:
            print(f"  {t}: only {len(days)} days of history — skipping")
            continue
        print(describe_history(t, days))
        rel[t] = to_relative(days)
        book = normalize_orderbook(client.get_orderbook(t))
        last_day = days[max(days)]
        start_mid[t] = ((book["yes_bid"] + book["yes_ask"]) / 2) if book["yes_bid"] and book["yes_ask"] \
            else (last_day[-1]["bid"] + last_day[-1]["ask"]) / 2
    tickers = list(rel)
    common = sorted(set.intersection(*(set(rel[t]) for t in tickers)))
    print(f"  {len(common)} calendar days shared by all markets are used for sampling")

    results = {"old": [], "new": []}
    for _ in range(args.paths):
        # One shared draw of historical days per path, so the markets move
        # together the way they actually did (same Fed news hits all three).
        picks = [rng.choice(common) for _ in range(args.days)]
        paths = {t: sample_path([rel[t][d] for d in picks], start_mid[t]) for t in tickers}
        for rules in results:
            results[rules].append(run_bot(paths, rules))

    print(f"\nSimulated {args.paths:,} possible {args.days}-day futures, identical for both rule sets · "
          f"quote size {CONFIG.risk.quote_size}, max position {CONFIG.risk.max_position_per_market}, "
          f"fills from real taker trades\n")
    for rules, label in (("old", "OLD rules — first build (no inventory management)"),
                         ("new", "NEW rules — inventory skew, reduce-only, exit quotes, live loss limits")):
        rs = results[rules]
        tot = [r["total"] for r in rs]
        print(label)
        print(f"  {'market':<28} {'fills (median)':>15} {'quoting':>8} {'P&L p5':>9} {'median':>9} {'p95':>9} {'hit pos limit':>15}")
        for t in tickers:
            f = [r["per"][t]["fills"] for r in rs]
            p = [r["per"][t]["pnl"] for r in rs]
            q = statistics.mean(r["per"][t]["quoting_pct"] for r in rs)
            lim = sum(1 for r in rs if r["per"][t]["limit_hours"] > 0) / len(rs)
            print(f"  {t:<28} {statistics.median(f):>15.0f} {q:>7.0%} {pct(p, .05):>+9.2f} "
                  f"{statistics.median(p):>+9.2f} {pct(p, .95):>+9.2f} {lim:>14.0%}")
        print(f"  {'PORTFOLIO':<28} {'':>15} {'':>8} {pct(tot, .05):>+9.2f} {statistics.median(tot):>+9.2f} {pct(tot, .95):>+9.2f}")
        extra = ""
        if rules == "new":
            extra = (f" · halted by loss limit {sum(r['halted'] for r in rs) / len(rs):.0%}"
                     f" · a market went reduce-only on loss {sum(r['reduce_only'] > 0 for r in rs) / len(rs):.0%}")
        print(f"  chance of profit {sum(1 for x in tot if x > 0) / len(tot):.0%} · chance of loss "
              f"{sum(1 for x in tot if x < 0) / len(tot):.0%} · mean P&L ${statistics.mean(tot):+.2f} · "
              f"median max drawdown ${statistics.median(r['max_dd'] for r in rs):.2f}{extra}\n")


if __name__ == "__main__":
    main()
