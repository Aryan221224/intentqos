#!/bin/sh
# Remove IntentQoS from this router and put back what the installer changed.
#   sh /usr/lib/intentqos/uninstall.sh [--purge]     --purge also deletes the settings and the router key
set -u
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
STATE=/etc/intentqos
echo "IQ-STEP stopping IntentQoS"
if [ -x /etc/init.d/intentqos ]; then
	/etc/init.d/intentqos stop >/dev/null 2>&1
	/etc/init.d/intentqos disable >/dev/null 2>&1
fi
# remove shaping and flow marks left behind (the engine normally does this when it stops)
LAN=$(uci -q get intentqos.engine.lan); WAN=$(uci -q get intentqos.engine.wan)
for d in $LAN $WAN; do tc qdisc del dev "$d" root >/dev/null 2>&1; done
nft delete table inet intentqos >/dev/null 2>&1
# kernel flow engine (capture.backend=ebpf): its tc programs sit at preference 7, its maps are pinned
for d in $LAN $WAN $(ls "/sys/class/net/$LAN/brif" 2>/dev/null); do
	tc filter del dev "$d" ingress prio 7 >/dev/null 2>&1; tc filter del dev "$d" egress prio 7 >/dev/null 2>&1
done
rm -f /sys/fs/bpf/tc/globals/iq_flows /sys/fs/bpf/tc/globals/iq_verdict /sys/fs/bpf/tc/globals/iq_cfg \
	/sys/fs/bpf/tc/globals/iq_stats /sys/fs/bpf/tc/globals/iq_zero
# restore what the installer turned off
if [ -f "$STATE/saved-settings" ]; then
	FW=0; WL=0
	while IFS='=' read -r k v; do
		case "$k" in
			firewall.*) uci set "$k=$v"; FW=1 ;;
			wireless.*) if [ "$v" = "__unset__" ]; then uci -q delete "$k"; else uci set "$k=$v"; fi; WL=1 ;;
			service.*) s=${k#service.}; [ -x "/etc/init.d/$s" ] && { /etc/init.d/$s enable; /etc/init.d/$s start; } >/dev/null 2>&1
			           echo "IQ-STEP re-enabled the router's $s service" ;;
		esac
	done < "$STATE/saved-settings"
	if [ "$FW" = 1 ]; then uci commit firewall; /etc/init.d/firewall restart >/dev/null 2>&1; echo "IQ-STEP flow offloading restored"; fi
	if [ "$WL" = 1 ]; then uci commit wireless; ( sleep 3; wifi reload ) >/dev/null 2>&1 </dev/null & echo "IQ-STEP Wi-Fi QoS map restored (Wi-Fi reloads in a few seconds)"; fi
	rm -f "$STATE/saved-settings"
fi
rm -rf /usr/lib/intentqos /etc/init.d/intentqos /usr/sbin/intentqos-uci /usr/sbin/intentqos-ctl /usr/sbin/iq-capture \
	/etc/hotplug.d/dhcp/90-intentqos /var/etc/intentqos.toml /var/run/intentqos /tmp/intentqos
if [ "$PURGE" = 1 ]; then
	rm -rf "$STATE" /etc/config/intentqos
	echo "IQ-STEP settings and router key deleted"
else
	echo "IQ-STEP settings kept in /etc/config/intentqos and $STATE (reinstall picks them up; --purge deletes them)"
fi
echo "IQ-STEP Python, tc and nftables stay installed (other packages may use them)"
echo 'IQ-RESULT {"ok":true,"removed":true}'
