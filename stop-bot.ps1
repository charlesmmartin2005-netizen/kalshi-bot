# Stops the unattended bot (run-bot.ps1 / the "Kalshi bot" task): ends the
# restart loop first so nothing relaunches, then stops trading through the
# dashboard (cancels quotes) and closes it. It starts again at the next
# logon, or with: Start-ScheduledTask -TaskName "Kalshi bot"
Stop-ScheduledTask -TaskName "Kalshi bot" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -match 'run-bot\.ps1' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8050/api/stop -TimeoutSec 20 | Out-Null } catch {}
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -match 'dashboard\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
"Bot stopped."
