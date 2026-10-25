# Working in this repo

A paper-trading bot for Kalshi's BTC daily markets, with a local dashboard. It runs on the owner's Windows PC (`.venv\Scripts\python.exe`, PowerShell launchers). Start with [README.md](README.md): strategies, how to run it, the file table and the dated summaries of what testing showed. The evidence behind each strategy setting is in [research/README.md](research/README.md).

## Safety rules

- Paper trading is the default (`KALSHI_PAPER_TRADING=true`). Never turn it off, and never change code so that real orders can go out without that flag. The README's [Before going live](README.md#before-going-live) list is still open.
- Never read, print or commit `.env`, `*.pem` or `*.key`.
- Runtime data, not source; don't edit or commit: `fills_log*.jsonl` (the ledger; open positions are restored from it on restart), `arb_log.jsonl`, `halted.txt` (the saved kill switch; deleting it un-halts the bot), `logs/`, `research/cache*/`.
- Tunable numbers and risk limits live in `config.py`. Don't hard-code them in `strategy.py`, `market_maker.py` or `risk_manager.py`.

## How the code fits together

- `engine.py` is the main loop, shared by `main.py` (terminal) and `dashboard.py` (web, http://127.0.0.1:8050).
- `strategy.plan_quotes` is the quoting logic for the bot (`market_maker.py`) and for `simulate.py`, `backtest.py` and `research/strategy_lab.py`, so a change there changes the backtests too.
- Fees are in `fees.py`; makers pay no fee on KXBTCD/KXETHD.
- The README's Files table lists every module; update it when you add, move or rename one.

## Checking a change

There is no test suite. Before committing:

- `python -m py_compile *.py research/*.py`
- For a strategy change, compare against the live settings with `research/strategy_lab.py` or `backtest.py` before changing a default.
- Judge paper trading with `research/paper_markouts.py` (markouts net of fees), not daily P&L.

## Notes and docs

- The repo opens as an Obsidian vault: `.obsidian/app.json` is committed; `workspace.json` and plugin data stay local.
- Links are relative markdown links (`[research/README.md](research/README.md)`), never `[[wikilinks]]`, so they work on GitHub, in VS Code and in Obsidian.
- Each round of testing gets a dated section in `research/README.md` and a short "What testing showed (date)" summary in the README. Mark superseded results as superseded; don't delete them.
- A new note gets frontmatter (`title`, `date`, `tags: [a, b]`), a date-prefixed filename, a line in the README's Notes section, and a closing `Related:` line linking the notes it builds on.
- `archive/` holds the first scaffold's docs and scripts. It's history: don't follow its instructions.
- Commit messages: one plain sentence on what changed and why (see `git log`).
