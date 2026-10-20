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


def load_events(client, force: bool = False) -> list[dict]:
    with _lock:
        if not force and _cache["events"] is not None and time.time() - _cache["ts"] < CATALOG_TTL:
            return _cache["events"]
        t0, events, cursor = time.time(), [], None
        while True:
            resp = client.get_events(cursor=cursor)
            events += resp.get("events", [])
            cursor = resp.get("cursor")
            if not cursor:
                break
        _cache.update(ts=time.time(), events=events)
        log.info("Catalog: %d events, %d markets in %.1fs", len(events),
                 sum(len(e.get("markets") or []) for e in events), time.time() - t0)
        return events


def age_seconds() -> float | None:
    return time.time() - _cache["ts"] if _cache["events"] is not None else None
