"""
Prints the full raw JSON for one market so we can see the ACTUAL field
names Kalshi returns, instead of guessing. Run this once, paste the output
back, and we'll fix find_liquid_markets_v2.py to use the real field names.

Usage:
    python debug_market_fields.py KXBTCD-26AUG3017-T87249.99
"""
import json
import sys
from kalshi_client import build_client
from config import CONFIG


def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_market_fields.py <TICKER>")
        print("Example: python debug_market_fields.py KXBTCD-26AUG3017-T87249.99")
        return

    ticker = sys.argv[1]
    client = build_client(CONFIG)

    print(f"--- get_market({ticker}) ---")
    try:
        market = client.get_market(ticker)
        print(json.dumps(market, indent=2))
    except Exception as e:
        print(f"ERROR: {e}")

    print(f"\n--- get_orderbook({ticker}) ---")
    try:
        book = client.get_orderbook(ticker)
        print(json.dumps(book, indent=2))
    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    main()
