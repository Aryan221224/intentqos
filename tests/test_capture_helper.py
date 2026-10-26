"""The native capture helper must produce the same per-flow aggregates as the Python FlowTable.

Sends identical synthetic frames through a veth pair to both implementations and compares every
window field. Needs root (network namespace + raw sockets) and a built router/iq-capture; skipped
otherwise.

    sudo python3 -m unittest tests.test_capture_helper -v
"""
from __future__ import annotations

import os
import random
import socket
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from intentqos.capture import FlowTable, HelperCapture, parse_frame  # noqa: E402
from tests.test_modules import eth_ipv4  # noqa: E402

HELPER = os.path.join(ROOT, "router", "iq-capture")
NS = "iqtest-cap"


def sh(cmd: str) -> None:
    subprocess.run(cmd, shell=True, check=True, capture_output=True)


@unittest.skipUnless(os.geteuid() == 0 and os.path.exists(HELPER), "needs root and router/iq-capture")
class TestHelperMatchesPython(unittest.TestCase):
    def setUp(self):
        subprocess.run(f"ip netns del {NS}", shell=True, capture_output=True)
        sh(f"ip netns add {NS}")
        sh("ip link add iqta type veth peer name iqtb")
        sh(f"ip link set iqta netns {NS}")
        sh(f"ip link set iqtb netns {NS}")
        sh(f"ip -n {NS} link set iqta up")
        sh(f"ip -n {NS} link set iqtb up")

    def tearDown(self):
        subprocess.run(f"ip netns del {NS}", shell=True, capture_output=True)

    def test_same_aggregates(self):
        rnd = random.Random(7)
        frames = []
        for i in range(400):
            up = rnd.random() < 0.4
            lan, rem = "192.168.9.20", "198.51.100.7"
            size = rnd.choice([60, 172, 600, 1400, 1500])
            proto = 17 if i % 3 == 0 else 6
            sp, dp = (5004, 3478) if proto == 17 else (40000 + i % 3, 443)
            if up:
                frames.append(eth_ipv4(lan, rem, proto, sp, dp, size, flags=0x10))
            else:
                frames.append(eth_ipv4(rem, lan, proto, dp, sp, size, flags=0x10))
        # the helper listens on b inside the namespace; we send on a from inside the namespace too
        table_h = FlowTable(["192.168.9.0/24"])
        cmd = ["ip", "netns", "exec", NS, HELPER, "-i", "iqtb", "-n", "192.168.9.0/24"]
        h = HelperCapture("iqtb", table_h, HELPER, ["192.168.9.0/24"])
        h.args = cmd
        h.start()
        try:
            sender = subprocess.Popen(["ip", "netns", "exec", NS, sys.executable, "-c",
                                       "import socket,sys,pickle,time\n"
                                       "fr=pickle.load(sys.stdin.buffer)\n"
                                       "s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW)\n"
                                       "s.bind(('iqta',0))\n"
                                       "[ (s.send(f), time.sleep(0.0005)) for f in fr ]\n"],
                                      stdin=subprocess.PIPE)
            import pickle
            sender.communicate(pickle.dumps(frames))
            time.sleep(0.3)
            h.roll(table_h, time.monotonic())
        finally:
            h.stop()
        # Python reference: same frames through parse_frame + FlowTable.add
        table_p = FlowTable(["192.168.9.0/24"])
        t = 100.0                       # any non-zero start (0.0 would read as 'no previous packet')
        for fr in frames:
            p = parse_frame(fr, min(len(fr), 96), len(fr))
            table_p.add(p, t)
            t += 0.0005
        table_p.roll_windows(t)
        self.assertEqual(set(table_h.flows), set(table_p.flows))
        for k, fp in table_p.flows.items():
            fh = table_h.flows[k]
            wp, wh = fp.hist[-1], fh.hist[-1]
            for field in ("pk_up", "by_up", "sq_up", "mx_up", "pk_dn", "by_dn", "sq_dn", "mx_dn", "small", "large",
                          "iat_n_up", "iat_n_dn"):
                self.assertEqual(getattr(wh, field), getattr(wp, field), (k, field))
            self.assertEqual((fh.pk_up, fh.pk_dn, fh.by_up, fh.by_dn), (fp.pk_up, fp.pk_dn, fp.by_up, fp.by_dn))
            self.assertEqual([(s, d) for _, s, d in fh.early], [(s, d) for _, s, d in fp.early])


if __name__ == "__main__":
    unittest.main()
