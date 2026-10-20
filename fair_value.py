"""
Fair-value models: an independent estimate of what a contract is worth, so
the bot can quote around a price it believes instead of just around
whatever the order book says. Two families are supported:

  CRYPTO  (KXBTCD/KXBTC, KXETHD/KXETH, KXSOLD/KXSOL, KXXRPD/KXXRP)
    P(price at close beyond the strike), from the live Coinbase price and
    recent realized volatility, rescaled every 60s so the model matches the
    event's own strike ladder. Kalshi settles on the CF Benchmarks index (a
    60s average of several exchanges), which Coinbase spot tracks closely.
    Returns are modeled as a mix of 85% normal(sigma) + 15% normal(2*sigma)
    because crypto has fat tails. The edge this targets is SPEED: the model
    agrees with the market at rest and reprices the instant spot moves,
    while resting Kalshi quotes lag.

  DAILY HIGH TEMPERATURE  (KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHAUS,
                           KXHIGHDEN, KXHIGHLAX, KXHIGHPHIL)
    P(official high in a bracket), from the National Weather Service hourly
    forecast plus today's observed high so far. Forecast error grows with
    lead time (~1.5°F same day, ~2.5°F next day, wider after).

Both are estimates. Every number they produce is a model opinion, not a
fact, which is why config.take_edge_cents is deliberately large.
"""
import logging
import math
import re
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("fair_value")
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "kalshi-mm-bot (personal research)"


@dataclass
class FairValue:
    cents: float        # model probability of YES, in cents (1-99)
    model: str          # "crypto" | "weather"
    detail: str         # short human-readable basis, shown on the dashboard
    # Whether the bot may trade on this value. Validation on 2026-10-19:
    # crypto (>2h to close) matched the market within ~3¢ on average with no
    # bias across 37 strikes; weather disagreed with the market by 13-39¢,
    # with the market clearly better informed (it already "knew" today's
    # high). So weather is display-only until it's calibrated.
    tradeable: bool = False
    # Crypto only: which coin it's a bet on, and how much the probability
    # moves (0-1 units) for a 1% move in that coin — used for the delta lean.
    underlying: str = ""
    delta: float = 0.0

# Last hours before close: settlement is a 60s index average and short-term
# moves are smaller than hourly vol implies — the model was 12¢ off with
# 12 minutes left. Don't trade on it in that window.
CRYPTO_MIN_HOURS_TO_TRADE = 2.0


# ---- tiny TTL cache ----------------------------------------------------------

_cache: dict = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _clip(c):
    return min(99.0, max(1.0, c))


def _series(market: dict) -> str:
    return (market.get("event_ticker") or market.get("ticker", "")).split("-")[0]


# ---- crypto -------------------------------------------------------------------

CRYPTO_SERIES = {
    "KXBTCD": "BTC-USD", "KXBTC": "BTC-USD",
    "KXETHD": "ETH-USD", "KXETH": "ETH-USD",
    "KXSOLD": "SOL-USD", "KXSOL": "SOL-USD",
    "KXXRPD": "XRP-USD", "KXXRP": "XRP-USD",
}
TAIL_WEIGHT, TAIL_MULT = 0.15, 2.0


def crypto_spot(product: str) -> float:
    def fetch():
        r = HTTP.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    return _cached(("spot", product), 1, fetch)


def crypto_sigma_per_sqrt_hour(product: str) -> float:
    """Realized volatility of log returns per sqrt(hour). Takes the larger of
    the last-24h and last-7d estimates, so a sudden calm doesn't make the
    model overconfident."""
    def fetch():
        r = HTTP.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                     params={"granularity": 3600}, timeout=8)
        r.raise_for_status()
        closes = [c[4] for c in sorted(r.json(), key=lambda c: c[0])][-169:]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        return max(statistics.pstdev(rets[-24:]), statistics.pstdev(rets))
    return _cached(("sigma", product), 600, fetch)


def _prob_beyond(log_ratio_sd_units):
    """P(Z > x) under the fat-tailed mixture, x in units of sigma."""
    x = log_ratio_sd_units
    return (1 - TAIL_WEIGHT) * (1 - _norm_cdf(x)) + TAIL_WEIGHT * (1 - _norm_cdf(x / TAIL_MULT))


