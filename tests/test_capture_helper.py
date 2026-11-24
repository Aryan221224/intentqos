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


def eth6(src: str, dst: str, kind: str, sport: int, dport: int, size: int) -> bytes:
    """An IPv6 frame of `size` bytes on the wire: plain UDP, TCP, or UDP behind hop-by-hop, AH or fragment headers."""
    import struct
    l4p = 6 if kind == "tcp" else 17
    ext, first = b"", l4p
    if kind == "hbh":
        first, ext = 0, bytes([l4p, 0]) + b"\x01\x04\x00\x00\x00\x00"
    elif kind == "ah":
        first, ext = 51, bytes([l4p, 4]) + b"\x00" * 22                # (4 + 2) * 4 = 24 bytes
    elif kind in ("frag0", "fragN"):
        off = 0 if kind == "frag0" else 1232
        first, ext = 44, struct.pack("!BBHI", l4p, 0, off | 1, 0x1234)
    if kind == "fragN":
        l4 = b"\x9a\x9b\x9c\x9d\x9e\x9f\xa0\xa1"                  # payload, not a UDP header
    elif l4p == 6:
        l4 = struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 0x50, 0x10, 1000, 0, 0)
    else:
        l4 = struct.pack("!HHHH", sport, dport, 8, 0)
    pad = max(0, size - 14 - 40 - len(ext) - len(l4))
    ip = struct.pack("!IHBB16s16s", 0x60000000, len(ext) + len(l4) + pad, first, 64,
                     socket.inet_pton(socket.AF_INET6, src), socket.inet_pton(socket.AF_INET6, dst))
    return b"\x02" * 6 + b"\x04" * 6 + b"\x86\xdd" + ip + ext + l4 + b"\x00" * pad


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
            for field in ("ect0", "ect1", "ce", "dscp_up", "retx_up", "retx_dn"):
                self.assertEqual(getattr(wh, field), getattr(wp, field), (k, field))

    def test_same_aggregates_ipv6(self):
        """IPv6 with extension headers: hop-by-hop, AH, and fragments (first and non-first) must agree too.
        Raw Ethernet frames on a veth pair: no IPv6 stack is needed on the test machine."""
        lan, rem = "fd00:9::20", "2001:db8:7::7"
        frames = []
        for i in range(240):
            up = i % 5 < 2
            kind = ("plain", "hbh", "ah", "frag0", "fragN", "tcp")[i % 6]
            sz = (96, 600, 1280, 1400)[i % 4]
            s_, d_ = (lan, rem) if up else (rem, lan)
            sp, dp = (51000, 3478) if up else (3478, 51000)
            frames.append(eth6(s_, d_, kind, sp, dp, sz))
        table_h = FlowTable(["fd00:9::/64"])
        h = HelperCapture("iqtb", table_h, HELPER, ["fd00:9::/64"])
        h.args = ["ip", "netns", "exec", NS, HELPER, "-i", "iqtb", "-n", "fd00:9::/64"]
        h.start()
        try:
            import pickle
            sender = subprocess.Popen(["ip", "netns", "exec", NS, sys.executable, "-c",
                                       "import socket,sys,pickle,time\n"
                                       "fr=pickle.load(sys.stdin.buffer)\n"
                                       "s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW)\n"
                                       "s.bind(('iqta',0))\n"
                                       "[ (s.send(f), time.sleep(0.0005)) for f in fr ]\n"],
                                      stdin=subprocess.PIPE)
            sender.communicate(pickle.dumps(frames))
            time.sleep(0.3)
            h.roll(table_h, time.monotonic())
        finally:
            h.stop()
        table_p = FlowTable(["fd00:9::/64"])
        t = 100.0
        for fr in frames:
            table_p.add(parse_frame(fr, min(len(fr), 96), len(fr)), t)
            t += 0.0005
        table_p.roll_windows(t)
        self.assertEqual(set(table_h.flows), set(table_p.flows))
        # UDP 51000<->3478 (plain, hop-by-hop, AH and first fragments), TCP, and the non-first fragments
        # as their own port-less entry: never a bogus "protocol 44" flow, never payload read as ports
        self.assertEqual(sorted((k[2], k[3], k[4]) for k in table_p.flows), [(6, 51000, 3478), (17, 0, 0), (17, 51000, 3478)])
        for k, fp in table_p.flows.items():
            fh = table_h.flows[k]
            self.assertEqual((fh.pk_up, fh.pk_dn, fh.by_up, fh.by_dn), (fp.pk_up, fp.pk_dn, fp.by_up, fp.by_dn), k)

    def test_same_experience_fields(self):
        """TCP-timestamp RTT, ECN, DSCP, retransmissions and QUIC spin: counts must agree exactly."""
        from tests.test_innovations import experience_frames
        fr = experience_frames()
        table_h = FlowTable(["192.168.9.0/24"])
        h = HelperCapture("iqtb", table_h, HELPER, ["192.168.9.0/24"])
        h.args = ["ip", "netns", "exec", NS, HELPER, "-i", "iqtb", "-n", "192.168.9.0/24"]
        h.start()
        try:
            import pickle
            sender = subprocess.Popen(["ip", "netns", "exec", NS, sys.executable, "-c",
                                       "import socket,sys,pickle,time\n"
                                       "fr=pickle.load(sys.stdin.buffer)\n"
                                       "s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW)\n"
                                       "s.bind(('iqta',0))\n"
                                       "t0=time.monotonic()\n"
                                       "for dt,f in fr:\n"
                                       "    time.sleep(max(0,t0+dt-time.monotonic())); s.send(f)\n"],
                                      stdin=subprocess.PIPE)
            sender.communicate(pickle.dumps(fr))
            time.sleep(0.3)
            h.roll(table_h, time.monotonic())
        finally:
            h.stop()
        tcp = table_h.flows[("192.168.9.20", "198.51.100.7", 6, 40001, 443)].hist[-1]
        self.assertEqual((tcp.rw_n, tcp.rl_n, tcp.ect1, tcp.ce, tcp.dscp_up, tcp.retx_dn), (1, 1, 1, 1, 46, 1))
        self.assertAlmostEqual(tcp.rw_s, 40.0, delta=15.0)
        quic = table_h.flows[("192.168.9.20", "198.51.100.7", 17, 50000, 443)].hist[-1]
        self.assertEqual(quic.sp_n, 7)
        self.assertAlmostEqual(quic.sp_s / quic.sp_n, 30.0, delta=10.0)


if __name__ == "__main__":
    unittest.main()
