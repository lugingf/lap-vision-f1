#!/usr/bin/env bash

set -Euo pipefail

# One health check pass, run by f1-tunnel-watchdog.timer every 2 minutes. Failure counters live in
# /run (tmpfs), so they reset on reboot. Escalation:
#   gateway unreachable  x2 -> Wi-Fi off/on, x4 -> restart the network stack, x8 -> reboot
#   local SOCKS broken while the internet works        -> restart f1-socks
#   end-to-end check through the VPS fails x2          -> restart f1-tunnel
#   / or /var/log (Armbian's RAM log disk) >= 85% full -> drop rotated logs, vacuum the journal,
#                                                         clean the apt cache
# The end-to-end check asks the VPS to fetch api.ipify.org through 127.0.0.1:SOCKS_PORT, i.e.
# exactly the path lap-vision-f1 uses. A VPS outage never reboots the board - only a local
# network failure does.

WIFI_RESET_AFTER="${WIFI_RESET_AFTER:-2}"
NETWORK_RESTART_AFTER="${NETWORK_RESTART_AFTER:-4}"
REBOOT_AFTER="${REBOOT_AFTER:-8}"
TUNNEL_RESTART_AFTER="${TUNNEL_RESTART_AFTER:-2}"
DISK_CLEAN_AT_PERCENT="${DISK_CLEAN_AT_PERCENT:-85}"
STATE="/run/f1-tunnel-watchdog"
IP_CHECK_URL="https://api.ipify.org"

mkdir -p "${STATE}"

log() {
  printf '[watchdog] %s\n' "$*"
}

counter() {
  cat "${STATE}/$1" 2>/dev/null || echo 0
}

bump() {
  local value=$(( $(counter "$1") + 1 ))
  echo "${value}" > "${STATE}/$1"
  echo "${value}"
}

clear_counter() {
  rm -f "${STATE}/$1"
}

network_ok() {
  local gateway
  gateway="$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')"
  if [[ -n "${gateway}" ]] && ping -c 3 -W 2 -q "${gateway}" >/dev/null 2>&1; then
    return 0
  fi
  # Some routers drop ICMP - reaching the internet counts as a working network too.
  curl -s --max-time 10 -o /dev/null "${IP_CHECK_URL}"
}

reset_wifi() {
  if command -v nmcli >/dev/null 2>&1; then
    nmcli radio wifi off
    sleep 5
    nmcli radio wifi on
  else
    ip link set "${WIFI_IFACE}" down
    sleep 5
    ip link set "${WIFI_IFACE}" up
  fi
}

clean_disk_if_full() {
  local mount used
  for mount in / /var/log; do
    used="$(df --output=pcent "${mount}" 2>/dev/null | tail -1 | tr -dc '0-9')"
    [[ -n "${used}" ]] && (( used >= DISK_CLEAN_AT_PERCENT )) || continue
    log "${mount} is ${used}% full - cleaning old logs and the apt cache"
    # /var/log.hdd is where Armbian's ramlog persists /var/log on the SD card.
    find /var/log /var/log.hdd -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete 2>/dev/null
    find /var/log.hdd -type f -mtime +30 -delete 2>/dev/null
    journalctl --vacuum-size=10M >/dev/null 2>&1
    apt-get clean 2>/dev/null
    return
  done
}

clean_disk_if_full

if ! network_ok; then
  failures="$(bump network)"
  log "network down (${failures} in a row)"
  if (( failures >= REBOOT_AFTER )); then
    log "rebooting"
    systemctl reboot
  elif (( failures == NETWORK_RESTART_AFTER )); then
    log "restarting the network stack"
    if command -v nmcli >/dev/null 2>&1; then
      systemctl restart NetworkManager || true
    else
      # netplan + networkd: regenerates and restarts networkd and the wpa_supplicant for Wi-Fi.
      netplan apply || true
    fi
  elif (( failures == WIFI_RESET_AFTER )); then
    log "resetting Wi-Fi"
    reset_wifi
  fi
  exit 0
fi
clear_counter network

direct_ip="$(curl -s --max-time 10 "${IP_CHECK_URL}" || true)"
if [[ -z "${direct_ip}" ]]; then
  log "LAN is up but the internet is not (ISP outage?) - nothing to restart"
  exit 0
fi

if ! curl -s --max-time 15 -o /dev/null --socks5-hostname "127.0.0.1:${SOCKS_PORT}" "${IP_CHECK_URL}"; then
  log "local SOCKS on 127.0.0.1:${SOCKS_PORT} broken - restarting f1-socks"
  systemctl restart f1-socks.service
  exit 0
fi

# With the restricted key the VPS runs its forced command (the same curl) and ignores this one.
vps_ip="$(timeout 45 runuser -u f1tunnel -- ssh -F /etc/f1-tunnel/ssh_config vps \
  "curl -sS --max-time 15 --socks5-hostname 127.0.0.1:${SOCKS_PORT} ${IP_CHECK_URL}" </dev/null 2>/dev/null || true)"
if [[ "${vps_ip}" =~ ^[0-9a-fA-F:.]+$ ]]; then
  clear_counter tunnel
  log "ok: VPS exits via ${vps_ip} (direct ${direct_ip})"
  exit 0
fi

failures="$(bump tunnel)"
log "end-to-end check through the VPS failed (${failures} in a row)"
if (( failures >= TUNNEL_RESTART_AFTER )); then
  log "restarting f1-tunnel"
  systemctl restart f1-tunnel.service
  clear_counter tunnel
fi
