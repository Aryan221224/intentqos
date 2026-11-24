#!/usr/bin/env python3
"""IntentQoS full-system emulation: the whole Round 2 hardware setup in one Linux host.

    sudo python3 systemsim/world.py up      # build namespaces, links, emulators, servers; boot the router VM
    sudo python3 systemsim/world.py status
    sudo python3 systemsim/world.py down

What is real and what is emulated (see systemsim/README.md):
  * Router: the official OpenWrt release image (armsr/armv8, the same aarch64 kernel and userland as the
    GL-MT3000's OpenWrt build) in a QEMU aarch64 VM: 2 x Cortex-A53, 512 MB RAM, NAND-like disk throttling,
    no battery-backed clock. IntentQoS is installed on it by the real Router Setup program over SSH.
  * Laptops and phones run the real kit programs (Linux builds) and real Linux network stacks, each in its
    own network namespace with its own DHCP client identity.
  * Emulated: the fibre line (rate, 50 ms modem FIFO, GPON upstream wait, jitter, burst loss, evening dips),
    the ISP CPE with NAT and the ISP's CGNAT, the internet servers, home Wi-Fi (airtime caps, aggregation,
    stalls), and the 4G path (LTE rate variation, 300 ms modem buffer, CGNAT, phone USB tethering).
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.environ.get("SYSSIM_WORK", "/home/claude/syssim")
STATE = os.path.join(WORK, "state.json")
PROFILE = json.load(open(os.path.join(REPO, "testbed", "profiles", "india_ftth_evening.json")))

# The emulated line: a 30/30 Mbit/s plan (India's entry fibre tier). Not the field profile's 100/100: the router VM
# forwards packets about half as fast as a real MT7981 (TCG-emulated virtio), and at 50-100 Mbit/s it ran out of
# CPU, which a real GL-MT3000 would not. 30/30 keeps the router below saturation so the results are about QoS,
# not about the emulator. The calibration runs are in the report (systemsim/README.md).
PLAN = {"down": float(os.environ.get("SYSSIM_DOWN", 30)), "up": float(os.environ.get("SYSSIM_UP", 30))}
LTE = PROFILE["failover_4g"]

# household: name -> (namespace, segment, mac, dhcp hostname, vendor class, role)
DEVICES = {
    "ops":     ("w-ops",   "br-sw",  "3c:a9:f4:10:00:01", "OPS-LAPTOP",   "MSFT 5.0",        "Operator laptop (Operator Console)"),
    "pc":      ("w-pc",    "br-sw",  "f8:bc:12:10:00:02", "FAMILY-PC",    "MSFT 5.0",        "Desktop: ISO download, cloud backup"),
    "laptop":  ("w-lap",   "br-w5",  "3c:a9:f4:10:00:03", "ASHA-LAPTOP",  "MSFT 5.0",        "Work laptop on 5 GHz: video call, Test Kit"),
    "phone":   ("w-phone", "br-w5",  "8e:5a:21:10:00:04", "",             "android-dhcp-14", "Phone on 5 GHz: subscriber app (private MAC)"),
    "tv1":     ("w-tv1",   "br-w5",  "a8:23:fe:10:00:05", "LGwebOSTV",    "udhcp 1.31.1",    "TV 1 on 5 GHz: streaming"),
    "tv2":     ("w-tv2",   "br-w24", "64:cc:2e:10:00:06", "MiTV-Stick",   "android-dhcp-11", "TV 2 (streaming stick) on 2.4 GHz"),
    "tv3":     ("w-tv3",   "br-w24", "a8:23:fe:10:00:07", "LGwebOSTV-2",  "udhcp 1.31.1",    "TV 3 on 2.4 GHz"),
    "console": ("w-con",   "br-w5",  "00:d9:d1:10:00:08", "PS5-7781",     "PS5",             "Game console on 5 GHz"),
}
# Public-looking addresses on purpose: OpenWrt's dnsmasq rebind protection discards upstream answers in private
# and documentation ranges (192.0.2.0/24, 203.0.113.0/24, ...), exactly as it would on a real router.
SERVERS = {"cdn": "104.18.20.10", "dns1": "1.1.1.1", "dns2": "8.8.8.8", "dns3": "9.9.9.9", "ntp": "162.159.200.1",
           "mirror": "151.101.2.132"}
ISP_PUBLIC = "49.36.128.9"          # the ISP's CGNAT public address for this home (Jio-like prefix)
LTE_PUBLIC = "106.205.77.4"         # the 4G operator's CGNAT public address
VM = {"mt3000": {"img": "openwrt-25.12.6-armsr-armv8-generic-squashfs-combined-efi.img", "mem": 512,
                 "lan": "t3-lan", "wan": "t3-wan", "w5": "t3-w5", "w24": "t3-w24", "macbase": "94:83:c4:3a:00"},
      "cudy":   {"img": "openwrt-24.10.8-armsr-armv8-generic-squashfs-combined-efi.img", "mem": 256,
                 "lan": "tc-lan", "wan": "tc-wan", "w5": "-", "w24": "-", "macbase": "80:af:ca:5b:00"}}


def sh(cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd}\n{p.stdout}{p.stderr}")
    return p


def nsx(ns: str, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh(f"ip netns exec {ns} sh -c {json.dumps(cmd)}", check=check)


def bg(ns: str | None, cmd: str, log: str) -> int:
    """Start a long-running process (in a namespace), detached, logging to WORK/logs/<log>."""
    os.makedirs(os.path.join(WORK, "logs"), exist_ok=True)
    full = (f"ip netns exec {ns} " if ns else "") + cmd
    p = subprocess.Popen(full, shell=True, stdout=open(os.path.join(WORK, "logs", log), "ab"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return p.pid


def no_offload(dev: str, ns: str | None = None) -> None:
    # real Ethernet/Wi-Fi carries MTU-sized frames; veth/TAP offloads would hide per-packet costs
    pre = f"ip netns exec {ns} " if ns else ""
    sh(f"{pre}ethtool -K {dev} tso off gso off gro off tx off rx off", check=False)


def veth(a: str, ns_a: str | None, b: str, ns_b: str | None, mac_b: str | None = None) -> None:
    sh(f"ip link add {a}{' netns ' + ns_a if ns_a else ''} type veth peer name {b}{' netns ' + ns_b if ns_b else ''}")
    if mac_b:
        sh(f"ip {'-n ' + ns_b if ns_b else ''} link set {b} address {mac_b}")
    for d, n in ((a, ns_a), (b, ns_b)):
        sh(f"ip {'-n ' + n if n else ''} link set {d} up")
        no_offload(d, n)


def ns_add(ns: str) -> None:
    sh(f"ip netns add {ns}")
    sh(f"ip -n {ns} link set lo up")
    nsx(ns, "sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv4.ping_group_range='0 2147483647'", check=False)


def bridge(name: str) -> None:
    sh(f"ip link add {name} type bridge forward_delay 0 stp_state 0 mcast_snooping 0")
    sh(f"ip link set {name} up")


def buffer_bytes(rate_mbit: float, ms: float) -> int:
    return max(64_000, int(rate_mbit * 1e6 / 8 * ms / 1000))


def accessemu_conf(path: str, seed: int, lte: bool = False) -> None:
    """WAN-only configuration of the field-calibrated access emulator (Wi-Fi is modelled on the LAN side)."""
    lines = [f"seed={seed}", "epoch=0", "lan4=0.0.0.0", f"gw4={'100.88.0.0' if lte else '100.72.15.0'}"]
    for d in ("down", "up"):
        for k, v in PROFILE["wan"][d].items():
            lines.append(f"{d}.{k}={v}")
        if lte:
            lines += [f"{d}.extra_ms={LTE['extra_ms']}", f"{d}.jitter_ms={LTE['jitter_ms']}", f"{d}.loss={LTE['loss']}",
                      f"{d}.ge_p=0"]
            if d == "up":
                lines += ["up.dba_min_ms=0", "up.dba_max_ms=0"]
    tmp = path + ".tmp"
    open(tmp, "w").write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def start_accessemu(ns: str, tun: str, conf: str, iifs: list[str]) -> int:
    binary = os.path.join(REPO, "testbed", "accessemu")
    src = binary + ".c"
    if not os.path.exists(binary) or os.path.getmtime(binary) < os.path.getmtime(src):
        sh(f"gcc -O2 -o {binary} {src} -lm")
    pid = bg(ns, f"{binary} {tun} {conf}", f"accessemu-{ns}.log")
    for _ in range(50):
        if sh(f"ip -n {ns} link show {tun}", check=False).returncode == 0:
            break
        time.sleep(0.1)
    time.sleep(0.3)
    nsx(ns, f"sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 net.ipv4.conf.{tun}.rp_filter=0")
    sh(f"ip -n {ns} route add default dev {tun} table 200")
    for i, iif in enumerate(iifs):
        sh(f"ip -n {ns} rule add iif {iif} lookup 200 pref {100 + i}")
    return pid


def shape(ns: str, dev: str, rate: float, limit: int) -> None:
    burst = max(16_000, int(rate * 1e6 / 8 / 250))
    nsx(ns, f"tc qdisc replace dev {dev} root tbf rate {rate}mbit burst {burst} limit {limit}")


def up() -> dict:
    os.makedirs(WORK, exist_ok=True)
    st: dict = {"t_up": time.time(), "plan": PLAN, "pids": {}, "profile": PROFILE["name"]}
    sh("sysctl -qw net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0", check=False)
    sh("sysctl -qw net.ipv4.ip_forward=1")
    for ns in ["w-inet", "w-isp", "w-ont", "w-lte", "w-ph4g", "w-brg"] + [d[0] for d in DEVICES.values()]:
        ns_add(ns)
    for b in ["br-ont", "br-brg", "br-sw", "br-w5", "br-w24", "br-ont2", "br-lan2"]:
        bridge(b)

    # ---------------------------------------------------------------- internet
    veth("eth-isp", "w-inet", "eth-inet", "w-isp")
    sh("ip -n w-inet addr add 172.31.0.2/30 dev eth-isp"); sh("ip -n w-isp addr add 172.31.0.1/30 dev eth-inet")
    veth("eth-lte", "w-inet", "eth-inet", "w-lte")
    sh("ip -n w-inet addr add 172.31.1.2/30 dev eth-lte"); sh("ip -n w-lte addr add 172.31.1.1/30 dev eth-inet")
    for a in SERVERS.values():
        if a != SERVERS["mirror"]:
            sh(f"ip -n w-inet addr add {a}/32 dev lo")
    # replies leave from the server address the client contacted (an unconnected UDP socket would otherwise use
    # the link address, and the home's NATs would rightly drop the "unknown" source)
    sh(f"ip -n w-inet route add {ISP_PUBLIC}/32 via 172.31.0.1 src {SERVERS['cdn']}")
    sh(f"ip -n w-inet route add {LTE_PUBLIC}/32 via 172.31.1.1 src {SERVERS['cdn']}")
    nsx("w-inet", "sysctl -qw net.ipv4.ip_forward=1")
    # the package mirror runs in the host namespace (it fetches the real release files through the proxy)
    veth("v-mir", None, "eth-mir", "w-inet")
    sh("ip addr add 10.255.0.1/30 dev v-mir"); sh(f"ip addr add {SERVERS['mirror']}/32 dev v-mir")
    sh("ip -n w-inet addr add 10.255.0.2/30 dev eth-mir")
    sh(f"ip -n w-inet route add {SERVERS['mirror']}/32 via 10.255.0.1")
    sh(f"ip route add {ISP_PUBLIC}/32 via 10.255.0.2"); sh(f"ip route add {LTE_PUBLIC}/32 via 10.255.0.2")
    nsx("w-inet", f"sysctl -qw net.ipv4.tcp_congestion_control={PROFILE['tcp']['server_cc']}", check=False)
    for ns in ["w-inet"] + [d[0] for d in DEVICES.values()] + ["w-brg"]:
        nsx(ns, "sysctl -qw net.core.rmem_max=16777216 net.core.wmem_max=16777216 "
                "net.ipv4.tcp_rmem='4096 131072 16777216' net.ipv4.tcp_wmem='4096 131072 16777216'", check=False)

    # ---------------------------------------------------------------- ISP: fibre line + CGNAT
    veth("eth-ont", "w-isp", "eth-wan", "w-ont")
    sh("ip -n w-isp addr add 100.72.15.1/30 dev eth-ont"); sh("ip -n w-ont addr add 100.72.15.2/30 dev eth-wan")
    sh("ip -n w-ont route add default via 100.72.15.1")
    sh("ip -n w-isp route add default via 172.31.0.2")
    nsx("w-isp", "sysctl -qw net.ipv4.ip_forward=1")
    # CGNAT. Packets pass the emulator TUN first, so translate on the way into it and leave the second pass
    # (out of the TUN) untracked.
    nsx("w-isp", f"iptables -t nat -A POSTROUTING -s 100.64.0.0/10 ! -d 100.64.0.0/10 -o emu-fibre -j SNAT --to-source {ISP_PUBLIC}")
    nsx("w-isp", "iptables -t raw -A PREROUTING -i emu-fibre -j CT --notrack")
    f = PROFILE["plan"]["rate_factor"]
    shape("w-isp", "eth-ont", round(PLAN["down"] * f, 2), buffer_bytes(PLAN["down"], PROFILE["buffers"]["down_ms"]))
    shape("w-isp", "eth-inet", round(PLAN["up"] * f, 2), buffer_bytes(PLAN["up"], PROFILE["buffers"]["up_ms"]))
    accessemu_conf(os.path.join(WORK, "access-isp.conf"), seed=1)
    st["pids"]["accessemu_isp"] = start_accessemu("w-isp", "emu-fibre", os.path.join(WORK, "access-isp.conf"),
                                                  ["eth-ont", "eth-inet"])

    # ---------------------------------------------------------------- ISP CPE (JioFiber-like ONT/router)
    veth("v-ont", None, "eth-lan", "w-ont", mac_b="d4:6b:a6:29:00:01")
    sh("ip link set v-ont master br-ont")
    sh("ip -n w-ont addr add 192.168.29.1/24 dev eth-lan")
    nsx("w-ont", "sysctl -qw net.ipv4.ip_forward=1")
    nsx("w-ont", "iptables -t nat -A POSTROUTING -o eth-wan -j MASQUERADE")
    st["pids"]["ont_dnsmasq"] = bg("w-ont", "dnsmasq -k --interface=eth-lan --bind-interfaces "
                                   "--dhcp-range=192.168.29.10,192.168.29.250,24h --dhcp-option=3,192.168.29.1 "
                                   "--dhcp-option=6,192.168.29.1 --server=1.1.1.1 --no-resolv --log-dhcp "
                                   f"--dhcp-leasefile={WORK}/ont.leases --pid-file={WORK}/ont-dnsmasq.pid", "ont-dnsmasq.log")

    # ---------------------------------------------------------------- 4G: LTE network, phone (USB tethering), bridge laptop
    veth("eth-ue", "w-lte", "rmnet0", "w-ph4g")
    sh("ip -n w-lte addr add 100.88.0.1/30 dev eth-ue"); sh("ip -n w-ph4g addr add 100.88.0.2/30 dev rmnet0")
    sh("ip -n w-ph4g route add default via 100.88.0.1")
    sh("ip -n w-lte route add default via 172.31.1.2")
    nsx("w-lte", "sysctl -qw net.ipv4.ip_forward=1")
    nsx("w-lte", f"iptables -t nat -A POSTROUTING -s 100.64.0.0/10 ! -d 100.64.0.0/10 -o emu-lte -j SNAT --to-source {LTE_PUBLIC}")
    nsx("w-lte", "iptables -t raw -A PREROUTING -i emu-lte -j CT --notrack")
    shape("w-lte", "eth-ue", LTE["down_mbit"], buffer_bytes(LTE["down_mbit"], LTE["buffer_ms"]))
    shape("w-lte", "eth-inet", LTE["up_mbit"], buffer_bytes(LTE["up_mbit"], LTE["buffer_ms"]))
    accessemu_conf(os.path.join(WORK, "access-lte.conf"), seed=2, lte=True)
    st["pids"]["accessemu_lte"] = start_accessemu("w-lte", "emu-lte", os.path.join(WORK, "access-lte.conf"),
                                                  ["eth-ue", "eth-inet"])
    # phone: Android USB tethering (rndis) gives the laptop 192.168.42.x and NATs to the mobile data link
    veth("usb0", "w-ph4g", "usb0", "w-brg", mac_b="02:1a:11:f0:42:01")
    sh("ip -n w-ph4g addr add 192.168.42.129/24 dev usb0")
    nsx("w-ph4g", "sysctl -qw net.ipv4.ip_forward=1")
    nsx("w-ph4g", "iptables -t nat -A POSTROUTING -o rmnet0 -j MASQUERADE")
    st["pids"]["phone_dnsmasq"] = bg("w-ph4g", "dnsmasq -k --interface=usb0 --bind-interfaces "
                                     "--dhcp-range=192.168.42.10,192.168.42.254,1h --dhcp-option=3,192.168.42.129 "
                                     "--dhcp-option=6,192.168.42.129 --server=8.8.8.8 --no-resolv "
                                     f"--dhcp-leasefile={WORK}/phone4g.leases --pid-file={WORK}/phone4g-dnsmasq.pid",
                                     "phone4g-dnsmasq.log")
    # bridge laptop's Ethernet port; its cable goes to the router WAN only during the failover test
    veth("v-brg", None, "eth0", "w-brg", mac_b="54:bf:64:42:00:09")
    sh("ip link set v-brg master br-brg")

    # router VM ports: persistent TAPs on the host bridges (QEMU attaches to them)
    for vm, seg in (("mt3000", {"lan": "br-sw", "wan": "br-ont", "w5": "br-w5", "w24": "br-w24"}),
                    ("cudy", {"lan": "br-lan2", "wan": "br-ont2"})):
        for k, b in seg.items():
            t = VM[vm][k]
            sh(f"ip tuntap add dev {t} mode tap")
            sh(f"ip link set {t} master {b} up")
            no_offload(t)

    # ---------------------------------------------------------------- household devices
    for name, (ns, seg, mac, host, vendor, _role) in DEVICES.items():
        if seg == "br-sw":                      # wired: a cable to the 5-port switch
            port = f"p-{name}"[:15]
            veth(port, None, "eth0", ns, mac_b=mac)
            sh(f"ip link set {port} master {seg}")
        conf = os.path.join(WORK, f"dhclient-{name}.conf")
        lines = []
        if host:
            lines.append(f'send host-name "{host}";')
        lines.append(f'send vendor-class-identifier "{vendor}";')
        lines.append("request subnet-mask, broadcast-address, routers, domain-name-servers, domain-name, ntp-servers;")
        open(conf, "w").write("\n".join(lines) + "\n")

    # Wi-Fi model (airtime caps, aggregation, stalls) on the LAN side, between the router's radios and stations
    st["pids"]["wifiemu"] = int(sh(f"{sys.executable} {HERE}/wifi.py setup").stdout.strip().split()[-1])

    # ---------------------------------------------------------------- internet services
    hosts = os.path.join(WORK, "inet-hosts")
    open(hosts, "w").write(
        f"{SERVERS['mirror']} downloads.openwrt.org\n"
        f"{SERVERS['ntp']} 0.openwrt.pool.ntp.org 1.openwrt.pool.ntp.org 2.openwrt.pool.ntp.org 3.openwrt.pool.ntp.org "
        "time.cloudflare.com time.windows.com pool.ntp.org\n"
        f"{SERVERS['cdn']} speed.cloudflare.com cdn.example-ott.in media.example-meet.in game.example-play.in\n")
    st["pids"]["inet_dns"] = bg("w-inet", f"dnsmasq -k --no-resolv --no-hosts --local=/#/ --addn-hosts={hosts} --listen-address="
                                f"{SERVERS['dns1']},{SERVERS['dns2']},{SERVERS['dns3']} --bind-interfaces --log-queries "
                                f"--log-facility={WORK}/logs/inet-dns-queries.log --pid-file={WORK}/inet-dns.pid",
                                "inet-dns.log")
    chrony = os.path.join(WORK, "chrony-inet.conf")
    open(chrony, "w").write(f"local stratum 3\nallow all\nbindaddress {SERVERS['ntp']}\n"
                            f"pidfile {WORK}/chronyd.pid\ndriftfile {WORK}/chrony.drift\nlogdir {WORK}/logs\n")
    st["pids"]["inet_ntp"] = bg("w-inet", f"chronyd -x -d -f {chrony}", "inet-ntp.log")
    os.makedirs(os.path.join(WORK, "servers"), exist_ok=True)
    st["pids"]["traffic_server"] = bg("w-inet", f"sh -c 'cd {REPO} && exec {sys.executable} -m testbed.traffic server "
                                      f"--out {WORK}/servers'", "traffic-server.log")
    st["pids"]["mirror"] = bg(None, f"{sys.executable} {HERE}/feedmirror.py {SERVERS['mirror']} 80 "
                              f"{WORK}/mirror-cache {WORK}/logs/mirror.jsonl", "mirror.log")
    st["pids"]["speedcf"] = bg("w-inet", f"{sys.executable} {HERE}/speedcf.py {SERVERS['cdn']} 80", "speedcf.log")
    st["pids"]["tcpok"] = bg("w-inet", f"{sys.executable} {HERE}/tcpok.py 1.1.1.1:443 8.8.8.8:443", "tcpok.log")
    st["pids"]["traces"] = bg(None, f"{sys.executable} {HERE}/traces.py", "traces.log")
    json.dump(st, open(STATE, "w"), indent=1)
    return st


def down() -> None:
    sh("pkill -f 'systemsim/(wifi|traces|feedmirror|capture)'", check=False)
    sh("pkill -x accessemu", check=False)
    sh("pkill -x wifiemu", check=False)
    sh("pkill -f qemu-system-aarch64", check=False)
    for ns in sh("ip netns list", check=False).stdout.split("\n"):
        ns = ns.split(" ")[0]
        if ns.startswith("w-"):
            sh(f"ip netns pids {ns} | xargs -r kill", check=False)
            sh(f"ip netns del {ns}", check=False)
    for d in ["br-ont", "br-brg", "br-sw", "br-w5", "br-w24", "br-ont2", "br-lan2", "v-mir", "ap5", "ap24"]:
        sh(f"ip link del {d}", check=False)
    for v in VM.values():
        for k in ("lan", "wan", "w5", "w24"):
            if v[k] != "-":
                sh(f"ip link del {v[k]}", check=False)
    sh(f"ip route del {ISP_PUBLIC}/32", check=False)
    sh(f"ip route del {LTE_PUBLIC}/32", check=False)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "up":
        print(json.dumps(up(), indent=1))
    elif cmd == "down":
        down()
    else:
        print(open(STATE).read() if os.path.exists(STATE) else "{}")
