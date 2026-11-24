#!/bin/bash
# Lease addresses for the household devices (and the bridge laptop's phone link) with each device's DHCP identity.
W=${SYSSIM_WORK:-/home/claude/syssim}
for pair in ops:w-ops pc:w-pc laptop:w-lap phone:w-phone tv1:w-tv1 tv2:w-tv2 tv3:w-tv3 console:w-con; do
  n=${pair%%:*}; ns=${pair##*:}
  ip netns exec $ns pkill -f "dhclient.*$ns" 2>/dev/null
  ip netns exec $ns dhclient -nw -cf $W/dhclient-$n.conf -pf $W/dhclient-$ns.pid -lf $W/dhclient-$ns.leases eth0 2>/dev/null
done
