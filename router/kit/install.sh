#!/bin/sh
# IntentQoS router installer: OpenWrt 21.02 or newer, prplOS, GL.iNet firmware (opkg or apk).
#
#   sh install.sh [--down 100] [--up 100] [--controller https://IP:8443 --claim CODE --pubkey HEX --ca FILE]
#                 [--serial S] [--no-deps] [--no-start] [--keep-offload] [--no-qos-map]
#
# Run from the unpacked payload directory (IntentQoS Router Setup does this over SSH). Safe to run again:
# an existing configuration is kept and only the options given on the command line change.
# Machine-readable lines start with IQ-STEP / IQ-WARN / IQ-FAIL / IQ-RESULT.

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
LOG=/tmp/intentqos-install.log
VER=$(cat "$HERE/VERSION" 2>/dev/null || echo "?")
STATE=/etc/intentqos
DOWN="" UP="" CTL="" CLAIM="" PUBKEY="" CA="" SERIAL="" DEPS=1 START=1 OFFLOAD=1 QOSMAP=1

say()  { echo "IQ-STEP $*"; echo "$(date '+%F %T') $*" >> "$LOG"; }
warn() { echo "IQ-WARN $*"; echo "$(date '+%F %T') WARN $*" >> "$LOG"; }
fail() { echo "IQ-FAIL $*"; echo "$(date '+%F %T') FAIL $*" >> "$LOG"; exit 1; }

while [ $# -gt 0 ]; do
	case "$1" in
		--down) DOWN=$2; shift ;;
		--up) UP=$2; shift ;;
		--controller) CTL=$2; shift ;;
		--claim) CLAIM=$2; shift ;;
		--pubkey) PUBKEY=$2; shift ;;
		--ca) CA=$2; shift ;;
		--serial) SERIAL=$2; shift ;;
		--no-deps) DEPS=0 ;;
		--no-start) START=0 ;;
		--keep-offload) OFFLOAD=0 ;;
		--no-qos-map) QOSMAP=0 ;;
		*) fail "unknown option $1" ;;
	esac
	shift
done

echo "IntentQoS $VER installer" > "$LOG"

# ---------------------------------------------------------------- 1. what is this box?
[ -f /etc/openwrt_release ] || fail "this is not an OpenWrt, prplOS or GL.iNet router (/etc/openwrt_release missing)"
. /etc/openwrt_release
[ "$(id -u)" = 0 ] || fail "run as root"
PM=""
if command -v apk >/dev/null 2>&1 && [ -d /etc/apk ]; then PM=apk
elif command -v opkg >/dev/null 2>&1; then PM=opkg
fi
[ -n "$PM" ] || fail "no package manager found (opkg or apk)"
FW="OpenWrt ${DISTRIB_RELEASE:-?}"
[ -f /etc/glversion ] && FW="GL.iNet firmware $(cat /etc/glversion) (OpenWrt ${DISTRIB_RELEASE:-?})"
case "$(uname -m)" in
	aarch64|arm64) ARCH=aarch64 ;;
	armv7*|armv8l) ARCH=armv7 ;;
	x86_64|amd64) ARCH=x86_64 ;;
	mips*)
		if [ "$(printf '\001\000' | od -An -tu2 | tr -d ' ')" = 1 ]; then ARCH=mipsel
		else fail "big-endian MIPS routers are not supported"; fi ;;
	*) fail "unsupported CPU $(uname -m)" ;;
esac
[ -f "$HERE/bin/iq-capture.$ARCH" ] || fail "payload has no capture helper for $ARCH"
say "router: $FW, CPU $ARCH, packages: $PM"

# ---------------------------------------------------------------- 2. dependencies
pm_update() { if [ "$PM" = apk ]; then apk update >>"$LOG" 2>&1; else opkg update >>"$LOG" 2>&1; fi; }
pm_has()    { if [ "$PM" = apk ]; then apk info -e "$1" >/dev/null 2>&1; else opkg status "$1" 2>/dev/null | grep -q "^Status:.* installed"; fi; }
pm_add()    { if [ "$PM" = apk ]; then apk add "$@" >>"$LOG" 2>&1; else opkg install "$@" >>"$LOG" 2>&1; fi; }

