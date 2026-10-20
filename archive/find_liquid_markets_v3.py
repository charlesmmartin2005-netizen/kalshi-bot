"""
The market-list summary fields (volume_fp etc.) turned out to be
unreliable for picking a liquid strike — many out-of-the-money strikes in
a ladder show zero trade volume even while carrying a live quote. This
script instead pulls the REAL order book for each candidate market
directly (like debug_market_fields.py did manually) and ranks by actual
resting order depth on both sides — which is what your bot actually needs
to quote against.

Usage:
    python find_liquid_markets_v3.py
"""
from auth import KalshiAuth
from kalshi_client import KalshiClient, normalize_orderbook
from config import CONFIG

SERIES_TO_CHECK = [
    "KXBTCD",
    "KXBTC",
    "KXBTC15M",
    "KXINXU",
    "KXNASDAQ100U",
    "KXFEDDECISION",
    "KXHIGHNY",
]

# How many markets per series to check order books for. Keep modest —
# each one is a real API call.
MARKETS_PER_SERIES = 25


def main():
    auth = KalshiAuth(CONFIG.kalshi.api_key_id, CONFIG.kalshi.private_key_path)
    client = KalshiClient(CONFIG.kalshi.base_url, auth)

    results = []

    for series in SERIES_TO_CHECK:
        try:
            resp = client.get_markets(status="open", limit=MARKETS_PER_SERIES, series_ticker=series)
            markets = resp.get("markets", [])
        except Exception as e:
            print(f"{series}: ERROR fetching markets — {e}")
            continue

        print(f"Checking {len(markets)} markets in {series}...")
        for m in markets:
            ticker = m.get("ticker")
            try:
                raw_book = client.get_orderbook(ticker, depth=10)
                book = normalize_orderbook(raw_book)
                depth = len(book["raw_yes_levels"]) + len(book["raw_no_levels"])
                two_sided = book["yes_bid"] is not None and book["yes_ask"] is not None
                results.append({
                    "ticker": ticker,
                    "series": series,
                    "title": (m.get("title") or "")[:40],
                    "yes_bid": book["yes_bid"],
                    "yes_ask": book["yes_ask"],
                    "spread": book["spread_cents"],
                    "depth": depth,
                    "two_sided": two_sided,
                })
            except Exception as e:
                results.append({
                    "ticker": ticker, "series": series, "title": "ERROR",
                    "yes_bid": None, "yes_ask": None, "spread": None, "depth": -1,
                })

    # Sort two-sided markets first (these are the only ones your bot can
    # actually quote against), then by depth within that.
    results.sort(key=lambda r: (r["two_sided"], r["depth"]), reverse=True)

    print(f"\n{'TICKER':<38} {'2-SIDED':>8} {'DEPTH':>6} {'BID':>6} {'ASK':>6} {'SPREAD':>7} {'TITLE'}")
    print("-" * 110)
    for r in results[:35]:
        print(f"{r['ticker']:<38} {str(r['two_sided']):>8} {r['depth']:>6} {str(r['yes_bid']):>6} "
              f"{str(r['yes_ask']):>6} {str(r['spread']):>7} {r['title']}")

    n_two_sided = sum(1 for r in results if r["two_sided"])
    n_tradeable = sum(1 for r in results if r["two_sided"] and r["spread"] is not None and r["spread"] >= 4)
    print(f"\n{n_two_sided} of {len(results)} checked markets have real two-sided quotes.")
    print(f"{n_tradeable} of those have a spread of 4\u00a2+ — wide enough to clear your bot's")
    print("current min_edge_cents + fee threshold in config.py. Those are the ones")
    print("actually worth putting in CONFIG.watched_tickers right now.")


if __name__ == "__main__":
    main()
