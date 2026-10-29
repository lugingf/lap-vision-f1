#!/usr/bin/env bash

set -Eeuo pipefail

# Reverse-tunnels the local microsocks to the VPS loopback (and, if ADMIN_PORT is set, this board's
# sshd too). Run by f1-tunnel.service: ssh exits on a dead connection (ServerAlive*) or on a port
# the VPS still holds from the previous connection (ExitOnForwardFailure), and systemd starts it
# again. Settings come from /etc/f1-tunnel/tunnel.env and /etc/f1-tunnel/ssh_config.

forwards=(-R "127.0.0.1:${SOCKS_PORT}:127.0.0.1:${SOCKS_PORT}")
if [[ -n "${ADMIN_PORT:-}" ]]; then
  forwards+=(-R "127.0.0.1:${ADMIN_PORT}:127.0.0.1:22")
fi

exec ssh -F /etc/f1-tunnel/ssh_config -NT "${forwards[@]}" vps