NEED="python3-light python3-codecs python3-email python3-urllib python3-openssl python3-logging kmod-sched-core tc-full"
command -v nft >/dev/null 2>&1 || NEED="$NEED nftables"
if [ "$DEPS" = 1 ]; then
	MISSING=""
	for p in $NEED; do pm_has "$p" || MISSING="$MISSING $p"; done
	if [ -n "$MISSING" ]; then
		FREE=$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}')
		[ -z "$FREE" ] && FREE=$(df -k / | awk 'NR==2 {print $4}')
		[ "${FREE:-0}" -lt 22000 ] && fail "not enough free flash: ${FREE} KB free, about 22000 KB needed for Python and tc"
		say "installing:$MISSING (a few minutes on a router)"
		pm_update || warn "package list update reported errors (see $LOG)"
		for p in $MISSING; do
			pm_add "$p" || {
				# tc-full and tc-tiny are alternatives on older releases: swap them
				if [ "$p" = tc-full ] && pm_has tc-tiny; then
					if [ "$PM" = apk ]; then apk del tc-tiny >>"$LOG" 2>&1; else opkg remove tc-tiny >>"$LOG" 2>&1; fi
					pm_add tc-full || fail "could not install tc-full (see $LOG)"
				elif [ "$p" = nftables ]; then
					pm_add nftables-json || pm_add nftables-nojson || fail "could not install nftables (see $LOG)"
				else
					fail "could not install $p: check the router's internet connection (see $LOG)"
				fi
			}
		done
	fi
	pm_has kmod-sched-cake || pm_add kmod-sched-cake || warn "CAKE is not available on this firmware: HTB shaping will be used"
fi
command -v python3 >/dev/null 2>&1 || fail "python3 is missing (run without --no-deps)"
command -v tc >/dev/null 2>&1 || fail "tc is missing (run without --no-deps)"
command -v nft >/dev/null 2>&1 || fail "nft is missing (run without --no-deps)"
python3 -c 'import ssl, json, hmac, http.server, urllib.request, ipaddress, dataclasses, encodings.idna' 2>>"$LOG" \
	|| fail "Python on this router is missing standard modules (see $LOG)"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "dependencies ready: Python $PYV, $(tc -V 2>&1 | head -1), $(nft -v 2>&1)"

# ---------------------------------------------------------------- 3. files
[ -x /etc/init.d/intentqos ] && /etc/init.d/intentqos stop >/dev/null 2>&1
mkdir -p /usr/lib/intentqos "$STATE"
rm -rf /usr/lib/intentqos/intentqos
cp -R "$HERE/files/." / || fail "could not copy files (flash full?)"
# dnsmasq turns on its DHCP script only when it starts and finds hotplug handlers: reload it once so the
# device-identity hook (/etc/hotplug.d/dhcp/90-intentqos) runs on the next lease (DNS/DHCP pause for ~1 s)
[ -x /etc/init.d/dnsmasq ] && /etc/init.d/dnsmasq reload >/dev/null 2>&1
cp "$HERE/bin/iq-capture.$ARCH" /usr/sbin/iq-capture
chmod 0755 /usr/sbin/iq-capture /etc/init.d/intentqos /usr/sbin/intentqos-uci /usr/sbin/intentqos-ctl \
	/usr/lib/intentqos/uninstall.sh
cp "$HERE/VERSION" /usr/lib/intentqos/VERSION
say "files installed to /usr/lib/intentqos"

# ---------------------------------------------------------------- 4. configuration (detected)
iface_dev() { ubus call "network.interface.$1" status 2>/dev/null | jsonfilter -e '@.l3_device' 2>/dev/null; }
iface_ip()  { ubus call "network.interface.$1" status 2>/dev/null | jsonfilter -e '@["ipv4-address"][0].address' 2>/dev/null; }
iface_len() { ubus call "network.interface.$1" status 2>/dev/null | jsonfilter -e '@["ipv4-address"][0].mask' 2>/dev/null; }

