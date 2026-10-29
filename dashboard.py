"""
Web dashboard for the bot. Run with:

    python dashboard.py                              # opens the page; click Start to trade
    python dashboard.py --autostart --no-browser     # unattended: trades straight away (run-bot.ps1)

then open http://127.0.0.1:8050 (it opens automatically). The bot runs on a
background thread inside this process; the page polls /api/state. Logs go to
logs/bot.log as well as the console.

Binds to localhost only — the control endpoints can start/stop trading, so
never expose this port to a network. Only one dashboard runs at a time: a
second one finds the port taken and exits, so two bots never trade the same
ledger.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

import ledger
import markouts
import pnl
from config import CONFIG
from engine import Engine
from kalshi_client import KalshiClient, build_client, top_of_book

HOST, PORT = "127.0.0.1", 8050
STATIC = Path(__file__).parent / "static"
LOG_DIR = Path(__file__).parent / "logs"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
ET = ZoneInfo("America/New_York")   # Kalshi's event times

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
# An unattended bot has no console to read, so keep a log file too
# (5 MB x 4 files at most).
LOG_DIR.mkdir(exist_ok=True)
_log_file = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_log_file.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(_log_file)
if "--autostart" in sys.argv:
    # Unattended: the file is the record, and run-bot.ps1 keeps stderr for crashes.
    for h in [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]:
        logging.getLogger().removeHandler(h)
log = logging.getLogger("dashboard")

app = Flask(__name__, static_folder=None)
engine = Engine(CONFIG)

# pnl.compute may call the API for settlements, so cache it rather than
# recomputing on every poll.
_cache = {"pnl": (0.0, None)}
_cache_lock = threading.Lock()
PNL_TTL = 5.0
MARK_MAX_AGE = 10.0   # seconds an engine book still counts as the current price


def _mid(bid, ask):
    """Mid of a book; a missing side counts as 0¢ / 100¢ (e.g. no bids
    under a nearly worthless strike)."""
    if bid is None and ask is None:
        return None
    return ((bid or 0) + (ask if ask is not None else 100)) / 2


def _marks(tickers):
    """Current mid for each open position: the engine's book while it's
    fresh, otherwise one batched REST request (e.g. while the bot is
    stopped, or for a market it no longer watches)."""
    out, missing, now = {}, [], time.time()
    with engine.lock:
        for t in tickers:
            b = engine.books.get(t)
            if b and now - b["updated"] < MARK_MAX_AGE:
                out[t] = _mid(b["yes_bid"], b["yes_ask"])
            if out.get(t) is None:
                missing.append(t)
    if missing:
        try:
            for m in engine.client.get_markets_batch(missing):
                b = top_of_book(m)
                out[m["ticker"]] = _mid(b["yes_bid"], b["yes_ask"])
        except Exception as e:
            logging.getLogger("dashboard").warning("Couldn't price open positions: %s", e)
    return out


def _settlement(markets):
    """Open positions grouped by event, with what each band pays at settlement
    (pnl.settlement_bands). Only "above the strike" markets, like the BTC ladder."""
    events = {}
    for ml in markets:
        if not ml.net_contracts or ml.settled:
            continue
        info = engine.market_info.get(ml.ticker, {})
        strike, kind = info.get("floor_strike"), info.get("strike_type")
        if strike is None:          # no longer watched: read it off the ticker (…-T81249.99)
            part = ml.ticker.rsplit("-", 1)[-1]
            try:
                strike, kind = (float(part[1:]), "greater") if part.startswith("T") else (None, None)
            except ValueError:
                strike = None
        if kind != "greater" or strike is None:
            continue
        ev = ml.ticker.rsplit("-", 1)[0]
        e = events.setdefault(ev, {"event": ev, "close_time": None, "positions": []})
        e["close_time"] = e["close_time"] or info.get("close_time")
        e["positions"].append({"ticker": ml.ticker, "strike": strike, "net": ml.net_contracts,
                               "avg_cost": round(ml.avg_cost_cents, 2)})
    for e in events.values():
        e["close_time"] = e["close_time"] or _event_close(e["event"])
        e["bands"] = pnl.settlement_bands([(p["strike"], p["net"], p["avg_cost"]) for p in e["positions"]])
    return sorted(events.values(), key=lambda e: e["close_time"] or e["event"])


_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _event_close(event_ticker):
    """Close time from an event ticker like KXBTCD-26OCT2217 (Oct 22 2026, 5pm ET)."""
    code = event_ticker.rsplit("-", 1)[-1]
    try:
        return datetime(2000 + int(code[:2]), _MONTHS[code[2:5]], int(code[5:7]), int(code[7:9]),
                        tzinfo=ET).isoformat()
    except (KeyError, ValueError):
        return None


def _settled_by(ticker, since):
    """(price, result) if the market had closed by `since` and has settled."""
    close = _event_close(ticker.rsplit("-", 1)[0])
    if close and datetime.fromisoformat(close).timestamp() <= since:
        final, price, result = pnl.check_settlement(engine.client, ticker)
        if final:
            return price, result
    return None


def _today_usd(fills, equity_usd):
    """Equity now minus the account at midnight ET (pnl.pnl_since)."""
    since = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return pnl.pnl_since(fills, since, equity_usd, _settled_by)


# The dashboard's P&L bars: the last 1/7/30 days, 1/4/12 weeks, 1/3/6 months.
PERIOD_DAYS = (1, 7, 28, 30, 84, 91, 182)


def _period_usd(fills, equity_usd):
    now = time.time()
    return {str(d): pnl.pnl_since(fills, now - d * 86400, equity_usd, _settled_by) for d in PERIOD_DAYS}


def _pnl_summary():
    with _cache_lock:
        ts, value = _cache["pnl"]
        if value is not None and time.time() - ts < PNL_TTL:
            return value
    fills = ledger.read_all_fills()
    s = pnl.compute(client=engine.client, fills=fills, get_marks=_marks)
    value = {
        "balance_usd": s["balance_usd"],
        "equity_usd": s["equity_usd"],
        "starting_balance_usd": s["starting_balance_usd"],
        "net_usd": s["net_usd"],
        "realized_usd": s["realized_usd"],
        "unrealized_usd": s["unrealized_usd"],
        "unmarked": s["unmarked"],
        "fees_usd": s["fees_usd"],
        "fill_count": s["fill_count"],
        "equity_curve": s["equity_curve"],
        "markets": [
            {"ticker": ml.ticker, "fills": ml.fill_count, "net": ml.net_contracts,
             "avg_cost": round(ml.avg_cost_cents, 1), "realized_usd": ml.realized_pnl_cents / 100,
             "mark": ml.mark_cents, "open_usd": ml.unrealized_pnl_cents / 100 if ml.mark_cents is not None else None,
             "fees_usd": ml.total_fees_cents / 100, "note": ml.settlement_note}
            for ml in s["markets"]
        ],
        "recent_fills": fills[-40:][::-1],
        "settlement": _settlement(s["markets"]),
        "today_usd": _today_usd(fills, s["equity_usd"]),
        "periods": _period_usd(fills, s["equity_usd"]),
    }
    with _cache_lock:
        _cache["pnl"] = (time.time(), value)
    return value


def _result(ok_msg):
    ok, msg = ok_msg
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    return send_from_directory(STATIC, name)


@app.get("/api/state")
def state():
    snap = engine.snapshot()
    snap["pnl"] = _pnl_summary()
    snap["server_time"] = time.time()
    return jsonify(snap)


# ---- background sweeps: scanner + arbitrage over all of Kalshi ------------
# One full catalog download takes ~30s, so it never runs inside a request:
# a worker refreshes both every SWEEP_SECONDS and the endpoints return the
# latest result immediately. The sweep runs in its own process (sweep.py):
# in this one, freeing each ~600 MB catalog froze the trading loop for 1-5s.

SWEEP_SECONDS = 300
SWEEP_TIMEOUT = 600
# The markouts refresh shares Kalshi's rate limit with trading, so it runs
# low-priority.
SWEEP_CLIENT = build_client(CONFIG)
SWEEP_CLIENT.low_priority = True
_sweep = {"scan": None, "arb": None, "running": False, "error": None}
_sweep_wake = threading.Event()


def _run_sweep():
    _sweep["running"] = True
    # Kalshi rate-limits both processes together: leave the sweep its share
    # while it runs (sharing the full rate got trade-tape requests 429'd).
    full_rps = KalshiClient.MAX_REQUESTS_PER_SECOND
    KalshiClient.MAX_REQUESTS_PER_SECOND = full_rps - KalshiClient.LOW_PRIORITY_RPS
    try:
        r = subprocess.run([sys.executable, "sweep.py"], cwd=Path(__file__).parent, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=SWEEP_TIMEOUT,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise RuntimeError((r.stderr.strip().splitlines() or [f"exit code {r.returncode}"])[-1])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        _sweep["scan"], _sweep["arb"] = out["scan"], out["arb"]
        _sweep["error"] = None
        log.info("Sweep: %d active markets, %d arbitrage candidates in %.0fs", out["scan"]["markets_active"],
                 out["arb"].get("candidates", 0), out["seconds"])
    except Exception as e:
        log.exception("Sweep failed")
        _sweep["error"] = str(e)
    finally:
        KalshiClient.MAX_REQUESTS_PER_SECOND = full_rps
        _sweep["running"] = False


def _sweeper():
    while True:
        _run_sweep()
        _sweep_wake.wait(SWEEP_SECONDS)    # or sooner, on "Rescan"
        _sweep_wake.clear()


@app.get("/api/scan")
def scan():
    if request.args.get("force") == "1" and not _sweep["running"]:
        _sweep_wake.set()
    return jsonify({**(_sweep["scan"] or {"rows": [], "ts": None}),
                    "running": _sweep["running"], "error": _sweep["error"]})


@app.get("/api/arb")
def arbitrage():
    return jsonify({**(_sweep["arb"] or {"confirmed": [], "ts": None}), "running": _sweep["running"]})


# ---- today's markouts (Edge view) -------------------------------------------
# One candle request per traded market, so refreshed in the background at
# most every MARKOUT_TTL, on the low-priority client.

MARKOUT_TTL = 300
_mk ={"ts": 0.0, "value": None, "running": False, "settled": {}}


def _refresh_markouts():
    try:
        since = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        mode = "paper" if CONFIG.kalshi.paper_trading else "live"
        fills = [f for f in ledger.read_all_fills() if f.get("mode", "paper") == mode and f["ts"] >= since]
        scored = markouts.score(SWEEP_CLIENT, fills, _mk["settled"])
        takes = [f for f in scored if f.get("liquidity") == "taker" and f.get("edge_cents") is not None]
        _mk["value"] = {"since": since, "fills": len(fills), "horizons": list(markouts.HORIZONS),
                        "by_liquidity": markouts.by(scored, lambda f: f.get("liquidity", "maker")),
                        "takes_by_gap": markouts.by(takes, markouts.gap_bucket),
                        "updated": time.time(), "error": None}
    except Exception as e:
        log.warning("Markouts failed: %s", e)
        _mk["value"] = {**(_mk["value"] or {}), "error": str(e)}
    finally:
        _mk["ts"] = time.time()
        _mk["running"] = False


@app.get("/api/markouts")
def markout_summary():
    if not _mk["running"] and time.time() - _mk["ts"] > MARKOUT_TTL:
        _mk["running"] = True
        threading.Thread(target=_refresh_markouts, name="markouts", daemon=True).start()
    return jsonify({**(_mk["value"] or {}), "running": _mk["running"]})


@app.post("/api/start")
def start():
    return _result(engine.start())


@app.post("/api/stop")
def stop():
    return _result(engine.stop())


@app.post("/api/kill")
def kill():
    return _result(engine.kill())


@app.post("/api/reset")
def reset():
    return _result(engine.reset_halt())


@app.post("/api/tickers")
def add_ticker():
    return _result(engine.add_ticker((request.get_json(silent=True) or {}).get("ticker", "")))


@app.post("/api/autopick")
def autopick():
    return _result(engine.autopick())


@app.post("/api/autorotate")
def autorotate():
    return _result(engine.set_auto_rotate((request.get_json(silent=True) or {}).get("on", False)))


@app.delete("/api/tickers/<ticker>")
def remove_ticker(ticker):
    return _result(engine.remove_ticker(ticker))


def _port_taken() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) == 0


def _autostart():
    ok, msg = engine.start()
    log.info("Autostart: %s", msg if ok else f"not started ({msg})")


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if _port_taken():
        # e.g. the hidden bot run-bot.ps1 keeps running: open that one
        # instead of starting a second bot on the same ledger.
        log.info("Dashboard already running at %s — not starting another", url)
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        sys.exit(1)
    print(f"\n  Kalshi MM dashboard → {url}   ({engine.mode} mode)\n")
    log.info("Dashboard starting (%s mode, %s)", engine.mode, " ".join(sys.argv[1:]) or "no options")
    threading.Thread(target=_sweeper, name="sweeper", daemon=True).start()
    if "--autostart" in sys.argv:
        threading.Timer(2.0, _autostart).start()      # once the server is up
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
    except Exception:
        log.exception("Dashboard crashed")
        raise
    finally:
        engine.stop("dashboard closed")
