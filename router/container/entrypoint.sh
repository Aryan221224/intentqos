#!/bin/sh
# engine + watchdog (+ agent when [agent] controller_url is set) in one container; the engine config
# lives in /etc/intentqos/intentqos.toml (mounted from the host).
CONF=/etc/intentqos/intentqos.toml
mkdir -p /var/run/intentqos /tmp/intentqos
python3 -m intentqos.watchdog --config "$CONF" &
if grep -q '^controller_url *= *"https' "$CONF"; then python3 -m intentqos.agent --config "$CONF" & fi
exec python3 -m intentqos.daemon --config "$CONF"
