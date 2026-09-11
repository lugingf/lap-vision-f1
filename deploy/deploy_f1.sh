#!/usr/bin/env bash

set -Eeuo pipefail

# Deploys the FastF1 service as a blue-green pair from a prebuilt image in GHCR.
#
# The image is built in CI. Building here meant resolving pandas and numpy wheels on the
# production host on every deploy, which is the heaviest build of the four services and the one
# least worth doing next to live containers.
#
# The service has no public entry point: lap_vision reaches it by container name over the shared
# docker network, so the router in front of it publishes no port.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_ROOT="${APP_ROOT:-/opt/lap-vision}"
NETWORK_NAME="${NETWORK_NAME:-lapvision_net}"
APP_NAME="lapvision-f1"
ROUTER_NAME="${ROUTER_NAME:-lapvision-f1-router}"
IMAGE="${IMAGE:?IMAGE is required}"
TARGET_PORT=8010
# One port per proxy source (laptop, phone, a residential proxy, ...) - comma-separated.
PROXY_TUNNEL_PORTS="${PROXY_TUNNEL_PORTS:-${PROXY_TUNNEL_PORT:-1080}}"

GHCR_USERNAME="${GHCR_USERNAME:-}"
GHCR_TOKEN="${GHCR_TOKEN:-}"

log() {
  printf '[deploy] %s\n' "$*"
}
FASTF1_CACHE_CONTAINER_DIR="/var/lib/lap-vision-f1/fastf1-cache"
DATA_CACHE_CONTAINER_DIR="/var/lib/lap-vision-f1/data-cache"

required_vars=(
  APP_ROOT
  LAP_VISION_F1_INTERNAL_TOKEN
)

for var in "${required_vars[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "missing required env: ${var}" >&2
    exit 1
  fi
done

LAP_VISION_F1_WORKERS="${LAP_VISION_F1_WORKERS:-2}"

mkdir -p \
  "${APP_ROOT}/shared/f1-proxy/conf.d" \
  "${APP_ROOT}/shared/f1-cache/fastf1" \
  "${APP_ROOT}/shared/f1-cache/data" \
  "${APP_ROOT}/f1"

docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1 || docker network create "${NETWORK_NAME}" >/dev/null

# The container reaches the SSH-forwarded SOCKS proxies (see `make tunnel` / f1-proxy-relay.sh)
# via socat relays bound to the docker bridge gateway IP on the host. ufw's default-deny INPUT
# chain blocks that hop even though it never leaves the host, so open each port narrowly to this
# network's own subnet.
network_subnet="$(docker network inspect -f '{{(index .IPAM.Config 0).Subnet}}' "${NETWORK_NAME}" 2>/dev/null || true)"
if [[ -n "${network_subnet}" ]] && command -v ufw >/dev/null 2>&1 && sudo -n ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw_status="$(sudo -n ufw status)"
  IFS=',' read -ra proxy_tunnel_ports <<< "${PROXY_TUNNEL_PORTS//[[:space:]]/}"
  for proxy_tunnel_port in "${proxy_tunnel_ports[@]}"; do
    [[ -n "${proxy_tunnel_port}" ]] || continue
    if ! grep -qE "^${proxy_tunnel_port}/tcp[[:space:]]+ALLOW[[:space:]]+${network_subnet}" <<< "${ufw_status}"; then
      log "opening ufw port ${proxy_tunnel_port}/tcp for ${network_subnet} (f1 outbound proxy tunnel)"
      sudo -n ufw allow from "${network_subnet}" to any port "${proxy_tunnel_port}" proto tcp comment 'lap-vision-f1 proxy tunnel' >/dev/null
    fi
  done
fi

# Outbound proxy relay: fully provisioned here so nothing needs to be done by hand on the host.
# Regenerates the systemd unit from the current PROXY_TUNNEL_PORTS/APP_ROOT on every deploy;
# deploy/f1-proxy-relay.service in the repo is a static copy for reference / manual install only.
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  if ! command -v socat >/dev/null 2>&1; then
    log "installing socat (outbound proxy relay)"
    sudo -n apt-get update -qq && sudo -n apt-get install -y -qq socat
  fi

  relay_env_file="${APP_ROOT}/f1-proxy-relay.env"
  relay_env_tmp="$(mktemp)"
  printf 'LAP_VISION_F1_INTERNAL_TOKEN=%s\n' "${LAP_VISION_F1_INTERNAL_TOKEN}" > "${relay_env_tmp}"
  relay_changed=0
  if ! sudo -n cmp -s "${relay_env_tmp}" "${relay_env_file}" 2>/dev/null; then
    sudo -n install -m 600 "${relay_env_tmp}" "${relay_env_file}"
    relay_changed=1
  fi
  rm -f "${relay_env_tmp}"

  relay_unit_tmp="$(mktemp)"
  cat > "${relay_unit_tmp}" <<EOF
[Unit]
Description=lap-vision-f1 outbound proxy relay (socat, docker bridge -> loopback)
After=docker.service
Requires=docker.service

