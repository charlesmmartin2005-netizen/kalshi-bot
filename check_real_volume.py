"""
Depth (resting order levels) turned out not to predict fills — 7 Fed
markets with depth ~20 produced zero fills over 5 hours on a real weekday.
This checks actual trade VOLUME instead, which is a much more direct
signal of whether a market genuinely trades or just has stale resting
orders sitting untouched.

Usage:
    python check_real_volume.py
"""
from kalshi_client import build_client
from config import CONFIG

TICKERS_TO_CHECK = [
    "KXFEDDECISION-28JAN-H0",
    "KXFEDDECISION-27DEC-H0",
    "KXFEDDECISION-27OCT-H25",
    "KXFEDDECISION-27SEP-H0",
    "KXFEDDECISION-28JAN-C25",
    "KXFEDDECISION-27DEC-H26",
    "KXFEDDECISION-27DEC-C26",
    "KXBTCD-26AUG3017-T78999.99",  # last night's Bitcoin contract, for comparison
]


def main():
    client = build_client(CONFIG)

    print(f"{'TICKER':<35} {'VOL 24H':>10} {'TOTAL VOL':>10} {'OPEN INT':>10}")
    print("-" * 70)
    for ticker in TICKERS_TO_CHECK:
        try:
            resp = client.get_market(ticker)
            m = resp.get("market", {})
            vol_24h = float(m.get("volume_24h_fp", "0") or 0)
            vol_total = float(m.get("volume_fp", "0") or 0)
            open_interest = float(m.get("open_interest_fp", "0") or 0)
            print(f"{ticker:<35} {vol_24h:>10.0f} {vol_total:>10.0f} {open_interest:>10.0f}")
        except Exception as e:
            print(f"{ticker:<35} ERROR: {e}")

    print("\nVOL 24H = contracts traded in the last 24 hours (the real signal for")
    print("'does this market actually trade'). A market can show real book DEPTH")
    print("while having near-zero actual VOLUME — that combination means stale,")
    print("rarely-touched resting orders, not an active market.")


if __name__ == "__main__":
    main()
