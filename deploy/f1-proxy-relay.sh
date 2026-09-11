#!/usr/bin/env bash

set -Eeuo pipefail

# Relays traffic from the docker bridge gateway IP (reachable from the f1 container) to
# 127.0.0.1:<port> on the host, one relay per port in PROXY_TUNNEL_PORTS. Each port is where an
# SSH reverse tunnel from one proxy source (laptop, phone, a residential proxy, ...) lands - see
# `make tunnel` / `deploy/tunnel.ps1`. deploy_f1.sh already opens these ports in ufw for the
# docker network's subnet.
#
# On startup this also registers the resulting socks5h:// addresses with lap-vision-f1 via
# PUT /v1/admin/proxy (replacing whatever was set before) - this script is the source of truth
# for the proxy list, not the admin UI. Requires LAP_VISION_F1_INTERNAL_TOKEN; without it,
# registration is skipped and the addresses are only logged for manual entry.
#
# Usage (on the server, meant to run continuously - see f1-proxy-relay.service):
#   PROXY_TUNNEL_PORTS=1080,1081,1082 LAP_VISION_F1_INTERNAL_TOKEN=... bash deploy/f1-proxy-relay.sh

APP_ROOT="${APP_ROOT:-/opt/lap-vision}"
NETWORK_NAME="${NETWORK_NAME:-lapvision_net}"
APP_NAME="lapvision-f1"
PROXY_TUNNEL_PORTS="${PROXY_TUNNEL_PORTS:-${PROXY_TUNNEL_PORT:-1080}}"

command -v socat >/dev/null 2>&1 || {
  echo "socat not found. Install it with: sudo apt-get install -y socat" >&2
  exit 1
}

gateway="$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "${NETWORK_NAME}" 2>/dev/null || true)"
if [[ -z "${gateway}" ]]; then
  echo "could not determine gateway IP for docker network ${NETWORK_NAME}" >&2
  exit 1
fi

IFS=',' read -ra ports <<< "${PROXY_TUNNEL_PORTS//[[:space:]]/}"
if [[ ${#ports[@]} -eq 0 ]]; then
  echo "PROXY_TUNNEL_PORTS is empty" >&2
  exit 1
fi

register_proxies() {
  if [[ -z "${LAP_VISION_F1_INTERNAL_TOKEN:-}" ]]; then
    echo "[f1-proxy-relay] LAP_VISION_F1_INTERNAL_TOKEN not set, skipping proxy registration" >&2
    return
  fi

  local active_file="${APP_ROOT}/shared/f1-proxy/conf.d/f1_active.conf"
  local container="${APP_NAME}-blue"
  if [[ -f "${active_file}" ]] && grep -q "${APP_NAME}-green" "${active_file}"; then
    container="${APP_NAME}-green"
  fi

  if ! docker ps --format '{{.Names}}' | grep -qx "${container}"; then
    echo "[f1-proxy-relay] active container ${container} not running, skipping proxy registration" >&2
    return
  fi

  local proxies_json="["
  local first=1
  for port in "${ports[@]}"; do
    [[ ${first} -eq 1 ]] || proxies_json+=","
    proxies_json+="\"socks5h://${gateway}:${port}\""
    first=0
  done
  proxies_json+="]"

  echo "[f1-proxy-relay] registering proxies with ${container}: ${proxies_json}"
  if ! docker exec \
    -e LVF1_TOKEN="${LAP_VISION_F1_INTERNAL_TOKEN}" \
    -e LVF1_PROXIES="${proxies_json}" \
    "${container}" python3 -c '
import json, os, requests
requests.put(
    "http://127.0.0.1:8010/v1/admin/proxy",
    json={"https_proxies": json.loads(os.environ["LVF1_PROXIES"])},
    headers={"X-Internal-Token": os.environ["LVF1_TOKEN"]},
    timeout=5,
).raise_for_status()
'; then
    echo "[f1-proxy-relay] WARNING: failed to register proxies with ${container}" >&2
  fi
}

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

for port in "${ports[@]}"; do
  echo "[f1-proxy-relay] relaying ${gateway}:${port} -> 127.0.0.1:${port}"
  echo "[f1-proxy-relay] proxy address: socks5h://${gateway}:${port}"
  socat "TCP-LISTEN:${port},bind=${gateway},reuseaddr,fork" "TCP:127.0.0.1:${port}" &
  pids+=("$!")
done

register_proxies

echo "[f1-proxy-relay] ${#ports[@]} relay(s) running, Ctrl+C to stop"
wait -n "${pids[@]}"
