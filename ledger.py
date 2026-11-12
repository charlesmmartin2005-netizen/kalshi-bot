"""
A simple append-only ledger of fills, stored as one JSON object per line
in fills_log.jsonl. This is what lets P&L tracking survive bot restarts —
everything else in this project only lives in memory while running.
"""
import json
import time
import os

from fees import per_contract_fee_cents

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "fills_log.jsonl")
TAKES_FILE = "takes_log.jsonl"     # in the ledger's folder


def append_fill(ticker: str, side: str, action: str, count: int, price_cents: int,
                 mode: str = "paper", liquidity: str = "maker", strategy: str = "",
                 model_cents: float | None = None, fee_cents: float | None = None,
                 order_id: str | None = None, trade_id: str | None = None, ts: float | None = None):
    """Record one fill. Call this every time a real or simulated fill happens.
    liquidity: "maker" (our resting quote was hit) or "taker" (we crossed).
    model_cents: the model's price when the quote went up or the take was
    made; the record then also carries edge_cents, how far the fill price was
    on our side of it before fees (positive = the model liked the trade).
    fee_cents, order_id, trade_id, ts: live fills only, as Kalshi reported them.
    A row with an order_id and no trade_id was booked from the order's own
    answer, before Kalshi sent the fill (MarketMaker.process_live_fills)."""
    record = {
        "ts": time.time() if ts is None else ts,
        "ticker": ticker,
        "side": side,       # "yes" or "no"
        "action": action,   # "buy" or "sell"
        "count": count,
        "price_cents": price_cents,
        "mode": mode,        # "paper" or "live" — keeps them distinguishable in one file
        "liquidity": liquidity,
        "strategy": strategy,  # "fair-value" | "book" | "exit" | "take"
    }
    if model_cents is not None:
        record["model_cents"] = round(model_cents, 2)
        record["edge_cents"] = round((model_cents - price_cents) if action == "buy" else (price_cents - model_cents), 2)
    for key, value in (("fee_cents", fee_cents), ("order_id", order_id), ("trade_id", trade_id)):
        if value is not None:
            record[key] = value
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def append_take(ticker: str, action: str, price_cents: int, count: float, model_cents: float, outcome: str,
                filled: float = 0, ms: float | None = None, hours_left: float | None = None):
    """Record one live take the bot tried, next to the fills ledger in
    takes_log.jsonl. outcome: "filled", "missed" (nothing left at the price),
    "failed" (the order was rejected or unanswered), or why it wasn't sent
    ("can't pay", "order limit"). gap_cents is how far the price was from the
    model after the taker fee; ms is how long Kalshi took to answer."""
    edge = (model_cents - price_cents) if action == "buy" else (price_cents - model_cents)
    record = {"ts": time.time(), "ticker": ticker, "action": action, "price_cents": price_cents, "count": count,
              "model_cents": round(model_cents, 2),
              "gap_cents": round(edge - per_contract_fee_cents(price_cents, maker=False), 2),
              "outcome": outcome, "filled": filled}
    if ms is not None:
        record["ms"] = round(ms, 1)
    if hours_left is not None:
        record["hours_left"] = round(hours_left, 2)
    with open(os.path.join(os.path.dirname(LEDGER_PATH), TAKES_FILE), "a") as f:
        f.write(json.dumps(record) + "\n")


def read_fills(mode: str) -> list:
    """One mode's fills. Paper and live share the file and never mix."""
    return [f for f in read_all_fills() if f.get("mode", "paper") == mode]


def read_all_fills() -> list:
    """Returns every recorded fill, oldest first. Empty list if no ledger yet."""
    if not os.path.exists(LEDGER_PATH):
        return []
    fills = []
    with open(LEDGER_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                fills.append(json.loads(line))
    return fills
