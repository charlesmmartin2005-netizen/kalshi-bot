"""
Instead of scanning all open markets blindly (which surfaces obscure
zero-volume combo/parlay markets first), this checks a curated list of
series that are typically liquid: Bitcoin price bands, S&P 500, Nasdaq,
and Fed decisions. Prints the highest-volume currently open market in
each series.

Usage:
    python find_liquid_markets_v2.py

Feel free to add/remove series tickers in SERIES_TO_CHECK below.
"""
from auth import KalshiAuth
from kalshi_client import KalshiClient
from config import CONFIG

# Common series prefixes for typically-active markets. Not guaranteed to
# be exactly right or exhaustive — Kalshi adds/renames series over time —
# but a reasonable starting set.
SERIES_TO_CHECK = [
    "KXBTCD",       # Bitcoin price, daily
    "KXBTC",        # Bitcoin price, other cadence
    "KXINXU",       # S&P 500
    "KXNASDAQ100U",  # Nasdaq 100
    "KXFEDDECISION",  # Fed rate decision
]


def volume_key(m):
    """Kalshi's market objects use *_fp suffixed string fields, confirmed
    against a live response: volume_fp, volume_24h_fp, liquidity_dollars.
    These are decimal strings, not integers — convert before comparing."""
    raw = m.get("volume_24h_fp") or m.get("volume_fp") or "0"
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def price_display(m, field):
    raw = m.get(field)
    return raw if raw is not None else "-"


def main():
    auth = KalshiAuth(CONFIG.kalshi.api_key_id, CONFIG.kalshi.private_key_path)
    client = KalshiClient(CONFIG.kalshi.base_url, auth)

    print(f"{'SERIES':<18} {'TICKER':<38} {'VOL':>8} {'BID':>6} {'ASK':>6} {'TITLE'}")
    print("-" * 110)

    any_found = False
    for series in SERIES_TO_CHECK:
        try:
            resp = client.get_markets(status="open", limit=100, series_ticker=series)
            markets = resp.get("markets", [])
            if not markets:
                print(f"{series:<18} (no open markets found for this series)")
                continue
            markets.sort(key=volume_key, reverse=True)
            best = markets[0]
            any_found = True
            title = (best.get("title") or best.get("subtitle") or "")[:35]
            print(f"{series:<18} {best.get('ticker', '?'):<38} "
                  f"{volume_key(best):>8.2f} {str(price_display(best, 'yes_bid_dollars')):>6} "
                  f"{str(price_display(best, 'yes_ask_dollars')):>6} {title}")
            # Show a couple more from this series too, in case the top one
            # isn't the specific contract you want.
            for m in markets[1:4]:
                title = (m.get("title") or m.get("subtitle") or "")[:35]
                print(f"{'':<18} {m.get('ticker', '?'):<38} "
                      f"{volume_key(m):>8.2f} {str(price_display(m, 'yes_bid_dollars')):>6} "
                      f"{str(price_display(m, 'yes_ask_dollars')):>6} {title}")
        except Exception as e:
            print(f"{series:<18} ERROR: {e}")

    if not any_found:
        print("\nNo markets found in any checked series. Series names may be stale —")
        print("verify exact series tickers on kalshi.com and update SERIES_TO_CHECK.")
    else:
        print("\nPick a ticker with real volume and a tight-ish bid/ask spread, then")
        print("paste it exactly into CONFIG.watched_tickers in main.py.")


if __name__ == "__main__":
    main()
