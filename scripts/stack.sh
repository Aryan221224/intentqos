#!/usr/bin/env bash
# Bring up the IntentQoS stack in the lab (Linux or WSL2), or take it down.
#
#   sudo scripts/stack.sh up [all|controller|gateway]   start what is not running yet (default: all)
#   sudo scripts/stack.sh down [all|controller|gateway]
#   sudo scripts/stack.sh status                          one line per part: running / stopped
#   sudo scripts/stack.sh hold                             keep a WSL session open (used by the Windows launchers)
#   sudo scripts/stack.sh diag [controller|gateway]       status, log tails and kernel messages (shown by the launchers on errors)
#
#   controller = fleet controller + 240 simulated homes (operator console and subscriber app)
#   gateway    = emulated home network + the real engine, watchdog and agent (the lab home)
#
# Open:  http://localhost:8088/ops/   operator console (this computer only; https://localhost:8443/ops/ elsewhere)
#        http://localhost:8088/app/   subscriber app
#        http://localhost:8090/       the lab home's gateway console, with lab controls
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=results-lab/logs
mkdir -p "$LOG"
SIM=${SIM:-240}
[ -f .python ] && PYTHON=${PYTHON:-$(cat .python)}      # written by the Windows launcher's setup when python3 is too old
PY=${PYTHON:-python3}

running() { pgrep -f "$1" > /dev/null 2>&1; }

# spawn NAME LOGFILE CMD...: start a long-running service in the background, detached from this shell.
# Under systemd (Ubuntu on WSL enables it) a process started from a `wsl.exe` command lives in that
# command's session and is stopped when the command returns, even after setsid; so there it runs as a
# transient systemd unit (iq-NAME) instead. Elsewhere setsid is enough.
spawn() {
  local name=$1 log=$2
  shift 2
  if [ -d /run/systemd/system ] && command -v systemd-run > /dev/null 2>&1; then
    systemd-run --quiet --collect --unit="iq-$name-$(date +%s%N | cut -c1-13)" --working-directory="$PWD" \
      /bin/bash -c 'log=$1; shift; exec "$@" >> "$log" 2>&1 < /dev/null' iq "$log" "$@"
  else
    setsid "$@" >> "$log" 2>&1 < /dev/null &
  fi
}

up_controller() {
  if [ ! -f controller/data/pki/server.pem ]; then
    "$PY" -m controller.pki --dir controller/data/pki --ip 10.255.0.1 --ip 127.0.0.1 --dns localhost --dns controller.lab
  fi
  if [ ! -f controller/data/controller.db ]; then
    "$PY" -m controller.seed --sim "$SIM"
  fi
  if ! running "controller[.]server"; then
    spawn controller "$PWD/$LOG/controller.log" "$PY" -m controller.server --config controller/controller.toml
    sleep 2
  fi
  if ! running "controller[.]simfleet"; then
    spawn simfleet "$PWD/$LOG/simfleet.log" "$PY" -m controller.simfleet --config controller/controller.toml --story
  fi
}

up_gateway() {
  if ! running "testbed[.]labd"; then
    "$PY" testbed/testbed.py down > /dev/null 2>&1 || true
    spawn labd "$PWD/$LOG/labd.log" "$PY" -m testbed.labd --agent --presets call,browse
  fi
}

# gone PATTERN: wait up to 15 s for matching processes to exit, then kill what is left.
gone() {
  for _ in $(seq 30); do running "$1" || return 0; sleep 0.5; done
  pkill -9 -f "$1" || true
}

down_gateway() {
  pkill -f "testbed[.]labd" || true
  gone "testbed[.]labd"
  pkill -f "intentqos[.](daemon|watchdog|agent)" || true
  pkill -f "testbed[.](traffic|speedsrv|delayline)" || true
  gone "intentqos[.](daemon|watchdog|agent)"
  "$PY" testbed/testbed.py down > /dev/null 2>&1 || true
}

down_controller() {
  pkill -f "controller[.](server|simfleet)" || true
  gone "controller[.](server|simfleet)"
}

status() {
  for part in "controller:controller[.]server" "simulated-homes:controller[.]simfleet" "lab:testbed[.]labd" \
              "engine:intentqos[.]daemon" "watchdog:intentqos[.]watchdog" "agent:intentqos[.]agent"; do
    name=${part%%:*}; pat=${part#*:}
    if running "$pat"; then echo "$name running"; else echo "$name stopped"; fi
  done
}

what=${2:-all}
case "${1:-}" in
  up)
    case "$what" in
      controller) up_controller ;;
      gateway) up_gateway ;;
      all) up_controller; up_gateway ;;
      *) echo "usage: $0 up [all|controller|gateway]"; exit 2 ;;
    esac
    echo "starting $what: operator console http://localhost:8088/ops/ , app http://localhost:8088/app/ , gateway http://localhost:8090/" ;;
  down)
    case "$what" in
      controller) down_controller ;;
      gateway) down_gateway ;;
      all) down_gateway; down_controller ;;
      *) echo "usage: $0 down [all|controller|gateway]"; exit 2 ;;
    esac
    echo "stopped $what" ;;
  status) status ;;
  hold) exec sleep 2147483647 ;;
  diag)
    status
    echo "kernel $(uname -r), $("$PY" --version 2>&1), memory free $(free -m | awk '/Mem:/{print $7}') MB"
    if [ "$what" = controller ]; then logs="controller.log simfleet.log"; else logs="engine.log labd.log traffic.log watchdog.log"; fi
    for f in $logs; do [ -f "$LOG/$f" ] && { echo "--- $f"; tail -n 6 "$LOG/$f" | cut -c1-160; }; done
    echo "--- kernel messages"; dmesg 2>/dev/null | tail -n 6 | cut -c1-160 ;;
  *) echo "usage: $0 up|down [all|controller|gateway] | status | hold"; exit 2 ;;
esac
