#!/bin/bash
# Bring the whole emulated home back (e.g. after the host restarted): world, router VM, leases, console.
set -e
W=${SYSSIM_WORK:-/home/claude/syssim}; S=$(cd "$(dirname "$0")" && pwd)
for n in $(ls /var/run/netns 2>/dev/null); do ip netns del $n 2>/dev/null || true; done
python3 $S/world.py down >/dev/null 2>&1 || true
python3 $S/world.py up > $W/world-up.json
cd $W/vm && cp $S/run_router.sh . && rm -f mt3000.sock mt3000.mon
setsid ./run_router.sh mt3000 mt3000.img 512 t3-wan t3-lan t3-w5 t3-w24 94:83:c4:3a:00 > mt3000-qemu.log 2>&1 < /dev/null &
for i in $(seq 1 50); do [ -S mt3000.sock ] && break; sleep 0.2; done; python3 $S/con.py mt3000.sock --wait "Please press Enter" --timeout 300 --quiet --log mt3000-boot-$(date +%s).log
$S/dhcp.sh
setsid ip netns exec w-ops env HOME=$W/home/ops INTENTQOS_NO_BROWSER=1 $(dirname $S)/dist/kit/linux/intentqos-console --no-window >> $W/logs/console.out 2>&1 < /dev/null &
sleep 25
for ns in w-ops w-pc w-lap w-phone w-tv1 w-tv2 w-tv3 w-con; do echo "$ns $(ip -n $ns -4 -o addr show eth0 | awk '{print $4}')"; done
