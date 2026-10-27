#!/bin/bash
# Sets up the bot on the Lightsail server, or refreshes it: Python 3.15 in
# .venv, the packages in requirements.txt, and the kalshi-bot service, which is
# then (re)started. deploy-server.ps1 runs this after every copy, so each step
# is safe to repeat.
set -euo pipefail
cd "$(dirname "$0")"

# Log and dashboard times match the PC's. The daily-loss reset is UTC anyway.
sudo timedatectl set-timezone America/Chicago

# The plan has 1 GB of memory and no swap; the Scanner's sweep process
# briefly holds the whole catalog.
if [ ! -f /swapfile ]; then
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# The same Python as the PC's .venv (Ubuntu 24.04 ships 3.12), via uv.
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -x .venv/bin/python ] || uv venv --python 3.15 .venv
uv pip install --quiet --python .venv/bin/python -r requirements.txt

mkdir -p logs
sudo cp kalshi-bot.service /etc/systemd/system/kalshi-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --quiet kalshi-bot
sudo systemctl restart kalshi-bot
echo "kalshi-bot: $(systemctl is-active kalshi-bot)"
