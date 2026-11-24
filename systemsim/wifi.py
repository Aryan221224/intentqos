#!/usr/bin/env python3
"""Home Wi-Fi between the router's two radio ports and the wireless stations (EMULATION; see wifiemu.c).

    wifi.py setup                    # build TAPs, start wifiemu, hand each station its eth0
    wifi.py degrade NAME MS LOSS     # e.g. the laptop moves two rooms away: +MS access delay, LOSS loss
    wifi.py restore NAME
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from world import DEVICES, PROFILE, WORK, bg, no_offload, sh  # noqa: E402

CONF = os.path.join(WORK, "wifiemu.conf")
BAND = {"br-w5": 5, "br-w24": 24}


def stations() -> list[tuple[int, str, str, int, str]]:
    out = []
    for i, (name, d) in enumerate((n, d) for n, d in DEVICES.items() if d[1] in BAND):
        out.append((i, name, d[0], BAND[d[1]], d[2]))
    return out


def write_conf(degraded: dict | None = None) -> None:
    degraded = degraded or {}
    nb = {5: PROFILE["wifi"]["neighbour_busy"]["5"]["median"], 24: PROFILE["wifi"]["neighbour_busy"]["24"]["median"]}
    psm = set(PROFILE["wifi"].get("psm", []))
    lines = ["ap5=ap5", "ap24=ap24"] + [f"{k}={v}" for k, v in PROFILE["wifi"]["params"].items()]
    for i, name, _ns, band, mac in stations():
        dg = degraded.get(name, {})
        lines.append(f"sta={i},ws-{name}"[:64] + f",{band},{mac},{nb[band]},{1 if name in psm else 0},"
                     f"{dg.get('extra_ms', 0)},{dg.get('loss', -1)}")
    tmp = CONF + ".tmp"
    open(tmp, "w").write("\n".join(lines) + "\n")
    os.replace(tmp, CONF)
    json.dump(degraded, open(CONF + ".degraded.json", "w"))


def setup() -> int:
    binary = os.path.join(HERE, "wifiemu")
    if not os.path.exists(binary) or os.path.getmtime(binary) < os.path.getmtime(binary + ".c"):
        sh(f"gcc -O2 -Wall -o {binary} {binary}.c -lm")
    sh("ip tuntap add dev ap5 mode tap"); sh("ip link set ap5 master br-w5 up"); no_offload("ap5")
    sh("ip tuntap add dev ap24 mode tap"); sh("ip link set ap24 master br-w24 up"); no_offload("ap24")
    for _i, name, _ns, _b, _mac in stations():
        sh(f"ip tuntap add dev ws-{name} mode tap")
    write_conf()
    pid = bg(None, f"{binary} {CONF}", "wifiemu.log")
    for _ in range(50):
        if "ports" in open(os.path.join(WORK, "logs", "wifiemu.log")).read():
            break
        time.sleep(0.1)
    for _i, name, ns, _b, mac in stations():
        sh(f"ip link set ws-{name} netns {ns}")
        sh(f"ip -n {ns} link set ws-{name} name eth0")
        sh(f"ip -n {ns} link set eth0 address {mac} mtu 1500 up")
        no_offload("eth0", ns)
    return pid


def restart() -> int:
    """Restart wifiemu (e.g. after a rebuild): bring each station's eth0 home, reattach, hand it back."""
    sh("pkill -x wifiemu")
    time.sleep(0.5)
    for _i, name, ns, _b, _mac in stations():
        sh(f"ip -n {ns} link set eth0 down")
        sh(f"ip -n {ns} link set eth0 name ws-{name}")
        sh(f"ip -n {ns} link set ws-{name} netns 1")
    pid = bg(None, f"{os.path.join(HERE, 'wifiemu')} {CONF}", "wifiemu.log")
    time.sleep(1)
    for _i, name, ns, _b, mac in stations():
        sh(f"ip link set ws-{name} netns {ns}")
        sh(f"ip -n {ns} link set ws-{name} name eth0")
        sh(f"ip -n {ns} link set eth0 address {mac} mtu 1500 up")
        no_offload("eth0", ns)
    return pid


def degrade(name: str, extra_ms: float, loss: float) -> None:
    try:
        dg = json.load(open(CONF + ".degraded.json"))
    except (OSError, ValueError):
        dg = {}
    if extra_ms <= 0 and loss < 0:
        dg.pop(name, None)
    else:
        dg[name] = {"extra_ms": extra_ms, "loss": loss}
    time.sleep(1.1)  # wifiemu notices changes by modification time (1 s resolution)
    write_conf(dg)
    with open(os.path.join(WORK, "logs", "wifi-control.jsonl"), "a") as f:
        f.write(json.dumps({"t": time.time(), "station": name, "extra_ms": extra_ms, "loss": loss}) + "\n")


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "setup":
        print(setup())
    elif c == "restart":
        print(restart())
    elif c == "degrade":
        degrade(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]))
    elif c == "restore":
        degrade(sys.argv[2], 0, -1)
