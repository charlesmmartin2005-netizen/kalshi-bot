"""
Throwaway liquidity checker.

Run this to see which candidate tickers actually have real resting orders
before committing one to CONFIG.watched_tickers in main.py.

Usage:
    python check_liquidity.py

Edit the CANDIDATE_TICKERS list below with tickers you're considering.
"""
from auth import KalshiAuth
from kalshi_client import KalshiClient, normalize_orderbook
from config import CONFIG

# Edit this list with tickers you're considering. Find them the same way
# you found the last one: browse to a specific open market on kalshi.com
# and copy the exact ticker from the URL or page.
CANDIDATE_TICKERS = [
    # "KXBTCD-26AUG29-T110000",
    # "KXINXU-26AUG29-...",
    # add a few real candidates here
]


def main():
    if not CANDIDATE_TICKERS:
        print("Edit CANDIDATE_TICKERS at the top of this file with real tickers first.")
        return

    auth = KalshiAuth(CONFIG.kalshi.api_key_id, CONFIG.kalshi.private_key_path)
    client = KalshiClient(CONFIG.kalshi.base_url, auth)

    print(f"{'TICKER':<35} {'YES BID':>8} {'YES ASK':>8} {'SPREAD':>8} {'BID LEVELS':>11} {'ASK LEVELS':>11}")
    print("-" * 90)

    for ticker in CANDIDATE_TICKERS:
        try:
            raw = client.get_orderbook(ticker, depth=10)
            book = normalize_orderbook(raw)
            yes_bid = book["yes_bid"] if book["yes_bid"] is not None else "-"
            yes_ask = book["yes_ask"] if book["yes_ask"] is not None else "-"
            spread = book["spread_cents"] if book["spread_cents"] is not None else "-"
            n_bid_levels = len(book["raw_yes_levels"])
            n_ask_levels = len(book["raw_no_levels"])
            print(f"{ticker:<35} {str(yes_bid):>8} {str(yes_ask):>8} {str(spread):>8} "
                  f"{n_bid_levels:>11} {n_ask_levels:>11}")
        except Exception as e:
            print(f"{ticker:<35} ERROR: {e}")

    print("\nLook for: non-empty bid/ask (not '-'), a spread that's not huge (a few cents,")
    print("not 20+), and multiple price levels on both sides. That combination means")
    print("real two-sided liquidity worth quoting against.")


if __name__ == "__main__":
    main()
