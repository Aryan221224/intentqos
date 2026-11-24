#!/bin/bash
# cablereq.sh to-4g|to-fibre : ask the host-side cable service to move the router's WAN cable, wait until done
W=${SYSSIM_WORK:-/home/claude/syssim}; rm -f $W/cable.done; echo "$1" > $W/cable.request
for i in $(seq 1 150); do [ -s $W/cable.done ] && { cat $W/cable.done; exit 0; }; sleep 0.2; done; echo timeout; exit 1
