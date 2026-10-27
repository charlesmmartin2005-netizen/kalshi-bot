# Opens the server bot's dashboard on this PC, through an SSH tunnel: the
# server's 127.0.0.1:8050 appears here at http://127.0.0.1:8060 (8050 is this
# PC's own bot). The server's firewall only lets SSH in, so the tunnel is the
# only way to reach it. The tunnel runs with no window until you log off;
# running this again reuses it, or starts a new one if it dropped.
Set-Location $PSScriptRoot
$port = 8060

function Listening { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }

if (-not (Listening)) {
    Start-Process ssh -WindowStyle Hidden -ArgumentList "-N", "-o", "ExitOnForwardFailure=yes", "-L", "${port}:127.0.0.1:8050", "kalshi-bot"
    for ($i = 0; $i -lt 20 -and -not (Listening); $i++) { Start-Sleep -Milliseconds 500 }
}
Start-Process "http://127.0.0.1:$port"
