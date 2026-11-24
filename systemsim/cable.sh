#!/bin/bash
# cable.sh to-4g|to-fibre [SECONDS_IN_HAND]
# A person moves the router's WAN cable: it goes dark (carrier lost), they plug it into the other box a few
# seconds later (carrier back). Logged to WORK/logs/cable.jsonl. Runs in the host namespace (cablesvc.sh).
W=${SYSSIM_WORK:-/home/claude/syssim}; HAND=${2:-4}
mon() { python3 -c "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.connect('$W/vm/mt3000.mon'); time.sleep(0.2); s.recv(4096); s.sendall(b'$1\n'); time.sleep(0.3); s.close()"; }
log() { echo "{\"t\": $(date +%s.%N), \"cable\": \"$1\"}" >> $W/logs/cable.jsonl; }
case "$1" in
  to-4g)   from=br-ont; to=br-brg ;;
  to-fibre) from=br-brg; to=br-ont ;;
  *) echo "usage: $0 to-4g|to-fibre [seconds]"; exit 2 ;;
esac
mon "set_link wan off"; log "unplugged from $from"
ip link set t3-wan nomaster || { log "FAILED to unplug"; exit 1; }
sleep $HAND
ip link set t3-wan master $to || { log "FAILED to plug into $to"; exit 1; }
bridge link show dev t3-wan | grep -q "master $to" || { log "FAILED: port not on $to"; exit 1; }
mon "set_link wan on"; log "plugged into $to"
echo "moved $1 after ${HAND}s"
