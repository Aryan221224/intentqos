#!/bin/bash
# start the Test Kit window program on the work laptop; prints its page URL
W=${SYSSIM_WORK:-/home/claude/syssim}
EP=$(cat $W/testkit-endpoints.json)
setsid ip netns exec w-lap env HOME=$W/home/laptop INTENTQOS_NO_BROWSER=1 INTENTQOS_TEST_ENDPOINTS="$EP" \
  /home/claude/aryan221224/intentqos/dist/kit/linux/intentqos-testkit >> $W/logs/testkit.out 2>&1 < /dev/null &
sleep 3; grep -o "http://127.0.0.1:[0-9]*/" $W/logs/testkit.out | tail -1