NEWCFG=0
if [ ! -f /etc/config/intentqos ]; then
	cp "$HERE/defaults/intentqos.config" /etc/config/intentqos
	NEWCFG=1
fi
LAN=$(iface_dev lan); [ -n "$LAN" ] || LAN=$(uci -q get network.lan.device); [ -n "$LAN" ] || LAN=br-lan
WAN=$(iface_dev wan); [ -n "$WAN" ] || WAN=$(uci -q get network.wan.device); [ -n "$WAN" ] || WAN=$(uci -q get network.wan.ifname)
[ -n "$WAN" ] || fail "could not find the WAN port (network.wan)"
LANIP=$(iface_ip lan); [ -n "$LANIP" ] || LANIP=$(uci -q get network.lan.ipaddr | cut -d/ -f1)
LEN=$(iface_len lan); [ -n "$LEN" ] || LEN=24
[ -n "$LANIP" ] || fail "could not find the router's LAN address"
NET=$(ipcalc.sh "$LANIP/$LEN" 2>/dev/null | sed -n 's/^NETWORK=//p')
[ -n "$NET" ] || NET=$(echo "$LANIP" | cut -d. -f1-3).0
uci -q batch <<EOF
set intentqos.engine.lan='$LAN'
set intentqos.engine.wan='$WAN'
delete intentqos.engine.lan_subnet
add_list intentqos.engine.lan_subnet='$NET/$LEN'
set intentqos.engine.api_listen='$LANIP:8080'
EOF
[ -n "$DOWN" ] && uci set intentqos.engine.down_mbit="$DOWN"
[ -n "$UP" ] && uci set intentqos.engine.up_mbit="$UP"
if [ -n "$CTL" ]; then
	[ -n "$CLAIM" ] || fail "--controller needs --claim"
	uci set intentqos.agent.enabled=1
	uci set intentqos.agent.controller_url="$CTL"
	uci set intentqos.agent.claim_code="$CLAIM"
	uci set intentqos.agent.discover=1             # find the Operator Console again if its address changes
	[ -n "$PUBKEY" ] && uci set intentqos.agent.release_pubkey="$PUBKEY"
	[ -n "$SERIAL" ] && uci set intentqos.agent.serial="$SERIAL"
	if [ -n "$CA" ]; then
		cp "$CA" "$STATE/operator-ca.pem" || fail "could not copy the Operator Console certificate ($CA)"
		uci set intentqos.agent.ca_file="$STATE/operator-ca.pem"
	fi
	rm -f "$STATE/agent.json"        # a new pairing enrols again
fi
uci commit intentqos
say "configured: LAN $LAN ($NET/$LEN, console on $LANIP:8080), WAN $WAN, plan $(uci -q get intentqos.engine.down_mbit)/$(uci -q get intentqos.engine.up_mbit) Mbit/s"

# ---------------------------------------------------------------- 5. things that would bypass or fight the shaper
SAVED="$STATE/saved-settings"
if [ "$OFFLOAD" = 1 ]; then
	[ -f "$SAVED" ] || : > "$SAVED"
	for opt in flow_offloading flow_offloading_hw; do
		cur=$(uci -q get "firewall.@defaults[0].$opt")
		if [ "$cur" = 1 ]; then
			grep -q "^firewall.@defaults\[0\].$opt=" "$SAVED" || echo "firewall.@defaults[0].$opt=1" >> "$SAVED"
			uci set "firewall.@defaults[0].$opt=0"
			CHANGED_FW=1
		fi
	done
	if [ "${CHANGED_FW:-0}" = 1 ]; then
		uci commit firewall
		/etc/init.d/firewall restart >>"$LOG" 2>&1
		say "turned off flow offloading (offloaded packets skip the shaper); uninstall restores it"
	fi
	for svc in sqm qos gl_qos; do
		if [ -x "/etc/init.d/$svc" ] && /etc/init.d/$svc enabled 2>/dev/null; then
			/etc/init.d/$svc stop >>"$LOG" 2>&1
			/etc/init.d/$svc disable >>"$LOG" 2>&1
			grep -q "^service.$svc=" "$SAVED" || echo "service.$svc=enabled" >> "$SAVED"
			say "stopped the router's own $svc service (two shapers would fight); uninstall restores it"
		fi
	done
	[ -d /sys/module/mtkhnat ] && warn "MediaTek hardware NAT is loaded: turn off 'Network Acceleration' in the GL.iNet admin panel if shaping has no effect"
	[ -d /sys/module/shortcut_fe ] && warn "Shortcut-FE acceleration is loaded: turn it off in the router's admin panel if shaping has no effect"
