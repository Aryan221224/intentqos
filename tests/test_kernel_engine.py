"""Kernel flow engine (router/iq-flow.bpf.c + iq-capture -e) against the packet-ring helper, in network namespaces.

Needs root, clang, gcc, tc with BPF (libbpf) support and `ip netns`; skipped otherwise. Emulation only: veth
pairs and a Linux bridge on this machine's kernel, not router hardware.

    sudo python3 -m unittest tests.test_kernel_engine -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = f"k{os.getpid() % 10000}"
C, RT, S = f"{TAG}c", f"{TAG}r", f"{TAG}s"


def ns(n, *cmd, **kw):
    return subprocess.run(["ip", "netns", "exec", n, *cmd], capture_output=True, text=True, **kw)


def usable() -> str | None:
    if os.geteuid() != 0:
        return "needs root"
    for tool in ("clang", "gcc", "tc", "ip"):
        if not shutil.which(tool):
            return f"needs {tool}"
    if subprocess.run(["ip", "netns", "add", f"{TAG}probe"], capture_output=True).returncode:
        return "cannot create network namespaces"
    subprocess.run(["ip", "netns", "del", f"{TAG}probe"], capture_output=True)
    return None


WHY = usable()


@unittest.skipIf(WHY, WHY or "")
class KernelEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        inc = f"/usr/include/{os.uname().machine}-linux-gnu"
        cls.obj = os.path.join(cls.tmp, "iq-flow.bpf.o")
        cls.helper = os.path.join(cls.tmp, "iq-capture")
        r = subprocess.run(["clang", "-O2", "-g", "-target", "bpfel"] + (["-I", inc] if os.path.isdir(inc) else []) +
                           ["-c", os.path.join(ROOT, "router", "iq-flow.bpf.c"), "-o", cls.obj], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        r = subprocess.run(["gcc", "-O2", "-o", cls.helper, os.path.join(ROOT, "router", "iq-capture.c")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        for n in (C, RT, S):
            subprocess.run(["ip", "netns", "add", n], check=True)
            ns(n, "ip", "link", "set", "lo", "up")
        sh = lambda *a: subprocess.run(a, check=True, capture_output=True)   # noqa: E731
        sh("ip", "link", "add", "c0", "netns", C, "type", "veth", "peer", "name", "lan0", "netns", RT)
        sh("ip", "link", "add", "s0", "netns", S, "type", "veth", "peer", "name", "wan0", "netns", RT)
        for n, cmds in ((C, ["addr add 10.78.1.2/24 dev c0", "link set c0 up", "route add default via 10.78.1.1"]),
                        (S, ["addr add 10.78.2.2/24 dev s0", "link set s0 up", "route add default via 10.78.2.1"]),
                        (RT, ["link add brl type bridge", "link set lan0 master brl", "addr add 10.78.1.1/24 dev brl",
                              "link set lan0 up", "link set brl up", "addr add 10.78.2.1/24 dev wan0", "link set wan0 up"])):
            for c in cmds:
                r = ns(n, "ip", *c.split())
                assert r.returncode == 0, (c, r.stderr)
        ns(RT, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        for c in ("qdisc add dev brl root handle 1: htb default 30 r2q 100",
                  "class add dev brl parent 1: classid 1:10 htb rate 100mbit",
                  "class add dev brl parent 1: classid 1:30 htb rate 100mbit"):
            ns(RT, "tc", *c.split())
        cls.srv = subprocess.Popen(["ip", "netns", "exec", S, sys.executable, "-c",
                                    "import socket,time\ns=socket.socket();s.setsockopt(1,2,1);s.bind(('0.0.0.0',5001));"
                                    "s.listen();c,_=s.accept();b=b'x'*65536;e=time.time()+8\n"
                                    "while time.time()<e:\n  try: c.send(b);time.sleep(0.004)\n  except OSError: break"])
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.kill()
        for n in (C, RT, S):
            subprocess.run(["ip", "netns", "del", n], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def helper_proc(self, *extra):
        return subprocess.Popen(["ip", "netns", "exec", RT, self.helper, *extra, "-i", "brl", "-n", "10.78.1.0/24"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    @staticmethod
    def roll(p):
        p.stdin.write("R\n")
        p.stdin.flush()
        out = []
        while True:
            ln = p.stdout.readline()
            if not ln or ln[0] == "E":
                return out
            out.append(ln.split())

    def classes(self):
        out, res, cur = ns(RT, "tc", "-s", "class", "show", "dev", "brl").stdout, {}, None
        for ln in out.splitlines():
            if ln.startswith("class htb"):
                cur = ln.split()[2]
            elif "Sent" in ln and cur:
                res[cur] = int(ln.split()[1])
        return res

    def test_same_counts_and_round_trips_and_verdicts_apply(self):
        ring = self.helper_proc()
        kern = self.helper_proc("-e", self.obj, "-w", "wan0")
        time.sleep(0.8)
        self.assertIsNone(kern.poll(), kern.stderr.read() if kern.poll() is not None else "")
        kern.stdin.write("P 0 65552 255 65552 255 65584 255 65584 255 65584 255\n")
        kern.stdin.flush()
        cl = subprocess.Popen(["ip", "netns", "exec", C, sys.executable, "-c",
                               "import socket,time\nc=socket.create_connection(('10.78.2.2',5001),"
                               "source_address=('10.78.1.2',40100));e=time.time()+5\n"
                               "while time.time()<e and c.recv(65536): pass"])
        tot = {"ring": {"pk_dn": 0, "rw_n": 0, "rw_s": 0.0}, "kern": {"pk_dn": 0, "rw_n": 0, "rw_s": 0.0}}
        before = None
        for i in range(8):
            time.sleep(0.5)
            for name, p in (("ring", ring), ("kern", kern)):
                for w in self.roll(p):
                    if w[0] == "W" and w[6] == "5001":
                        tot[name]["pk_dn"] += int(w[17])
                        tot[name]["rw_n"] += int(w[37])
                        tot[name]["rw_s"] += float(w[38])
            if i == 2:
                kern.stdin.write("M 4 10.78.1.2 10.78.2.2 6 40100 5001 1\n")
                kern.stdin.flush()
                time.sleep(0.2)
                before = self.classes()
        after = self.classes()
        cl.wait(timeout=10)
        for p in (ring, kern):
            p.stdin.write("Q\n")
            p.stdin.flush()
            p.wait(timeout=5)
        r, k = tot["ring"], tot["kern"]
        self.assertGreater(r["pk_dn"], 500)
        self.assertLess(abs(r["pk_dn"] - k["pk_dn"]) / r["pk_dn"], 0.05)          # same packets, window edges aside
        self.assertGreater(k["rw_n"], 50)
        self.assertLess(abs(r["rw_s"] / r["rw_n"] - k["rw_s"] / k["rw_n"]), 1.0)   # internet RTT within 1 ms
        moved = after["1:10"] - before["1:10"]
        stayed = after["1:30"] - before["1:30"]
        self.assertGreater(moved, 10 * max(1, stayed))                             # the verdict picked the class
        self.assertFalse(os.path.exists("/sys/fs/bpf/tc/globals/iq_flows") and
                         ns(RT, "tc", "filter", "show", "dev", "lan0", "ingress").stdout.strip())   # detached on exit


if __name__ == "__main__":
    unittest.main()
