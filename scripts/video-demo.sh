#!/usr/bin/env bash
# IntentQoS without a router, on any Ubuntu/Debian Linux machine (or WSL): one command for the whole lab and the
# 80 s video demo.
#
#   sudo bash scripts/video-demo.sh          start everything and open the three windows; the demo plays by itself
#   sudo bash scripts/video-demo.sh stop     stop everything
#
# What runs: an emulated home network (Linux network namespaces: laptop, TVs, console, NAS, phone, a bloated
# 100/20 Mbit/s fibre modem), the real IntentQoS engine, watchdog and agent on its gateway, and the operator
# controller with simulated homes. Nothing is installed with pip (the code uses the Python standard library).
set -euo pipefail
cd "$(dirname "$0")/.."
say() { echo "[intentqos] $*"; }

if [ "$(id -u)" != 0 ]; then
  exec sudo -E bash "$0" "$@"
fi
if [ "${1:-}" = stop ]; then
  bash scripts/stack.sh down all
  say "stopped"
  exit 0
fi

# --- the few system tools the lab needs (one time) ----------------------------------------------
need=()
command -v ip > /dev/null || need+=(iproute2)
command -v tc > /dev/null || need+=(iproute2)
command -v nft > /dev/null || need+=(nftables)
command -v openssl > /dev/null || need+=(openssl)
command -v python3 > /dev/null || need+=(python3)
if [ ${#need[@]} -gt 0 ]; then
  say "installing ${need[*]} (one time, needs internet)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq "${need[@]}" > /dev/null
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  if ! command -v python3.11 > /dev/null; then
    say "installing python3.11 (one time, needs internet)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq python3.11 > /dev/null
  fi
  echo python3.11 > .python
fi
case "$(uname -m)" in
  x86_64) [ -x router/iq-capture ] || cp router/bin/iq-capture.x86_64 router/iq-capture ;;
  aarch64) [ -x router/iq-capture ] || cp router/bin/iq-capture.aarch64 router/iq-capture ;;
esac
chmod +x router/iq-capture scripts/*.sh 2>/dev/null || true

# --- start and wait ---------------------------------------------------------------------------------
say "starting the lab (emulated home + IntentQoS engine) and the operator controller"
bash scripts/stack.sh up all
for i in $(seq 1 90); do
  if curl -s -m 2 http://127.0.0.1:8090/api/healthz > /dev/null && curl -s -m 2 http://127.0.0.1:8088/healthz > /dev/null; then
    break
  fi
  sleep 2
done
bash scripts/stack.sh status

# --- the windows (as the desktop user, not root) ------------------------------------------------------
open_url() {
  local u=${SUDO_USER:-}
  if [ -n "$u" ] && command -v xdg-open > /dev/null; then
    sudo -u "$u" DISPLAY="${DISPLAY:-:0}" XDG_RUNTIME_DIR="/run/user/$(id -u "$u")" xdg-open "$1" > /dev/null 2>&1 &
  elif command -v xdg-open > /dev/null; then
    xdg-open "$1" > /dev/null 2>&1 &
  fi
}
open_url "http://localhost:8088/ops/"
sleep 1
open_url "http://localhost:8088/app/"
sleep 1
open_url "http://localhost:8090/#autoplay"
cat <<EOF

[intentqos] ready
  Gateway console (the demo plays here by itself): http://localhost:8090/#autoplay
  Operator console:                                http://localhost:8088/ops/   (lab sign-in buttons)
  Household app:                                   http://localhost:8088/app/   (lab sign-in button)
  Play the demo again: the console's Experiments page -> "Play the 80 s demo", or reload the #autoplay address.
  Stop everything:     sudo bash scripts/video-demo.sh stop
EOF
