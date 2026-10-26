#!/usr/bin/env python3
"""Userspace propagation-delay emulator (a tiny "netem delay" replacement).

Some minimal kernels (including the CI sandbox this project was validated in)
ship without `sch_netem`. Base RTT matters for realistic TCP behaviour: with
~0 ms RTT, receive-window autotuning stays small and bufferbloat is understated.
This delay line runs in the ISP namespace: every forwarded packet is routed
into a TUN device, held for a fixed one-way delay, and re-injected; policy
routing (`iif <tun>` -> table 100) sends re-injected packets to the real egress.

It adds a constant delay only (no jitter, no loss) -- the access link's
bottleneck queue (tbf, bloated buffer) remains the only source of queueing.
Labelled clearly as EMULATION, per the brief.
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import ipaddress
import os
import struct
import sys
import threading
import time

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


def open_tun(name: str) -> int:
    fd = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI))
    return fd


def run(name: str, down_ms: float, up_ms: float, lan: str) -> None:
    lan_net = ipaddress.ip_network(lan)
    # The emulated network must not be perturbed by the system under test sharing the CPU:
    # run the delay line with real-time priority, like netem running in kernel softirq context.
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
    except (PermissionError, OSError, AttributeError):
        pass
    fd = open_tun(name)
    os.system(f"ip link set {name} up mtu 1500")
    os.system(f"ip link set dev {name} txqueuelen 10000")
    q: collections.deque = collections.deque()
    cv = threading.Condition()
    dn_s, up_s = down_ms / 1000.0, up_ms / 1000.0

    def reader():
        while True:
            pkt = os.read(fd, 65535)
            if len(pkt) < 20 or (pkt[0] >> 4) != 4:
                os.write(fd, pkt)  # non-IPv4: pass through
                continue
            dst = ipaddress.IPv4Address(pkt[16:20])
            d = dn_s if dst in lan_net else up_s
            with cv:
                q.append((time.monotonic() + d, pkt))
                cv.notify()

    def writer():
        while True:
            with cv:
                while not q:
                    cv.wait()
                t, pkt = q[0]
                now = time.monotonic()
                if t > now:
                    cv.wait(timeout=t - now)
                    continue
                q.popleft()
            try:
                os.write(fd, pkt)
            except OSError:
                pass

    threading.Thread(target=reader, daemon=True).start()
    print(f"delay line {name}: down {down_ms} ms, up {up_ms} ms", flush=True)
    writer()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dev", default="iq-delay")
    ap.add_argument("--down-ms", type=float, default=10.0)
    ap.add_argument("--up-ms", type=float, default=10.0)
    ap.add_argument("--lan", default="192.168.50.0/24")
    a = ap.parse_args(argv)
    run(a.dev, a.down_ms, a.up_ms, a.lan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
