"""
Reads fills_log.jsonl and shows your running P&L: per-market breakdown,
realized profit/loss, current open positions, and a simulated account
balance (paper mode) so you can track wins and losses over time without
the numbers resetting every time you restart the bot.

Usage:
    python show_pnl.py

The accounting lives in pnl.py (shared with the dashboard). Open positions
in markets that have since resolved are booked at their settlement value.
STARTING_BALANCE_USD in pnl.py is the hypothetical paper bankroll.
"""
import ledger
import pnl
from config import CONFIG
from kalshi_client import build_client


def main():
    fills = ledger.read_all_fills()
    if not fills:
        print("No fills recorded yet. This is expected if the bot hasn't")
        print("simulated or executed any fills since you started using the ledger.")
        print(f"(Looking for: {ledger.LEDGER_PATH})")
        return

    print("Checking settlement status for open positions...\n")
    summary = pnl.compute(client=build_client(CONFIG), fills=fills)

    print(f"{'TICKER':<35} {'FILLS':>6} {'NET POS':>8} {'AVG COST':>9} {'REALIZED':>10} {'FEES':>7}  NOTE")
    print("-" * 100)
    for ml in summary["markets"]:
        print(f"{ml.ticker:<35} {ml.fill_count:>6} {ml.net_contracts:>8} "
              f"{ml.avg_cost_cents:>8.1f}¢ {ml.realized_pnl_cents / 100:>+9.2f}$ "
              f"{ml.total_fees_cents / 100:>6.2f}$  {ml.settlement_note}")

    print("-" * 100)
    print(f"Total realized P&L (before fees):  ${summary['realized_usd']:+.2f}")
    print(f"Total estimated fees:               ${summary['fees_usd']:.2f}")
    print(f"Net realized P&L (after fees):      ${summary['net_usd']:+.2f}")
    print(f"Starting balance:                   ${summary['starting_balance_usd']:.2f}")
    print(f"Simulated current balance:          ${summary['balance_usd']:.2f}")
    print(f"\nTotal fills recorded: {summary['fill_count']}")
    still_open = sum(1 for ml in summary["markets"] if ml.net_contracts != 0)
    if still_open:
        print(f"\n{still_open} position(s) still open (NET POS != 0) and NOT yet settled —")
        print("those aren't marked to a current price here, so P&L on them is still")
        print("incomplete until the market resolves or you close the position yourself.")
    print("\nFees use Kalshi's quadratic formula (fees.py) at the taker rate for every")
    print("fill — verify the maker rate on kalshi.com/fee-schedule.")


if __name__ == "__main__":
    main()
