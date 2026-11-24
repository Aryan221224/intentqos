#!/bin/bash
# failover_run.sh on|off : the Test Kit's guided fibre->4G failover, with a call and pings as ground truth
Q=$1; W=${SYSSIM_WORK:-/home/claude/syssim}; S=$(cd "$(dirname "$0")" && pwd); O=$W/capture/run3/failover
mkdir -p $O; TU=$($S/start_testkit.sh)
L=$(ip -n w-lap -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
(cd $(dirname $S) && ip netns exec w-lap python3 -m testbed.traffic voip --name fo-$Q-call --src $L --dst 104.18.20.10 --duration 200 --out $O > /dev/null 2>&1 &)
(ip netns exec w-phone ping -D -i 0.2 -W 1 -c 1000 104.18.20.10 > $O/fo-$Q-phone-ping.txt 2>&1 &)
echo "{\"t\": $(date +%s.%N), \"event\": \"failover_${Q}_start\"}" >> $W/capture/run3/timeline.jsonl
cd $W/capture/screens && ip netns exec w-lap python3 $S/ui_testkit.py $TU $W/capture/screens failover $Q "$S/cablereq.sh to-4g" "$S/cablereq.sh to-fibre" > $W/logs/ui-testkit-fo-$Q.out 2>&1
echo "{\"t\": $(date +%s.%N), \"event\": \"failover_${Q}_end\"}" >> $W/capture/run3/timeline.jsonl
