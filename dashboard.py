"""
Web dashboard for the bot. Run with:

    python dashboard.py

then open http://127.0.0.1:8050 (it opens automatically). The bot runs on a
background thread inside this process; the page polls /api/state.

Binds to localhost only — the control endpoints can start/stop trading, so
never expose this port to a network.
"""
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import arb
import ledger
import pnl
import scanner
from config import CONFIG
from engine import Engine

HOST, PORT = "127.0.0.1", 8050
STATIC = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder=None)
engine = Engine(CONFIG)

# pnl.compute may call the API for settlements, so cache it rather than
# recomputing on every poll.
_cache = {"pnl": (0.0, None)}
_cache_lock = threading.Lock()
PNL_TTL = 5.0


def _pnl_summary():
    with _cache_lock:
        ts, value = _cache["pnl"]
        if value is not None and time.time() - ts < PNL_TTL:
            return value
    s = pnl.compute(client=engine.client)
    value = {
        "balance_usd": s["balance_usd"],
        "starting_balance_usd": s["starting_balance_usd"],
        "net_usd": s["net_usd"],
        "realized_usd": s["realized_usd"],
        "fees_usd": s["fees_usd"],
        "fill_count": s["fill_count"],
        "equity_curve": s["equity_curve"],
        "markets": [
            {"ticker": ml.ticker, "fills": ml.fill_count, "net": ml.net_contracts,
             "avg_cost": round(ml.avg_cost_cents, 1), "realized_usd": ml.realized_pnl_cents / 100,
             "fees_usd": ml.total_fees_cents / 100, "note": ml.settlement_note}
            for ml in s["markets"]
        ],
        "recent_fills": ledger.read_all_fills()[-40:][::-1],
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


@app.get("/api/state")
def state():
    snap = engine.snapshot()
    snap["pnl"] = _pnl_summary()
    snap["server_time"] = time.time()
    return jsonify(snap)


# ---- background sweeps: scanner + arbitrage over all of Kalshi ------------
# One full catalog download takes ~15s, so it never runs inside a request:
# a worker refreshes both every SWEEP_SECONDS and the endpoints return the
# latest result immediately.

SWEEP_SECONDS = 300
_sweep = {"scan": None, "arb": None, "running": False, "error": None}
_sweep_wake = threading.Event()


def _run_sweep(force_catalog: bool):
    _sweep["running"] = True
    try:
        rows = scanner.scan(engine.client, force=force_catalog)
        suitable = [r for r in rows if r["tradeable"]]
        _sweep["scan"] = {
            "ts": time.time(), "markets_active": len(rows),
            "counts": {s: sum(1 for r in suitable if r["strategy"] == s) for s in ("fair-value", "book")},
            # top of each strategy list + the best rejected ones (for "why not")
            "rows": ([r for r in suitable if r["strategy"] == "fair-value"][:40]
                     + [r for r in suitable if r["strategy"] == "book"][:40]
                     + [r for r in rows if not r["tradeable"]][:20]),
        }
        _sweep["arb"] = arb.scan(engine.client)       # reuses the catalog just loaded
        _sweep["error"] = None
    except Exception as e:
        logging.getLogger("dashboard").exception("Sweep failed")
        _sweep["error"] = str(e)
    finally:
        _sweep["running"] = False


def _sweeper():
    force = True
    while True:
        _run_sweep(force)
        force = _sweep_wake.wait(SWEEP_SECONDS)    # True if woken by "Rescan"
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


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"\n  Kalshi MM dashboard → {url}   ({engine.mode} mode)\n")
    threading.Thread(target=_sweeper, name="sweeper", daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)
    finally:
        engine.stop("dashboard closed")
