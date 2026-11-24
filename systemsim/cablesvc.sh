#!/bin/bash
# Runs in the host namespace: carries out cable moves requested from inside a laptop's namespace
# (echo to-4g > $W/cable.request; wait for $W/cable.done).
W=${SYSSIM_WORK:-/home/claude/syssim}; S=$(cd "$(dirname "$0")" && pwd)
while true; do
  if [ -f $W/cable.request ]; then a=$(cat $W/cable.request); rm -f $W/cable.request $W/cable.done
    $S/cable.sh $a 4 > $W/cable.done 2>&1; fi
  sleep 0.2
done
