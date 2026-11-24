#!/bin/bash
# setplan.sh DOWN UP : the ISP line's plan rate (emulator side) + restart the capacity trace player
D=$1; U=$2; W=${SYSSIM_WORK:-/home/claude/syssim}
python3 - "$D" "$U" <<'PY'
import sys, subprocess
d, u = float(sys.argv[1]), float(sys.argv[2])
for dev, r in (("eth-ont", d), ("eth-inet", u)):
    rate = r * 1.02; burst = max(16000, int(rate * 1e6 / 8 / 250)); lim = max(64000, int(r * 1e6 / 8 * 0.05))
    subprocess.run(f"ip netns exec w-isp tc qdisc replace dev {dev} root tbf rate {rate:.2f}mbit burst {burst} limit {lim}", shell=True, check=True)
PY
pkill -f systemsim/traces.py
SYSSIM_DOWN=$D SYSSIM_UP=$U setsid python3 /home/claude/aryan221224/intentqos/systemsim/traces.py >> $W/logs/traces.log 2>&1 < /dev/null &
echo "{\"t\": $(date +%s.%N), \"event\": \"line_plan\", \"down\": $D, \"up\": $U}" >> $W/logs/line-plan.jsonl
