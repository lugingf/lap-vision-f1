<#
.SYNOPSIS
    Windows equivalent of `make tunnel`: runs a local SOCKS5 proxy and reverse-tunnels it to the
    lap-vision-f1 server, so the server can reach the internet through this machine's own IP.

.DESCRIPTION
    Requires:
      - Python 3 on PATH (used to run pproxy, a pure-Python SOCKS5 server - no compiled
        binary needed on Windows). Installed automatically via `pip install --user pproxy`
        if missing.
      - OpenSSH client (`ssh`), built into Windows 10 1809+ / Windows 11 by default.

    After this is running, on the server run `bash deploy/f1-proxy-relay.sh` and set the
    printed socks5h:// address via PUT /v1/admin/proxy (or the lapvision_fe admin page).

.PARAMETER TunnelUser
    SSH user on the server. Default: ubuntu

.PARAMETER TunnelHost
    Server hostname/IP. Default: 95.179.154.60

.PARAMETER TunnelPort
    SSH port on the server. Default: 22

.PARAMETER SocksPort
    Local SOCKS5 port, and the port forwarded to the server's loopback. Must match
    PROXY_TUNNEL_PORT used by deploy_f1.sh / f1-proxy-relay.sh on the server. Default: 1080

.EXAMPLE
    ./deploy/tunnel.ps1

.EXAMPLE
    ./deploy/tunnel.ps1 -TunnelHost 95.179.154.60 -SocksPort 1080
#>
param(
    [string]$TunnelUser = "ubuntu",
    [string]$TunnelHost = "95.179.154.60",
    [int]$TunnelPort = 22,
    [int]$SocksPort = 1080
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python not found on PATH. Install Python 3, or use WSL and run 'make tunnel' there instead."
    exit 1
}

python -c "import pproxy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing pproxy (pure-Python SOCKS5 server)..."
    python -m pip install --user pproxy
}

Write-Host "Starting local SOCKS5 proxy on 127.0.0.1:$SocksPort ..."
$proxyProcess = Start-Process -FilePath "python" `
    -ArgumentList "-m", "pproxy", "-l", "socks5://127.0.0.1:$SocksPort" `
    -PassThru -WindowStyle Hidden

if (-not $proxyProcess -or $proxyProcess.HasExited) {
    Write-Error "Failed to start the local SOCKS5 proxy."
    exit 1
}

try {
    Start-Sleep -Seconds 1
    Write-Host "Reverse-tunneling it to $TunnelUser@${TunnelHost}:$SocksPort (Ctrl+C to stop) ..."
    ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=3 `
        -R "127.0.0.1:${SocksPort}:localhost:${SocksPort}" `
        -p $TunnelPort -N "$TunnelUser@$TunnelHost"
}
finally {
    Write-Host "Stopping local SOCKS5 proxy..."
    Stop-Process -Id $proxyProcess.Id -Force -ErrorAction SilentlyContinue
}
