# Copies the bot to the Lightsail server and restarts it there (README, "Run on
# a server"). Sends the code as it is on disk now, uncommitted edits included:
# every file git tracks or would track, so never .env, logs or the fills log.
# Then setup-server.sh installs packages and restarts the kalshi-bot service.
#
# -Fills also sends fills_log.jsonl, which carries this PC's open positions
# over. It replaces the server's own fills log, so use it only on the first
# deploy.
param([switch]$Fills)
Set-Location $PSScriptRoot
$server = "kalshi-bot"   # the Host entry in ~\.ssh\config

function Check($step) { if ($LASTEXITCODE) { throw "$step failed (exit $LASTEXITCODE)" } }

ssh $server "mkdir -p kalshi-bot"; Check "ssh"
# cmd's pipe passes tar's bytes through untouched; PowerShell 5.1's doesn't.
cmd /c "git ls-files --cached --others --exclude-standard | tar -cf - -T - | ssh $server tar -xf - -C kalshi-bot"; Check "copy"
if ($Fills) { scp -q fills_log.jsonl "${server}:kalshi-bot/"; Check "fills log copy" }
ssh $server "bash kalshi-bot/setup-server.sh"; Check "setup"
