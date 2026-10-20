"""
Terminal entry point. Run with:

    python main.py

For the web dashboard instead, run `python dashboard.py`.

Reads config from config.py / .env. Defaults to PAPER TRADING — you must
explicitly set KALSHI_PAPER_TRADING=false in your .env to send real orders.
"""
import logging
import sys

from config import CONFIG
from engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


def run():
    if not CONFIG.watched_tickers:
        log.error("No watched tickers — set KALSHI_WATCHED_TICKERS in .env "
                  "(run `python scanner.py` to find good ones).")
        sys.exit(1)
    Engine(CONFIG).run_forever()


if __name__ == "__main__":
    run()
