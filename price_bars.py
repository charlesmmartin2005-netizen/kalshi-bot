"""
Long price charts for the dashboard, without growing the bot's memory (the
server has 909 MB, and a full market catalog has already OOM-killed it once).

The market's own prices come from Kalshi's 1-minute candles when a chart asks
for them. They cover the whole market, including any time the bot wasn't
running, so nothing needs keeping. Only the model's price is ours alone, so the
bot writes it to disk as one-minute bars: one file per market in price_bars/,
one line per minute, [minute_start_ts, open, high, low, close].

In memory there's only each market's current minute, and files are written on
their own thread, so recording costs the trading path a dict lookup.
"""
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from kalshi_client import dollars_to_cents

log = logging.getLogger("price_bars")

FOLDER = Path(__file__).parent / "price_bars"
BAR_SECONDS = 60
FLUSH_SECONDS = 20
KEEP_DAYS = 30               # a file not written to for this long is deleted
PRUNE_EVERY_SECONDS = 3600
CHART_DAYS = 5               # how far back a chart reaches (120 one-hour candles)
CANDLES_PER_REQUEST = 4800   # Kalshi's limit is 5000
CANDLE_TTL_SECONDS = 30      # a market's candles are re-asked for at most this often
REFETCH_SECONDS = 300        # re-ask for the last few minutes: the newest candle may still be open
CACHE_MARKETS = 2            # only the last markets charted stay in memory

_TICKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def valid_ticker(ticker: str) -> bool:
    """Tickers name files here, so nothing with a path separator gets through."""
    return bool(_TICKER.fullmatch(ticker))


class BarStore:
    """The model's one-minute bars: the current minute in memory, the rest on disk."""

    def __init__(self, folder=FOLDER):
        self.folder = Path(folder)
        self._lock = threading.Lock()
        self._open: dict[str, list] = {}    # ticker -> [minute_ts, o, h, l, c]
        self._done: list = []               # (ticker, bar) waiting to be written
        self._stop = threading.Event()
        self._thread = None
        self._pruned_at = 0.0

    def add(self, ticker, ts, value):
        """Record the model's price. In memory only, safe on the trading path."""
        if value is None:
            return
        minute = int(ts) // BAR_SECONDS * BAR_SECONDS
        with self._lock:
            bar = self._open.get(ticker)
            if bar is None or bar[0] != minute:
                if bar is not None:
                    self._done.append((ticker, bar))
                self._open[ticker] = [minute, value, value, value, value]
            else:
                bar[2] = max(bar[2], value)
                bar[3] = min(bar[3], value)
                bar[4] = value

    def flush(self, now=None):
        """Write every finished minute, including those of markets no longer
        being recorded."""
        minute = int(time.time() if now is None else now) // BAR_SECONDS * BAR_SECONDS
        with self._lock:
            for ticker, bar in list(self._open.items()):
                if bar[0] < minute:
                    self._done.append((ticker, bar))
                    del self._open[ticker]
            done, self._done = self._done, []
        if not done:
            return
        by_ticker: dict[str, list] = {}
        for ticker, bar in done:
            by_ticker.setdefault(ticker, []).append(bar)
        self.folder.mkdir(exist_ok=True)
        for ticker, bars in by_ticker.items():
            if valid_ticker(ticker):
                with open(self.folder / f"{ticker}.jsonl", "a", encoding="utf-8") as f:
                    f.write("".join(json.dumps(b, separators=(",", ":")) + "\n" for b in bars))

    def read(self, ticker, since=0) -> list:
        """A market's bars from `since` on, oldest first, with the minute in progress."""
        out = []
        path = self.folder / f"{ticker}.jsonl"
        if valid_ticker(ticker) and path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        bar = json.loads(line)
                    except ValueError:      # a line cut off by a crash
                        continue
                    if bar[0] >= since:
                        out.append(bar)
        with self._lock:
            pending = [b for t, b in self._done if t == ticker and b[0] >= since]
            current = self._open.get(ticker)
            if current is not None and current[0] >= since:
                pending.append(list(current))
        return out + pending

    def prune(self, now=None):
        cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400
        for path in self.folder.glob("*.jsonl"):
            if path.stat().st_mtime < cutoff:
                path.unlink()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="price-bars", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.wait(FLUSH_SECONDS):
            try:
                self.flush()
                if time.time() - self._pruned_at > PRUNE_EVERY_SECONDS:
                    self._pruned_at = time.time()
                    self.prune()
            except Exception as e:      # a chart is never worth stopping for
                log.warning("Couldn't write price bars: %s", e)


def fetch_candles(client, ticker, start, end) -> dict:
    """{minute_start_ts: (mid open, high, low, close, volume)} from Kalshi's
    1-minute candles, in cents. Each mid is halfway between the same point of
    the bid's candle and the ask's. A minute missing either side is left out."""
    series = ticker.split("-")[0]
    out = {}
    for s in range(int(start), int(end), CANDLES_PER_REQUEST * BAR_SECONDS):
        r = client._request("GET", f"/series/{series}/markets/{ticker}/candlesticks",
                            params={"start_ts": s, "end_ts": min(int(end), s + CANDLES_PER_REQUEST * BAR_SECONDS),
                                    "period_interval": 1},
                            authenticated=False)
        for c in r.get("candlesticks", []):
            bid, ask = c.get("yes_bid") or {}, c.get("yes_ask") or {}
            mids = []
            for point in ("open", "high", "low", "close"):
                b = dollars_to_cents(bid.get(f"{point}_dollars"))
                a = dollars_to_cents(ask.get(f"{point}_dollars"))
                if b is None or a is None or (b == 0 and a == 100):     # an empty book isn't a price (it's how every market closes)
                    break
                mids.append((b + a) / 2)
            if len(mids) == 4:
                out[c["end_period_ts"] - BAR_SECONDS] = (*mids, float(c.get("volume_fp") or 0))
    return out


class CandleCache:
    """Kalshi's candles for the last few markets charted, topped up rather than
    downloaded again each time a chart refreshes. If Kalshi can't be reached,
    the chart draws what it has."""

    def __init__(self, keep=CACHE_MARKETS):
        self.keep = keep
        self._lock = threading.Lock()
        self._markets: OrderedDict = OrderedDict()

    def get(self, client, ticker, now=None) -> dict:
        now = int(time.time() if now is None else now)
        with self._lock:
            entry = self._markets.pop(ticker, None) or {"at": 0, "minutes": {}}
            if now - entry["at"] >= CANDLE_TTL_SECONDS:
                start = entry["at"] - REFETCH_SECONDS if entry["at"] else now - CHART_DAYS * 86400
                try:
                    entry["minutes"].update(fetch_candles(client, ticker, start, now))
                    entry["at"] = now
                except Exception as e:
                    log.warning("Couldn't get candles for %s: %s", ticker, e)
            self._markets[ticker] = entry
            while len(self._markets) > self.keep:
                self._markets.popitem(last=False)
            return dict(entry["minutes"])


def rows(candles: dict, model_bars: list, since=0) -> list:
    """One market's chart, oldest first: [minute_ts, mid open, high, low, close,
    volume, model open, high, low, close], with None where a side has no data."""
    model = {b[0]: b[1:] for b in model_bars}
    out = []
    for t in sorted(set(candles) | set(model)):
        if t >= since:
            out.append([t, *(candles.get(t) or (None,) * 5), *(model.get(t) or (None,) * 4)])
    return out
