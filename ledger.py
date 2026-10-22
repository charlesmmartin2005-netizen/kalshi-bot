"""
A simple append-only ledger of fills, stored as one JSON object per line
in fills_log.jsonl. This is what lets P&L tracking survive bot restarts —
everything else in this project only lives in memory while running.
"""
import json
import time
import os

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "fills_log.jsonl")


def append_fill(ticker: str, side: str, action: str, count: int, price_cents: int,
                 mode: str = "paper", liquidity: str = "maker", strategy: str = "",
                 model_cents: float | None = None):
    """Record one fill. Call this every time a real or simulated fill happens.
    liquidity: "maker" (our resting quote was hit) or "taker" (we crossed).
    model_cents: the model's price when the quote went up or the take was
    made; the record then also carries edge_cents, how far the fill price was
    on our side of it before fees (positive = the model liked the trade)."""
    record = {
        "ts": time.time(),
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
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


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
