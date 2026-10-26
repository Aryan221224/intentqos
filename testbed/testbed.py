#!/usr/bin/env python3
"""Reproducible home-network testbed built from Linux network namespaces.

Topology (all links are veth pairs):

    iq-cl  (household devices)          192.168.50.10-.19  fd00:50::10-19
       | cl-eth0 <-> gw-lan
    iq-gw  (the home gateway; IntentQoS runs HERE)
       | gw-wan  <-> isp-gw
    iq-isp (ISP modem / access link -- THE BOTTLENECK, with a bloated buffer)
       | isp-srv <-> srv-eth0
    iq-srv (Internet servers)            203.0.113.10       fd00:203::10

    root ns <-> iq-gw management link   10.255.0.1 <-> 10.255.0.2
    (dashboard reachable from the host at http://10.255.0.2:8080)

The ISP link is emulated with `tbf` and a deliberately large buffer, the way a
typical cable/DSL modem behaves. This is EMULATION (as the brief requires us to
label): rates, buffer sizes and delays are parameters, not measurements of a
real access network. If the kernel has `sch_netem`, a base propagation delay is
added; otherwise base RTT is ~0.1 ms and all latency comes from queueing.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

try:
    from . import profile as PR
except ImportError:                      # run as a script
    import profile as PR  # type: ignore

NS_CL, NS_GW, NS_ISP, NS_SRV = "iq-cl", "iq-gw", "iq-isp", "iq-srv"
ALL_NS = [NS_CL, NS_GW, NS_ISP, NS_SRV]

GW_LAN, GW_WAN = "gw-lan", "gw-wan"
CL_IF, ISP_GW, ISP_SRV, SRV_IF = "cl-eth0", "isp-gw", "isp-srv", "srv-eth0"
MGMT_HOST, MGMT_GW = "iq-mgmt0", "iq-mgmt1"

LAN4, LAN6 = "192.168.50", "fd00:50::"
SERVER4, SERVER6 = "203.0.113.10", "fd00:203::10"
GW_MGMT_IP = "10.255.0.2"

# Household devices used by the scenarios (one IP each => per-host fairness is observable)
DEVICES = {
    "laptop": 10,     # work laptop: video call / VoIP
    "desktop": 11,    # big downloads, updates
    "tv1": 12, "tv2": 13, "tv3": 14,
    "console": 15,    # gaming
    "phone": 16,      # second call / misc
    "nas": 17,        # cloud backup (upload bulk)
}

STATE_FILE = "/run/intentqos-testbed.json"
IPV6 = os.path.exists("/proc/net/if_inet6")  # some minimal kernels ship without IPv6


def sh(cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {cmd}\n{r.stderr.strip()}")
    if not quiet and r.stdout.strip():
        pass
    return r


def nsx(ns: str, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh(f"ip netns exec {ns} {cmd}", check=check)


def has_qdisc(kind: str) -> bool:
    """Probe whether the running kernel supports a qdisc (modules may be absent)."""
    ns = "iq-probe"
    sh(f"ip netns del {ns}", check=False)
    sh(f"ip netns add {ns}")
    try:
        sh(f"ip -n {ns} link add d0 type veth peer name d1")
        r = sh(f"ip netns exec {ns} tc qdisc add dev d0 root {kind}", check=False)
        return r.returncode == 0
    finally:
        sh(f"ip netns del {ns}", check=False)


def device_ip(name: str, v6: bool = False) -> str:
    n = DEVICES[name]
    return f"{LAN6}{n}" if v6 else f"{LAN4}.{n}"


def _veth(a: str, ns_a: str, b: str, ns_b: str | None) -> None:
    sh(f"ip link add {a} type veth peer name {b}")
    sh(f"ip link set {a} netns {ns_a}")
    if ns_b:
        sh(f"ip link set {b} netns {ns_b}")


def _no_offload(ns: str | None, dev: str) -> None:
    """Make every packet a real wire-sized packet (no 64 KB GSO super-packets),
    so shaping and per-packet statistics behave like a physical link."""
    pre = f"ip -n {ns}" if ns else "ip"
    sh(f"{pre} link set dev {dev} gso_max_size 1514 gso_max_segs 1", check=False)


def up(down_mbit: float, up_mbit: float, isp_buffer_ms: float, base_rtt_ms: float, cc: str = "cubic") -> dict:
    down()
    P = PR.active()
    if P:                                # field-calibrated profile: rates, buffers, delays come from the profile
        down_mbit, up_mbit = P["plan"]["down_mbit"], P["plan"]["up_mbit"]
        isp_buffer_ms = P["buffers"]["down_ms"]
        cc = P["tcp"]["client_cc"]
    for ns in ALL_NS:
        sh(f"ip netns add {ns}")
        sh(f"ip -n {ns} link set lo up")

    _veth(CL_IF, NS_CL, GW_LAN, NS_GW)
    _veth(GW_WAN, NS_GW, ISP_GW, NS_ISP)
    _veth(ISP_SRV, NS_ISP, SRV_IF, NS_SRV)
    _veth(MGMT_GW, NS_GW, MGMT_HOST, None)

    for ns, dev in [(NS_CL, CL_IF), (NS_GW, GW_LAN), (NS_GW, GW_WAN), (NS_ISP, ISP_GW),
                    (NS_ISP, ISP_SRV), (NS_SRV, SRV_IF), (NS_GW, MGMT_GW)]:
        _no_offload(ns, dev)
        sh(f"ip -n {ns} link set {dev} up")
    _no_offload(None, MGMT_HOST)
    sh(f"ip link set {MGMT_HOST} up")

    # --- addressing (dual stack) ---
    for name, n in DEVICES.items():
        sh(f"ip -n {NS_CL} addr add {LAN4}.{n}/24 dev {CL_IF}")
        if IPV6:
            sh(f"ip -n {NS_CL} addr add {LAN6}{n}/64 dev {CL_IF} nodad")
    sh(f"ip -n {NS_CL} route add default via {LAN4}.1")
    if IPV6:
        sh(f"ip -n {NS_CL} -6 route add default via {LAN6}1")

    sh(f"ip -n {NS_GW} addr add {LAN4}.1/24 dev {GW_LAN}")
    if IPV6:
        sh(f"ip -n {NS_GW} addr add {LAN6}1/64 dev {GW_LAN} nodad")
    sh(f"ip -n {NS_GW} addr add 100.64.0.2/30 dev {GW_WAN}")
    if IPV6:
        sh(f"ip -n {NS_GW} addr add fd00:64::2/64 dev {GW_WAN} nodad")
    sh(f"ip -n {NS_GW} route add default via 100.64.0.1")
    if IPV6:
        sh(f"ip -n {NS_GW} -6 route add default via fd00:64::1")
    sh(f"ip -n {NS_GW} addr add {GW_MGMT_IP}/30 dev {MGMT_GW}")
    sh(f"ip addr add 10.255.0.1/30 dev {MGMT_HOST}")

    sh(f"ip -n {NS_ISP} addr add 100.64.0.1/30 dev {ISP_GW}")
    if IPV6:
        sh(f"ip -n {NS_ISP} addr add fd00:64::1/64 dev {ISP_GW} nodad")
    sh(f"ip -n {NS_ISP} addr add 203.0.113.1/24 dev {ISP_SRV}")
    if IPV6:
        sh(f"ip -n {NS_ISP} addr add fd00:203::1/64 dev {ISP_SRV} nodad")
    sh(f"ip -n {NS_ISP} route add {LAN4}.0/24 via 100.64.0.2")
    if IPV6:
        sh(f"ip -n {NS_ISP} -6 route add {LAN6}/64 via fd00:64::2")

    sh(f"ip -n {NS_SRV} addr add {SERVER4}/24 dev {SRV_IF}")
    if IPV6:
        sh(f"ip -n {NS_SRV} addr add {SERVER6}/64 dev {SRV_IF} nodad")
    sh(f"ip -n {NS_SRV} route add default via 203.0.113.1")
    if IPV6:
        sh(f"ip -n {NS_SRV} -6 route add default via fd00:203::1")

    for ns in (NS_GW, NS_ISP):
        nsx(ns, "sysctl -qw net.ipv4.ip_forward=1")
        if IPV6:
            nsx(ns, "sysctl -qw net.ipv6.conf.all.forwarding=1")
    # Larger socket buffers so TCP is limited by the network, not the endpoints.
    # Hosts use CUBIC, the default congestion control of Windows, macOS, Android and Linux.
    for ns in ALL_NS:
        nsx(ns, f"sysctl -qw net.ipv4.tcp_congestion_control={cc}", check=False)
        nsx(ns, "sysctl -qw net.core.rmem_max=16777216 net.core.wmem_max=16777216", check=False)
        nsx(ns, "sysctl -qw net.ipv4.tcp_rmem='4096 131072 16777216'", check=False)
        nsx(ns, "sysctl -qw net.ipv4.tcp_wmem='4096 131072 16777216'", check=False)

    if P:
        nsx(NS_SRV, f"sysctl -qw net.ipv4.tcp_congestion_control={P['tcp']['server_cc']}", check=False)
    netem = has_qdisc("netem delay 1ms")
    state = {
        "down_mbit": down_mbit, "up_mbit": up_mbit,
        "isp_buffer_ms": isp_buffer_ms, "base_rtt_ms": base_rtt_ms if netem else 0.0,
        "netem": netem, "cake": has_qdisc("cake"), "fq_codel": has_qdisc("fq_codel"),
        "kernel": os.uname().release, "ipv6": IPV6, "tcp_cc": cc,
    }
    if P:
        state.update({"profile": P["name"], "server_cc": P["tcp"]["server_cc"], "delay_method": "accessemu (C, TUN)",
                      "base_rtt_ms": P["wan"]["down"]["base_ms"] + P["wan"]["up"]["base_ms"]})
    _write_state(state)
    set_isp_rate(down_mbit, up_mbit, isp_buffer_ms)
    if P:
        PR.write_access(seed=1)
        PR.write_capacity(seed=1, enabled=False)
        state = _read_state()
        state["accessemu_pid"] = _start_accessemu()
        state["capacity_pid"] = _start_capacity()
        _write_state(state)
        return state
    if base_rtt_ms > 0:
        half = base_rtt_ms / 2.0
        if netem:
            nsx(NS_SRV, f"tc qdisc replace dev {SRV_IF} root netem delay {half}ms limit 100000")
            nsx(NS_CL, f"tc qdisc replace dev {CL_IF} root netem delay {half}ms limit 100000")
            state["delay_method"] = "netem"
        else:
            state["delay_method"] = "userspace-tun"
            state["delayline_pid"] = _start_delayline(half, half)
        state["base_rtt_ms"] = base_rtt_ms
        _write_state(state)
    return state


def _start_delayline(down_ms: float, up_ms: float) -> int:
    """Fallback when sch_netem is missing: route ISP-forwarded packets through a TUN delay line."""
    import time as _t
    here = os.path.dirname(os.path.abspath(__file__))
    log = open("/tmp/intentqos-delayline.log", "w")
    p = subprocess.Popen(["ip", "netns", "exec", NS_ISP, sys.executable, os.path.join(here, "delayline.py"),
                          "--dev", "iq-delay", "--down-ms", str(down_ms), "--up-ms", str(up_ms),
                          "--lan", f"{LAN4}.0/24"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(50):
        if sh(f"ip -n {NS_ISP} link show iq-delay", check=False).returncode == 0:
            break
        _t.sleep(0.1)
    nsx(NS_ISP, "sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0", check=False)
    nsx(NS_ISP, "sysctl -qw net.ipv4.conf.iq-delay.rp_filter=0", check=False)
    sh(f"ip -n {NS_ISP} route add default dev iq-delay table 200")
    sh(f"ip -n {NS_ISP} rule add iif {ISP_GW} lookup 200 pref 100")
    sh(f"ip -n {NS_ISP} rule add iif {ISP_SRV} lookup 200 pref 101")
    return p.pid


def _policy_route_tun(dev: str) -> None:
    nsx(NS_ISP, "sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0", check=False)
    nsx(NS_ISP, f"sysctl -qw net.ipv4.conf.{dev}.rp_filter=0", check=False)
    sh(f"ip -n {NS_ISP} route add default dev {dev} table 200")
    sh(f"ip -n {NS_ISP} rule add iif {ISP_GW} lookup 200 pref 100")
    sh(f"ip -n {NS_ISP} rule add iif {ISP_SRV} lookup 200 pref 101")
    if IPV6:
        sh(f"ip -n {NS_ISP} -6 route add default dev {dev} table 200", check=False)
        sh(f"ip -n {NS_ISP} -6 rule add iif {ISP_GW} lookup 200 pref 100", check=False)
        sh(f"ip -n {NS_ISP} -6 rule add iif {ISP_SRV} lookup 200 pref 101", check=False)


def _start_accessemu() -> int:
    """Field-calibrated profile: the C access/Wi-Fi emulator replaces the constant delay line."""
    import time as _t
    here = os.path.dirname(os.path.abspath(__file__))
    binary = os.path.join(here, "accessemu")
    src = binary + ".c"
    if not os.path.exists(binary) or os.path.getmtime(binary) < os.path.getmtime(src):
        sh(f"gcc -O2 -o {binary} {src} -lm")
    log = open("/tmp/intentqos-accessemu.log", "w")
    p = subprocess.Popen(["ip", "netns", "exec", NS_ISP, binary, "iq-access", PR.ACCESS_CONF],
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(50):
        if sh(f"ip -n {NS_ISP} link show iq-access", check=False).returncode == 0:
            break
        _t.sleep(0.1)
    _t.sleep(0.3)
    _policy_route_tun("iq-access")
    return p.pid


def _start_capacity() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log = open("/tmp/intentqos-capacity.log", "w")
    p = subprocess.Popen([sys.executable, "-m", "testbed.capacity"], cwd=root, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return p.pid


def _buffer_bytes(rate_mbit: float, buffer_ms: float) -> int:
    # A "bloated" modem buffer sized in time at the *nominal* plan rate. When the
    # link degrades the same byte buffer becomes even more delay (realistic).
    return max(64_000, int(rate_mbit * 1e6 / 8 * buffer_ms / 1000.0))


def set_isp_rate(down_mbit: float, up_mbit: float, buffer_ms: float | None = None,
                 limit_down: int | None = None, limit_up: int | None = None) -> None:
    """Change the emulated access-link capacity (e.g. a 100 -> 20 Mbit/s drop).

    The buffer size in BYTES is kept from the nominal plan when the rate drops,
    exactly like a real modem whose buffer does not shrink with line rate."""
    st = _read_state()
    if buffer_ms is None:
        buffer_ms = st.get("isp_buffer_ms", 250.0)
    nominal_down = st.get("nominal_down", st.get("down_mbit", down_mbit))
    nominal_up = st.get("nominal_up", st.get("up_mbit", up_mbit))
    P = PR.active()
    factor = P["plan"]["rate_factor"] if P else 1.0
    lim_down = limit_down or _buffer_bytes(nominal_down, buffer_ms)
    lim_up = limit_up or _buffer_bytes(nominal_up, P["buffers"]["up_ms"] if P else buffer_ms)
    rd, ru = round(down_mbit * factor, 2), round(up_mbit * factor, 2)
    burst_d = max(16_000, int(rd * 1e6 / 8 / 250))   # ~4 ms worth of burst
    burst_u = max(16_000, int(ru * 1e6 / 8 / 250))
    nsx(NS_ISP, f"tc qdisc replace dev {ISP_GW} root tbf rate {rd}mbit burst {burst_d} limit {lim_down}")
    nsx(NS_ISP, f"tc qdisc replace dev {ISP_SRV} root tbf rate {ru}mbit burst {burst_u} limit {lim_up}")
    st.update({"down_mbit": down_mbit, "up_mbit": up_mbit, "nominal_down": nominal_down,
               "nominal_up": nominal_up, "isp_buffer_ms": buffer_ms, "rate_factor": factor,
               "limit_down": lim_down, "limit_up": lim_up})
    _write_state(st)


def reset_gateway() -> None:
    """Remove every shaping/marking rule from the gateway (the 'no QoS' state)."""
    for dev in (GW_LAN, GW_WAN):
        nsx(NS_GW, f"tc qdisc del dev {dev} root", check=False)
        nsx(NS_GW, f"tc qdisc del dev {dev} clsact", check=False)
    nsx(NS_GW, "nft delete table inet intentqos", check=False)


def down() -> None:
    st0 = _read_state()
    for key in ("delayline_pid", "accessemu_pid", "capacity_pid"):
        pid = st0.get(key)
        if pid:
            try:
                os.killpg(pid, 15)
            except (ProcessLookupError, PermissionError):
                pass
    sh("pkill -f testbed/delayline.py", check=False)
    sh("pkill -x accessemu", check=False)
    sh("pkill -f '^[^ ]*python3 -m testbed[.]capacity$'", check=False)
    for ns in ALL_NS:
        sh(f"ip netns del {ns}", check=False)
    sh(f"ip link del {MGMT_HOST}", check=False)
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass


def _read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _write_state(st: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)


def status() -> dict:
    st = _read_state()
    st["namespaces"] = [ns for ns in ALL_NS if sh(f"ip netns pids {ns}", check=False).returncode == 0]
    return st


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("up", help="create the testbed")
    p.add_argument("--down", type=float, default=100.0, help="ISP download Mbit/s (default 100)")
    p.add_argument("--up", type=float, default=20.0, help="ISP upload Mbit/s (default 20)")
    p.add_argument("--isp-buffer-ms", type=float, default=250.0,
                   help="modem buffer, in ms at the nominal rate (bloated default 250 ms)")
    p.add_argument("--base-rtt-ms", type=float, default=20.0, help="base RTT if netem exists")
    p.add_argument("--cc", default="cubic", help="TCP congestion control used by hosts (default cubic)")
    sub.add_parser("down", help="destroy the testbed")
    sub.add_parser("status")
    sub.add_parser("reset-gw", help="remove all QoS from the gateway")
    p = sub.add_parser("rate", help="change ISP access-link capacity")
    p.add_argument("--down", type=float, required=True)
    p.add_argument("--up", type=float, required=True)
    p = sub.add_parser("exec", help="run a command inside a namespace")
    p.add_argument("ns", choices=["cl", "gw", "isp", "srv"])
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("testbed must run as root (network namespaces)", file=sys.stderr)
        return 2
    if a.cmd == "up":
        print(json.dumps(up(a.down, a.up, a.isp_buffer_ms, a.base_rtt_ms, a.cc), indent=2))
    elif a.cmd == "down":
        down()
    elif a.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif a.cmd == "reset-gw":
        reset_gateway()
    elif a.cmd == "rate":
        set_isp_rate(a.down, a.up)
    elif a.cmd == "exec":
        ns = {"cl": NS_CL, "gw": NS_GW, "isp": NS_ISP, "srv": NS_SRV}[a.ns]
        os.execvp("ip", ["ip", "netns", "exec", ns] + a.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