fi

# ---------------------------------------------------------------- 5b. Wi-Fi: DSCP -> access category (QoS Map Set)
# IntentQoS marks calls EF, video AF41 and bulk CS1. The QoS Map Set tells the access point (and phones that
# support it) which Wi-Fi access category each DSCP goes to. Current OpenWrt already sets a default map (RFC 8325
# with the RFC 8622 update, where CS1 is best effort); IntentQoS's map follows RFC 8325's table, where CS1 is
# background, so its bulk marks also yield on the air. The old value is saved and put back on uninstall.
WIFI_RELOAD=0
if [ "$QOSMAP" = 1 ] && [ -f /etc/config/wireless ]; then
	MAP=$(python3 -c 'import sys; sys.path.insert(0, "/usr/lib/intentqos"); from intentqos.wifi import qos_map_set; print(qos_map_set())' 2>>"$LOG")
	if [ -n "$MAP" ]; then
		[ -f "$SAVED" ] || : > "$SAVED"
		for sec in $(uci -q show wireless | sed -n 's/^wireless\.\([^.=]*\)=wifi-iface$/\1/p'); do
			[ "$(uci -q get "wireless.$sec.mode")" = "ap" ] || continue
			cur=$(uci -q get "wireless.$sec.iw_qos_map_set")
			[ "$cur" = "$MAP" ] && continue
			grep -q "^wireless\.$sec\.iw_qos_map_set=" "$SAVED" || echo "wireless.$sec.iw_qos_map_set=${cur:-__unset__}" >> "$SAVED"
			uci set "wireless.$sec.iw_qos_map_set=$MAP"
			WIFI_RELOAD=1
		done
		if [ "$WIFI_RELOAD" = 1 ]; then
			uci commit wireless
			say "Wi-Fi QoS map set (DSCP to access category, RFC 8325); Wi-Fi reloads a few seconds after setup finishes"
		fi
	fi
fi

# ---------------------------------------------------------------- 6. start and check
if [ "$START" = 1 ]; then
	/etc/init.d/intentqos enable
	/etc/init.d/intentqos restart >>"$LOG" 2>&1
	OK=0
	i=0
	while [ $i -lt 40 ]; do
		i=$((i + 1))
		if wget -q -T 2 -O /tmp/intentqos-health.json "http://$LANIP:8080/api/healthz" 2>/dev/null; then OK=1; break; fi
		sleep 1
	done
	[ "$OK" = 1 ] || fail "the engine did not start within 40 s; see: logread -e intentqos"
	say "engine running"
fi
KEY=$(cat "$STATE/admin.key" 2>/dev/null)
SER=$(uci -q get intentqos.agent.serial)
if [ "$WIFI_RELOAD" = 1 ]; then          # after the result: setup may be running over this Wi-Fi
	( sleep 5; wifi reload ) >/dev/null 2>&1 </dev/null &
fi
echo "IQ-RESULT {\"ok\":true,\"version\":\"$VER\",\"firmware\":\"$FW\",\"arch\":\"$ARCH\",\"python\":\"$PYV\",\"lan\":\"$LAN\",\"wan\":\"$WAN\",\"lan_ip\":\"$LANIP\",\"subnet\":\"$NET/$LEN\",\"console\":\"http://$LANIP:8080/\",\"key\":\"$KEY\",\"agent\":\"$(uci -q get intentqos.agent.enabled)\",\"serial\":\"$SER\",\"new_config\":$NEWCFG}"
