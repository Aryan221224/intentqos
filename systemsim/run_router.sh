#!/bin/bash
# run_router.sh NAME IMG MEM_MB WAN_TAP LAN_TAP W5_TAP W24_TAP MACBASE
# One router VM: 2x Cortex-A53 (as MT7981B), RAM as the real board, no battery RTC (clock starts at 1970,
# OpenWrt's sysfixtime moves it to the newest file in /etc until NTP syncs), NAND-like disk throttling.
N=$1 IMG=$2 MEM=$3 WAN=$4 LAN=$5 W5=$6 W24=$7 MB=${8:-52:54:00:aa:00}
cd /home/claude/syssim/vm
NICS=""
[ -n "$LAN" ] && NICS="$NICS -netdev tap,id=lan,ifname=$LAN,script=no,downscript=no -device virtio-net-pci,netdev=lan,csum=off,gso=off,guest_tso4=off,guest_tso6=off,guest_ecn=off,guest_ufo=off,host_tso4=off,host_tso6=off,host_ecn=off,host_ufo=off,mrg_rxbuf=on,mac=$MB:01"
[ -n "$WAN" ] && NICS="$NICS -netdev tap,id=wan,ifname=$WAN,script=no,downscript=no -device virtio-net-pci,netdev=wan,csum=off,gso=off,guest_tso4=off,guest_tso6=off,guest_ecn=off,guest_ufo=off,host_tso4=off,host_tso6=off,host_ecn=off,host_ufo=off,mrg_rxbuf=on,mac=$MB:02"
[ "$W5" != "-" ] && [ -n "$W5" ] && NICS="$NICS -netdev tap,id=w5,ifname=$W5,script=no,downscript=no -device virtio-net-pci,netdev=w5,csum=off,gso=off,guest_tso4=off,guest_tso6=off,guest_ecn=off,guest_ufo=off,host_tso4=off,host_tso6=off,host_ecn=off,host_ufo=off,mrg_rxbuf=on,mac=$MB:03"
[ "$W24" != "-" ] && [ -n "$W24" ] && NICS="$NICS -netdev tap,id=w24,ifname=$W24,script=no,downscript=no -device virtio-net-pci,netdev=w24,csum=off,gso=off,guest_tso4=off,guest_tso6=off,guest_ecn=off,guest_ufo=off,host_tso4=off,host_tso6=off,host_ecn=off,host_ufo=off,mrg_rxbuf=on,mac=$MB:04"
exec qemu-system-aarch64 -name $N -M virt -accel tcg,tb-size=192 -cpu cortex-a53 -smp 2 -m $MEM \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd \
  -drive if=pflash,format=raw,file=$N-vars.fd \
  -drive if=none,id=d0,format=raw,file=$IMG,throttling.bps-read=20000000,throttling.bps-write=4000000 \
  -device virtio-blk-pci,drive=d0 \
  -rtc base=1970-01-01T00:00:00 \
  $NICS -display none -serial unix:$PWD/$N.sock,server,nowait \
  -monitor unix:$PWD/$N.mon,server,nowait -pidfile $N.pid
