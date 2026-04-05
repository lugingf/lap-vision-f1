#!/usr/bin/env bash

set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/lap-vision}"
NETWORK_NAME="${NETWORK_NAME:-lapvision_net}"
APP_NAME="lapvision-f1"
ROUTER_NAME="${ROUTER_NAME:-lapvision-f1-router}"
IMAGE_NAME="${IMAGE_NAME:-lapvision/f1}"
TARGET_PORT=8010
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

release_dir="${APP_ROOT}/f1/${next_color}"
rm -rf "${release_dir}"
mkdir -p "${release_dir}"

cp Dockerfile "${release_dir}/Dockerfile"
cp pyproject.toml "${release_dir}/pyproject.toml"
cp README.md "${release_dir}/README.md"
cp .dockerignore "${release_dir}/.dockerignore"
cp -R app deploy tests "${release_dir}/"

docker build -t "${IMAGE_NAME}:${next_color}" "${release_dir}"

container_name="${APP_NAME}-${next_color}"
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
  "${IMAGE_NAME}:${next_color}" >/dev/null

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
  exit 1
fi

cat > "${active_file}" <<EOF
upstream f1_upstream {
    server ${APP_NAME}-${next_color}:${TARGET_PORT};
}
EOF

if [[ ! -f "${APP_ROOT}/shared/f1-proxy/nginx.conf" ]]; then
  cp deploy/nginx.internal.conf "${APP_ROOT}/shared/f1-proxy/nginx.conf"
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

echo "lap-vision-f1 deployed: ${next_color}"