def _crypto_prob(market, spot, sd) -> float | None:
    def p_above(k):
        return _prob_beyond(math.log(k / spot) / sd)
    st, lo, hi = market.get("strike_type"), market.get("floor_strike"), market.get("cap_strike")
    if st == "greater" and lo is not None:
        return p_above(lo)
    if st == "less" and hi is not None:
        return 1 - p_above(hi)
    if st == "between" and lo is not None and hi is not None:
        return p_above(lo) - p_above(hi)
    return None


def _event_vol_multiplier(event_ticker, spot, base_sd):
    """Scale realized vol so the model matches the market's own ladder.

    Unscaled, the model ran ~2-4¢ off across a whole BTC ladder in a
    consistent pattern (rich on high strikes, cheap on low ones) — i.e. it
    disagreed with the market about volatility, which would have made the
    bot a volatility bettor. Calibrating to the ladder's mids (strikes priced
    10-90¢) removes that; what's left is the edge we actually want: when
    spot moves, the model reprices instantly while resting Kalshi quotes lag.
    Refit every 60s."""
    def fit():
        from kalshi_client import dollars_to_cents
        r = HTTP.get("https://api.elections.kalshi.com/trade-api/v2/markets",
                     params={"event_ticker": event_ticker, "limit": 200}, timeout=8).json()
        pts = []
        for m in r.get("markets", []):
            b, a = dollars_to_cents(m.get("yes_bid_dollars")), dollars_to_cents(m.get("yes_ask_dollars"))
            if b and a and a < 100 and a - b <= 4 and 10 <= (a + b) / 2 <= 90:
                pts.append((m, (a + b) / 2))
        if len(pts) < 3:
            return 1.0, 0
        best = min((sum((_crypto_prob(m, spot, base_sd * k) * 100 - mid) ** 2 for m, mid in pts), k)
                   for k in [x / 20 for x in range(6, 61)])   # 0.30x … 3.00x
        return best[1], len(pts)
    return _cached(("volfit", event_ticker), 60, fit)


