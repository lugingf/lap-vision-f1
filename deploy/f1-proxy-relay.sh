#!/usr/bin/env bash

set -Eeuo pipefail

# Relays traffic from the docker bridge gateway IP (reachable from the f1 container) to
# 127.0.0.1:PROXY_TUNNEL_PORT on the host, where `make tunnel` (run on your laptop) has an SSH
# reverse tunnel landing. deploy_f1.sh already opens PROXY_TUNNEL_PORT in ufw for the docker
# network's subnet, so this just needs to run while the laptop-side tunnel is up.
#
# Usage (on the server, after `make tunnel` is running locally):
#   bash deploy/f1-proxy-relay.sh
#
# Then point lap-vision-f1 at the printed socks5h:// address via PUT /v1/admin/proxy
# (or the lapvision_fe admin page).

NETWORK_NAME="${NETWORK_NAME:-lapvision_net}"
PROXY_TUNNEL_PORT="${PROXY_TUNNEL_PORT:-1080}"

command -v socat >/dev/null 2>&1 || {
  echo "socat not found. Install it with: sudo apt-get install -y socat" >&2
  exit 1
}

gateway="$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "${NETWORK_NAME}" 2>/dev/null || true)"
if [[ -z "${gateway}" ]]; then
  echo "could not determine gateway IP for docker network ${NETWORK_NAME}" >&2
  exit 1
fi

echo "[f1-proxy-relay] relaying ${gateway}:${PROXY_TUNNEL_PORT} -> 127.0.0.1:${PROXY_TUNNEL_PORT}"
echo "[f1-proxy-relay] set the f1 proxy to: socks5h://${gateway}:${PROXY_TUNNEL_PORT}"
echo "[f1-proxy-relay] Ctrl+C to stop"

exec socat "TCP-LISTEN:${PROXY_TUNNEL_PORT},bind=${gateway},reuseaddr,fork" "TCP:127.0.0.1:${PROXY_TUNNEL_PORT}"
