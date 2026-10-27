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
# -Server makes the "Kalshi Bot Pro" shortcuts instead, for the bot on the
# server (README, "Run on a server"): they run open_app.py --server, which
# opens that bot's dashboard through an SSH tunnel. No task: the server keeps
# its own bot running. -Server -Uninstall removes just those shortcuts.
#
#   powershell -ExecutionPolicy Bypass -File install-app.ps1
#   powershell -ExecutionPolicy Bypass -File install-app.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File install-app.ps1 -Server
#   powershell -ExecutionPolicy Bypass -File install-app.ps1 -Server -Uninstall
param([switch]$Uninstall, [switch]$Server)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$task = "Kalshi bot"
$name = if ($Server) { "Kalshi Bot Pro" } else { "Kalshi bot" }
$shell = New-Object -ComObject WScript.Shell
$links = @(
    (Join-Path $shell.SpecialFolders("Desktop") "$name.lnk"),
    (Join-Path $shell.SpecialFolders("Programs") "$name.lnk")
)

if ($Uninstall) {
    if (-not $Server) {
        & (Join-Path $root "stop-bot.ps1")
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    }
    $links | Where-Object { Test-Path $_ } | ForEach-Object { Remove-Item $_ }
    if ($Server) { "Removed the '$name' shortcuts." } else { "Removed the shortcuts and the '$task' task." }
    return
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "No $pythonw. Create the venv first (README, Run it)." }

foreach ($path in $links) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = if ($Server) { "`"$root\open_app.py`" --server" } else { "`"$root\open_app.py`"" }
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$root\static\icon.ico,0"
    $lnk.Description = if ($Server) { "Kalshi Bot Pro: the server bot's dashboard" } else { "Kalshi bot dashboard" }
    $lnk.Save()
}
if ($Server) {
    "Installed the '$name' shortcuts:"
    $links
    return
}

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
"Installed the '$task' task and shortcuts:"
$links
