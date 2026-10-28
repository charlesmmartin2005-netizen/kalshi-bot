"""
Every open Kalshi event with its markets, in one sweep (~13k events / ~128k
markets, ~15s). Cached for CATALOG_TTL so the scanner and the arbitrage
finder can share one download.
"""
import logging
import threading
import time

log = logging.getLogger("catalog")

CATALOG_TTL = 300
_lock = threading.Lock()
_cache = {"ts": 0.0, "events": None}

# Only the fields scanner.py, arb.py and fair_value.py read. With all 48 per
# market the catalog held ~760 MB, and on the 1 GB server the sweep got the
# bot OOM-killed four times on 2026-10-28; this keeps it near 190 MB.
EVENT_FIELDS = ("event_ticker", "title", "category", "mutually_exclusive")
MARKET_FIELDS = ("ticker", "event_ticker", "status", "title", "yes_sub_title", "yes_bid_dollars",
                 "yes_ask_dollars", "volume_24h_fp", "open_interest_fp", "close_time",
                 "strike_type", "floor_strike", "cap_strike")


def _slim(event: dict) -> dict:
    out = {k: event[k] for k in EVENT_FIELDS if k in event}
    out["markets"] = [{k: m[k] for k in MARKET_FIELDS if k in m} for m in event.get("markets") or []]
    return out


def load_events(client, force: bool = False) -> list[dict]:
    with _lock:
        if not force and _cache["events"] is not None and time.time() - _cache["ts"] < CATALOG_TTL:
            return _cache["events"]
        t0, events, cursor = time.time(), [], None
        while True:
            resp = client.get_events(cursor=cursor)
            events += [_slim(e) for e in resp.get("events", [])]
            cursor = resp.get("cursor")
            if not cursor:
                break
        _cache.update(ts=time.time(), events=events)
        log.info("Catalog: %d events, %d markets in %.1fs", len(events),
                 sum(len(e.get("markets") or []) for e in events), time.time() - t0)
        return events


def age_seconds() -> float | None:
    return time.time() - _cache["ts"] if _cache["events"] is not None else None
