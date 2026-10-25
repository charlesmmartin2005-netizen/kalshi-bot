# Installs the Kalshi bot as a desktop app for this Windows user:
# - "Kalshi bot" shortcuts on the Desktop and in the Start menu. They run
#   open_app.py, which opens the dashboard in its own window and starts the
#   bot first if it isn't running.
# - The "Kalshi bot" scheduled task, which runs run-bot.ps1 at every logon.
#   It runs in a console with no window. With Windows Terminal as the default
#   terminal, "powershell -WindowStyle Hidden" still shows a terminal window,
#   and closing that window stopped the bot (2026-10-22).
# Safe to run again. -Uninstall stops the bot and removes both.
#
#   powershell -ExecutionPolicy Bypass -File install-app.ps1
#   powershell -ExecutionPolicy Bypass -File install-app.ps1 -Uninstall
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$task = "Kalshi bot"
$shell = New-Object -ComObject WScript.Shell
$links = @(
    (Join-Path $shell.SpecialFolders("Desktop") "Kalshi bot.lnk"),
    (Join-Path $shell.SpecialFolders("Programs") "Kalshi bot.lnk")
)

if ($Uninstall) {
    & (Join-Path $root "stop-bot.ps1")
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    $links | Where-Object { Test-Path $_ } | ForEach-Object { Remove-Item $_ }
    "Removed the shortcuts and the '$task' task."
    return
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "No $pythonw. Create the venv first (README, Run it)." }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$root\run-bot.ps1`"" `
    -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $task -Force `
    -Description "Keeps the Kalshi paper bot running (run-bot.ps1, no window). Stop with stop-bot.ps1." `
    -Action $action -Settings $settings `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
    -Principal (New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited) | Out-Null

foreach ($path in $links) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$root\open_app.py`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$root\static\icon.ico,0"
    $lnk.Description = "Kalshi bot dashboard"
    $lnk.Save()
}
"Installed the '$task' task and shortcuts:"
$links
