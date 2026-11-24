#!/bin/bash
# The 4G bridge laptop: phone on USB (rndis_host), Ethernet port (r8152 USB adapter, the BOM's TP-Link UE300),
# DHCP on the phone link as NetworkManager would do, then the IntentQoS 4G Bridge window program.
W=${SYSSIM_WORK:-/home/claude/syssim}; S=/home/claude/aryan221224/intentqos/systemsim
ip -n w-brg link set usb0 up; ip -n w-brg link set eth0 up
ip netns exec w-brg dhclient -1 -pf $W/brg-usb0.pid -lf $W/brg-usb0.leases usb0 2>/dev/null
mkdir -p $W/home/bridge
setsid ip netns exec w-brg unshare -m sh -c "
  for i in usb0:rndis_host eth0:r8152; do n=\${i%%:*}; d=\${i##*:}; f=$W/fakesys/\$n; mkdir -p \$f/device \$f/drv/\$d
    ln -sfn \$f/drv/\$d \$f/device/driver; mount --bind \$f /sys/devices/virtual/net/\$n; done
  exec env HOME=$W/home/bridge INTENTQOS_NO_BROWSER=1 SYSSIM_WORK=$W PATH=$S/shim:\$PATH $(dirname $S)/dist/kit/linux/intentqos-4g-bridge
" >> $W/logs/bridge.out 2>&1 < /dev/null &
sleep 3; grep -o "http://127.0.0.1:[0-9]*/" $W/logs/bridge.out | tail -1