def crypto_fair_value(market: dict) -> FairValue | None:
    product = CRYPTO_SERIES.get(_series(market))
    close = market.get("close_time")
    if not product or not close:
        return None
    hours = (datetime.fromisoformat(close.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours <= 0:
        return None
    spot, sigma = crypto_spot(product), crypto_sigma_per_sqrt_hour(product)
    base_sd = sigma * math.sqrt(max(hours, 1 / 60))
    mult, n_fit = _event_vol_multiplier(market.get("event_ticker"), spot, base_sd)
    p = _crypto_prob(market, spot, base_sd * mult)
    if p is None:
        return None
    sym = product.split("-")[0]
    fit = f"vol fit ×{mult:.2f} on {n_fit} strikes" if n_fit else "realized vol (no fit)"
    delta = (_crypto_prob(market, spot * 1.005, base_sd * mult)
             - _crypto_prob(market, spot * 0.995, base_sd * mult))
    return FairValue(_clip(p * 100), "crypto",
                     f"{sym} ${spot:,.2f} · σ {sigma * mult * 100:.2f}%/√h ({fit}) · {hours:.1f}h left",
                     # Without a ladder fit (thin books, e.g. SOL ran 27¢ off
                     # raw) the model has nothing to anchor to — display only.
                     tradeable=hours >= CRYPTO_MIN_HOURS_TO_TRADE and n_fit >= 3,
                     underlying=product, delta=delta)


# ---- daily high temperature -----------------------------------------------------

# series -> (lat, lon, NWS observation station, timezone). Stations are the
# ones Kalshi's rules cite (e.g. CLINYC = Central Park).
WEATHER_SERIES = {
    "KXHIGHNY": (40.7789, -73.9692, "KNYC", "America/New_York"),
    "KXHIGHCHI": (41.7868, -87.7522, "KMDW", "America/Chicago"),
    "KXHIGHMIA": (25.7881, -80.3169, "KMIA", "America/New_York"),
    "KXHIGHAUS": (30.1945, -97.6699, "KAUS", "America/Chicago"),
    "KXHIGHDEN": (39.8466, -104.6562, "KDEN", "America/Denver"),
    "KXHIGHLAX": (33.9382, -118.3866, "KLAX", "America/Los_Angeles"),
    "KXHIGHPHIL": (39.8733, -75.2268, "KPHL", "America/New_York"),
}
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _event_date(event_ticker: str) -> date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def _hourly_forecast(lat, lon):
    def fetch():
        pts = HTTP.get(f"https://api.weather.gov/points/{lat},{lon}", timeout=10).json()
        url = pts["properties"]["forecastHourly"]
        periods = HTTP.get(url, timeout=10).json()["properties"]["periods"]
        return [(datetime.fromisoformat(p["startTime"]), float(p["temperature"])) for p in periods]
    return _cached(("nws", lat, lon), 1800, fetch)


def _observed_high_today(station, tz):
    def fetch():
        midnight = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
        r = HTTP.get(f"https://api.weather.gov/stations/{station}/observations",
                     params={"start": midnight.isoformat()}, timeout=10).json()
        temps_c = [f["properties"]["temperature"]["value"] for f in r.get("features", [])]
        temps_f = [c * 9 / 5 + 32 for c in temps_c if c is not None]
        return max(temps_f) if temps_f else None
    return _cached(("obs", station), 600, fetch)


def weather_fair_value(market: dict) -> FairValue | None:
    spec = WEATHER_SERIES.get(_series(market))
    day = _event_date(market.get("event_ticker", ""))
    if not spec or not day:
        return None
    lat, lon, station, tz = spec
    zone = ZoneInfo(tz)
    now_local = datetime.now(zone)
    lead_days = (day - now_local.date()).days
    if lead_days < 0:
        return None

    remaining = [t for start, t in _hourly_forecast(lat, lon)
                 if start.astimezone(zone).date() == day and start >= now_local - timedelta(hours=1)]
    observed = _observed_high_today(station, tz) if lead_days == 0 else None
    if not remaining and observed is None:
        return None
    fc_high = max(remaining) if remaining else -999
    if observed is not None and observed >= fc_high + 1:
        mu, sd = observed, 0.8            # today's high is very likely already in
    else:
        mu = max(fc_high, observed or -999)
        sd = {0: 1.5, 1: 2.5, 2: 3.0}.get(lead_days, 3.5)

    # Official highs are whole degrees: "greater 72" = 73+, "less 65" = 64-,
    # "between 71,72" = 71 or 72. Continuity-correct by half a degree.
    st, lo, hi = market.get("strike_type"), market.get("floor_strike"), market.get("cap_strike")
    cdf = lambda x: _norm_cdf((x - mu) / sd)
    if st == "greater" and lo is not None:
        p = 1 - cdf(lo + 0.5)
    elif st == "less" and hi is not None:
        p = cdf(hi - 0.5)
    elif st == "between" and lo is not None and hi is not None:
        p = cdf(hi + 0.5) - cdf(lo - 0.5)
    else:
        return None
    basis = f"NWS high {fc_high:.0f}°F" if remaining else "no forecast left"
    if observed is not None:
        basis += f" · observed {observed:.0f}°F so far"
    return FairValue(_clip(p * 100), "weather", f"{basis} · ±{sd:.1f}°F · display only",
                     tradeable=False)


# ---- entry point -------------------------------------------------------------------

def supported(market: dict) -> bool:
    s = _series(market)
    return s in CRYPTO_SERIES or s in WEATHER_SERIES


def fair_value(market: dict) -> FairValue | None:
    """Model price for a market dict (as returned by GET /markets), or None
    if no model covers it or the data source is unavailable."""
    try:
        s = _series(market)
        if s in CRYPTO_SERIES:
            return crypto_fair_value(market)
        if s in WEATHER_SERIES:
            return weather_fair_value(market)
    except Exception as e:
        log.warning("Fair value unavailable for %s: %s", market.get("ticker"), e)
    return None
