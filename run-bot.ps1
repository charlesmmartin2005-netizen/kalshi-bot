# Keeps the bot running unattended: starts the dashboard with trading on (no
# browser tab) and starts it again 10 seconds after it exits for any reason.
# The "Kalshi bot" scheduled task runs this hidden at logon (README, "Run
# unattended"); stop-bot.ps1 stops it. The dashboard stays at
# http://127.0.0.1:8050.
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force logs | Out-Null

function Note($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -Append -Encoding utf8 logs\launcher.log
}

while ($true) {
    Note "starting dashboard"
    # The bot logs to logs\bot.log; whatever Python prints to stderr (a crash
    # traceback) goes to logs\crash.log.
    cmd /c ".venv\Scripts\python.exe dashboard.py --autostart --no-browser >nul 2>>logs\crash.log"
    Note "dashboard exited (code $LASTEXITCODE); restarting in 10s"
    Start-Sleep -Seconds 10
}