[Service]
Type=simple
Environment=PROXY_TUNNEL_PORTS=${PROXY_TUNNEL_PORTS}
Environment=NETWORK_NAME=${NETWORK_NAME}
Environment=APP_ROOT=${APP_ROOT}
EnvironmentFile=-${relay_env_file}
WorkingDirectory=${APP_ROOT}/f1
ExecStart=/usr/bin/bash ${APP_ROOT}/f1/deploy/f1-proxy-relay.sh
Restart=on-failure
RestartSec=5
User=$(id -un)

[Install]
WantedBy=multi-user.target
EOF
  relay_unit_path="/etc/systemd/system/f1-proxy-relay.service"
  if ! sudo -n cmp -s "${relay_unit_tmp}" "${relay_unit_path}" 2>/dev/null; then
    log "installing f1-proxy-relay systemd unit"
    sudo -n cp "${relay_unit_tmp}" "${relay_unit_path}"
    sudo -n systemctl daemon-reload
    relay_changed=1
  fi
  rm -f "${relay_unit_tmp}"

  sudo -n systemctl enable f1-proxy-relay >/dev/null 2>&1 || true
  if [[ "${relay_changed}" -eq 1 ]] || ! sudo -n systemctl is-active --quiet f1-proxy-relay; then
    log "(re)starting f1-proxy-relay"
    sudo -n systemctl restart f1-proxy-relay
  fi
else
  log "no passwordless sudo available; skipping f1-proxy-relay systemd setup (see deploy/f1-proxy-relay.service)"
fi

if [[ -n "${GHCR_USERNAME}" && -n "${GHCR_TOKEN}" ]]; then
  log "login to ghcr.io"
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USERNAME}" --password-stdin
fi

log "pull image ${IMAGE}"
docker pull "${IMAGE}"

active_color="green"
active_file="${APP_ROOT}/shared/f1-proxy/conf.d/f1_active.conf"
if [[ -f "${active_file}" ]]; then
  if grep -q "${APP_NAME}-blue" "${active_file}"; then
    active_color="blue"
  fi
fi

if [[ "${active_color}" == "blue" ]]; then
  next_color="green"
else
  next_color="blue"
fi

container_name="${APP_NAME}-${next_color}"
log "start candidate container ${container_name}"
docker rm -f "${container_name}" >/dev/null 2>&1 || true
docker run -d \
  --name "${container_name}" \
  --restart unless-stopped \
  --network "${NETWORK_NAME}" \
  -e LAP_VISION_F1_HOST="0.0.0.0" \
  -e LAP_VISION_F1_PORT="${TARGET_PORT}" \
  -e LAP_VISION_F1_WORKERS="${LAP_VISION_F1_WORKERS}" \
  -e LAP_VISION_F1_INTERNAL_TOKEN="${LAP_VISION_F1_INTERNAL_TOKEN}" \
  -e LAP_VISION_F1_FASTF1_CACHE_DIR="${FASTF1_CACHE_CONTAINER_DIR}" \
  -e LAP_VISION_F1_DATA_CACHE_DIR="${DATA_CACHE_CONTAINER_DIR}" \
  -v "${APP_ROOT}/shared/f1-cache/fastf1:${FASTF1_CACHE_CONTAINER_DIR}" \
  -v "${APP_ROOT}/shared/f1-cache/data:${DATA_CACHE_CONTAINER_DIR}" \
  "${IMAGE}" >/dev/null

for _ in $(seq 1 40); do
  health_status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "${container_name}" 2>/dev/null || true)"
  if [[ "${health_status}" == "healthy" ]]; then
    break
  fi
  sleep 3
done

health_status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "${container_name}")"
if [[ "${health_status}" != "healthy" ]]; then
  docker logs "${container_name}" || true
  echo "lap-vision-f1 container did not become healthy" >&2
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  exit 1
fi

cat > "${active_file}" <<EOF
upstream f1_upstream {
    server ${APP_NAME}-${next_color}:${TARGET_PORT};
}
EOF

if [[ ! -f "${APP_ROOT}/shared/f1-proxy/nginx.conf" ]]; then
  cp "${SCRIPT_DIR}/nginx.internal.conf" "${APP_ROOT}/shared/f1-proxy/nginx.conf"
fi

if docker ps --format '{{.Names}}' | grep -qx "${ROUTER_NAME}"; then
  docker exec "${ROUTER_NAME}" nginx -s reload >/dev/null
else
  docker rm -f "${ROUTER_NAME}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${ROUTER_NAME}" \
    --restart unless-stopped \
    --network "${NETWORK_NAME}" \
    -v "${APP_ROOT}/shared/f1-proxy/nginx.conf:/etc/nginx/nginx.conf:ro" \
    -v "${APP_ROOT}/shared/f1-proxy/conf.d:/etc/nginx/conf.d:ro" \
    nginx:1.29-alpine >/dev/null
fi

old_container="${APP_NAME}-${active_color}"
if [[ "${old_container}" != "${container_name}" ]]; then
  docker rm -f "${old_container}" >/dev/null 2>&1 || true
fi

log "lap-vision-f1 deployed: ${next_color}"
docker image prune -af || true
