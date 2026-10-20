"""
Lists real, currently open Kalshi markets sorted by trading volume, so you
can pick a genuinely liquid ticker without guessing at formats by hand.

Usage:
    python find_liquid_markets.py
"""
from auth import KalshiAuth
from kalshi_client import KalshiClient
from config import CONFIG


def main():
    auth = KalshiAuth(CONFIG.kalshi.api_key_id, CONFIG.kalshi.private_key_path)
    client = KalshiClient(CONFIG.kalshi.base_url, auth)

    all_markets = []
    cursor = None
    pages_fetched = 0
    max_pages = 10  # ~1000 markets, plenty to find liquid ones

    print("Fetching open markets from Kalshi...")
    while pages_fetched < max_pages:
        resp = client.get_markets(status="open", limit=100, cursor=cursor)
        markets = resp.get("markets", [])
        all_markets.extend(markets)
        cursor = resp.get("cursor")
        pages_fetched += 1
        if not cursor or not markets:
            break

    print(f"Fetched {len(all_markets)} open markets. Sorting by volume...\n")

    # Kalshi market objects typically include volume and volume_24h fields.
    # Sort by whichever is present, most-liquid first.
    def volume_key(m):
        return m.get("volume_24h", m.get("volume", 0)) or 0

    all_markets.sort(key=volume_key, reverse=True)

    top = all_markets[:25]

    print(f"{'TICKER':<35} {'VOL 24H':>10} {'YES BID':>8} {'YES ASK':>8} {'TITLE'}")
    print("-" * 110)
    for m in top:
        ticker = m.get("ticker", "?")
        vol = volume_key(m)
        yes_bid = m.get("yes_bid", "-")
        yes_ask = m.get("yes_ask", "-")
        title = (m.get("title") or m.get("subtitle") or "")[:40]
        print(f"{ticker:<35} {vol:>10} {str(yes_bid):>8} {str(yes_ask):>8} {title}")

    print("\nPick a ticker near the top of this list (highest volume) and paste it")
    print("exactly as printed above into CONFIG.watched_tickers in main.py.")


if __name__ == "__main__":
    main()
