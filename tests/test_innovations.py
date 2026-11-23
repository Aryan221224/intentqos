"""Tests for the IntentQoS 2.0 additions (stdlib only, no root, no network).

    python3 -m unittest tests.test_innovations -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from intentqos import config as C  # noqa: E402
from intentqos import ed25519, release  # noqa: E402


class ReleaseEnvelopes(unittest.TestCase):
    """Signed instructions: freshness, gateway binding, replay and threshold signatures."""

    def setUp(self):
        self.seed, self.pub = ed25519.keypair(b"\x01" * 32)
        self.body = {"version": 3, "profile": "default v1", "set": {"link.down_mbit": 100}}

    def env(self, seq=5, device="gw-1", now=None, ttl=900):
        return release.make_instruction(self.seed, "config", device, seq, self.body, now=now, ttl=ttl)

    def test_valid_instruction_passes(self):
        self.assertIsNone(release.check_instruction(self.env(), "config", "gw-1", self.pub, 4))

    def test_replay_of_an_older_instruction_is_refused(self):
        why = release.check_instruction(self.env(seq=4), "config", "gw-1", self.pub, 4)
        self.assertIn("stale", why)

    def test_expired_instruction_is_refused(self):
        why = release.check_instruction(self.env(now=time.time() - 7200), "config", "gw-1", self.pub, 0)
        self.assertIn("expired", why)

    def test_other_gateway_and_tampering_are_refused(self):
        self.assertIn("another gateway", release.check_instruction(self.env(device="gw-2"), "config", "gw-1", self.pub, 0))
        e = self.env()
        e["body"] = {**e["body"], "set": {"link.down_mbit": 1}}
        self.assertIn("signature", release.check_instruction(e, "config", "gw-1", self.pub, 0))
        self.assertIn("not 'model'", release.check_instruction(self.env(), "model", "gw-1", self.pub, 0))

    def test_threshold_needs_distinct_trusted_keys(self):
        seeds = [bytes([i]) * 32 for i in (2, 3, 4)]
        pubs = [ed25519.keypair(s)[1] for s in seeds]
        meta = {"version": "m1", "sha256": "ab", "size": 10}
        one = [release.sign_with(seeds[0], meta)]
        self.assertFalse(release.verify_threshold(meta, one + one, pubs, 2)[0])     # the same key twice counts once
        self.assertTrue(release.verify_threshold(meta, one + [release.sign_with(seeds[2], meta)], pubs, 2)[0])
        stranger = release.sign_with(b"\x09" * 32, meta)
        self.assertFalse(release.verify_threshold(meta, one + [stranger], pubs, 2)[0])

    def test_agent_refuses_replayed_config(self):
        from intentqos.agent import Agent
        d = tempfile.mkdtemp()
        cfg = C.load(None, {"agent": {"state_path": os.path.join(d, "agent.json"), "release_pubkey": self.pub.hex()},
                            "api": {"key_file": os.path.join(d, "admin.key")}})
        a = Agent(cfg)
        a.st["device_id"] = "gw-1"
        calls = []
        a._engine = lambda m, p, b=None, timeout=5.0: (calls.append(p), (200, {"ok": True}))[1]
        env = self.env(seq=7)
        self.assertTrue(a.execute({"kind": "apply_config", "args": {"envelope": env}})["ok"])
        self.assertEqual(a.st["release_seq"], 7)
        r = a.execute({"kind": "apply_config", "args": {"envelope": env}})          # the same instruction again
        self.assertFalse(r["ok"])
        self.assertIn("replay", r["error"])
        self.assertEqual(calls, ["/api/config"])


def eth_tcp(src, dst, sport, dport, total_len, seq=0, ts=None, tos=0, flags=0x18):
    """Ethernet + IPv4 + TCP with an optional timestamp option (NOP NOP TS) and a chosen TOS byte."""
    import socket
    import struct
    eth = b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00"
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, tos, total_len, 1, 0, 64, 6, 0, socket.inet_aton(src), socket.inet_aton(dst))
    opts = (b"\x01\x01\x08\x0a" + struct.pack("!II", *ts)) if ts else b""
    doff = (20 + len(opts)) // 4
    l4 = struct.pack("!HHIIBBHHH", sport, dport, seq, 0, doff << 4, flags, 1000, 0, 0) + opts
    return eth + ip + l4 + b"P" * 8


def eth_udp(src, dst, sport, dport, total_len, first=0x40, tos=0):
    import socket
    import struct
    eth = b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00"
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, tos, total_len, 1, 0, 64, 17, 0, socket.inet_aton(src), socket.inet_aton(dst))
    return eth + ip + struct.pack("!HHHH", sport, dport, total_len - 20, 0) + bytes([first]) + b"Q" * 7


LAN, REM = "192.168.9.20", "198.51.100.7"


def experience_frames():
    """(t, frame) pairs: a TCP flow with timestamps, a retransmission, ECN marks; a QUIC flow with a spinning bit."""
    fr = []
    # device -> server data, stamp 100; server ACK echoes it 40 ms later (internet RTT 40 ms)
    fr.append((0.000, eth_tcp(LAN, REM, 40001, 443, 1000, seq=1000, ts=(100, 7), tos=0xB8 | 0x01)))  # EF + ECT(1)
    fr.append((0.040, eth_tcp(REM, LAN, 443, 40001, 52, seq=5000, ts=(900, 100))))
    # server -> device data, stamp 901; device echoes it 3 ms later (LAN RTT 3 ms)
    fr.append((0.050, eth_tcp(REM, LAN, 443, 40001, 1400, seq=5000, ts=(901, 101), tos=0x03)))      # CE
    fr.append((0.053, eth_tcp(LAN, REM, 40001, 443, 52, seq=1960, ts=(102, 901))))
    # the same server segment again: a retransmission
    fr.append((0.090, eth_tcp(REM, LAN, 443, 40001, 1400, seq=5000, ts=(903, 101))))
    # QUIC short-header packets from the server, spin bit flipping every 30 ms
    for k in range(8):
        fr.append((0.100 + 0.030 * k, eth_udp(REM, LAN, 443, 50000, 1200, first=0x40 | ((k % 2) << 5))))
    return fr


class Experience(unittest.TestCase):
    def table(self):
        from intentqos.capture import FlowTable, parse_frame
        t = FlowTable(["192.168.9.0/24"])
        for dt, f in experience_frames():
            t.add(parse_frame(bytearray(f[:96]), min(len(f), 96), len(f)), 100.0 + dt)
        t.roll_windows(101.0)
        return t

    def test_rtt_split_ecn_retx_and_spin(self):
        t = self.table()
        tcp = t.flows[(LAN, REM, 6, 40001, 443)].hist[-1]
        self.assertEqual((tcp.rw_n, tcp.rl_n), (1, 1))
        self.assertAlmostEqual(tcp.rw_s, 40.0, places=3)        # router -> server -> router
        self.assertAlmostEqual(tcp.rl_s, 3.0, places=3)         # router -> device -> router
        self.assertEqual((tcp.ect1, tcp.ce, tcp.dscp_up), (1, 1, 46))
        self.assertEqual((tcp.retx_dn, tcp.retx_up), (1, 0))
        quic = t.flows[(LAN, REM, 17, 50000, 443)].hist[-1]
        self.assertEqual(quic.sp_n, 7)
        self.assertAlmostEqual(quic.sp_s / quic.sp_n, 30.0, places=3)


class QoOAndDeltaQ(unittest.TestCase):
    def test_interpolation_and_minimum(self):
        from intentqos import qoo
        self.assertEqual(qoo.interp(40, 60, 300), 100.0)
        self.assertEqual(qoo.interp(300, 60, 300), 0.0)
        self.assertAlmostEqual(qoo.interp(180, 60, 300), 50.0)
        self.assertAlmostEqual(qoo.interp(3000, 5000, 1000), 50.0)          # throughput: more is better
        r = qoo.score(qoo.DEFAULT_REQUIREMENTS["voip"], {50: 60, 99: 325}, 0.1)
        self.assertEqual((r["qoo"], r["worst"]), (50.0, "p99"))              # the worst requirement decides
        self.assertIsNone(qoo.score(qoo.DEFAULT_REQUIREMENTS["voip"], None)["qoo"])

    def test_config_overrides(self):
        from intentqos import qoo
        req = qoo.requirements({"qoo": {"voip": {"latency": {"50": [40, 200]}, "loss": [1, 4]}}})
        self.assertEqual(req["voip"]["latency"], {50: (40, 200)})
        self.assertEqual(req["voip"]["loss"], (1, 4))
        self.assertIn("gaming", req)

    def test_composition_and_queue_model(self):
        from intentqos.deltaq import Dist, compose, queue_model
        c = compose(Dist([2.0]), Dist([10.0], loss=0.01), Dist([20.0], loss=0.02))
        self.assertAlmostEqual(c.pct(50), 32.0)
        self.assertAlmostEqual(c.loss, 1 - 0.99 * 0.98)
        light, heavy = queue_model(1000, 10000), queue_model(12000, 10000)
        self.assertLess(light.pct(99), heavy.pct(50))
        self.assertEqual(light.loss, 0.0)
        self.assertGreater(heavy.loss, 0.1)
        g = Dist([20, 21, 25, 40]).gsv(size_ms=0.5)
        self.assertEqual((g["g"], g["v_p99"]), (19.5, 20))


class ExperienceSessions(unittest.TestCase):
    def test_app_session_gets_measured_latency_and_qoo(self):
        from intentqos.capture import FlowTable, parse_frame
        from intentqos.experience import ExperienceTracker

        class Dec:
            committed, label = "web", "web"
        t = FlowTable(["192.168.9.0/24"])
        tr = ExperienceTracker(C.load(None))
        now = 100.0
        for k in range(6):                         # six windows of a TCP flow with 40 ms internet / 3 ms LAN RTT
            for dt, f in experience_frames()[:4]:
                fr = bytearray(f[:96])
                # keep stamps new each window so every window yields one sample of each
                fr[66:70] = (int.from_bytes(fr[66:70], "big") + 1000 * k).to_bytes(4, "big")
                fr[70:74] = ((int.from_bytes(fr[70:74], "big") + 1000 * k) if int.from_bytes(fr[70:74], "big") > 50
                             else int.from_bytes(fr[70:74], "big")).to_bytes(4, "big")
                t.add(parse_frame(fr, min(len(f), 96), len(f)), now + dt)
            now += 0.5
            t.roll_windows(now)
            flows = list(t.flows.values())
            snap = tr.update(flows, {f.id(): Dec() for f in flows}, {f.key: 3 for f in flows})
        app = next(a for a in snap["apps"] if a["label"] == "web")
        self.assertEqual(app["latency_source"], "measured")
        self.assertGreater(app["latency_ms"][50], 30)
        self.assertIsNotNone(app["qoo"])
        self.assertIn("192.168.9.20", snap["devices"])

    def test_call_frames_per_second(self):
        from intentqos.capture import Window
        from intentqos.experience import ExperienceTracker, FlowXP
        xp = FlowXP()
        xp.fr_dn, xp.fr_up, xp.vid_win = [15] * 8, [12] * 8, [40] * 8   # 15 frames per half second = 30 fps
        c = ExperienceTracker._call([(None, xp)])
        self.assertEqual((c["kind"], c["fps_down"], c["tier"]), ("video", 30.0, "smooth"))
        xp.fr_dn, xp.fr_up = [0] * 8, [0] * 8
        self.assertEqual(ExperienceTracker._call([(None, xp)])["kind"], "voice")
        self.assertIsNotNone(Window().rw_n == 0)


class WifiLocator(unittest.TestCase):
    def test_sundaresan_rule(self):
        from intentqos.wifi import BottleneckLocator
        b = BottleneckLocator(C.load(None))
        self.assertEqual(b.judge(0.3, 50, 40)["where"], "idle")
        self.assertEqual(b.judge(1.2, 500, 40)["where"], "none")
        self.assertEqual(b.judge(0.3, 500, 40)["where"], "wifi")
        self.assertEqual(b.judge(0.3, 500, 4)["where"], "line")

    def test_station_dump_and_qos_map(self):
        from intentqos.wifi import Stations, qos_map_set
        txt = ("Station aa:bb:cc:dd:ee:ff (on phy0-ap0)\n\tsignal:  \t-74 [-76, -77] dBm\n"
               "\ttx bitrate:\t26.0 MBit/s MCS 3\n\ttx packets:\t1000\n\ttx retries:\t300\n"
               "\texpected throughput:\t18.5Mbps\n")
        st = Stations.parse(txt, "phy0-ap0")["aa:bb:cc:dd:ee:ff"]
        self.assertEqual((st["signal_dbm"], st["tx_mbit"], st["retry_pct"], st["expected_mbit"]), (-74, 26.0, 30.0, 18.5))
        nums = [int(x) for x in qos_map_set().split(",")]
        self.assertEqual(len(nums), 21 * 2 + 16)                 # 21 exceptions, 8 ranges
        pairs = dict(zip(nums[0:42:2], nums[1:42:2]))
        self.assertEqual((pairs[46], pairs[34], pairs[8], pairs[48]), (6, 4, 1, 0))   # EF->voice, AF41->video, CS1->bk

    def test_cvt(self):
        from intentqos.capture import Window
        from intentqos.wifi import cvt_of
        w = Window()
        w.iat_n_dn, w.iat_s_dn, w.iat_q_dn, w.pk_dn = 100, 0.1, 0.1 * 0.001, 101   # every gap exactly 1 ms
        cv, pps = cvt_of([w])
        self.assertAlmostEqual(cv, 0.0, places=3)
        self.assertAlmostEqual(pps, 202.0)


class OutcomeControl(unittest.TestCase):
    def plan(self, g_voice):
        from intentqos import TIN_BE, TIN_BULK, TIN_INTENT, TIN_VIDEO, TIN_VOICE
        g = {TIN_INTENT: 8.0, TIN_VOICE: g_voice, TIN_VIDEO: 500.0, TIN_BE: 3000.0, TIN_BULK: 1000.0}
        return {d: {"rate": 10000.0, "guarantee": dict(g)} for d in ("down", "up")}

    def test_capacity_and_prediction(self):
        from intentqos import TIN_BE, TIN_BULK, TIN_VOICE
        from intentqos.outcome import predict_queues, tin_capacity
        dem = {d: {0: 0.0, 1: 2000.0, 2: 0.0, 3: 9000.0, 4: 9000.0} for d in ("down", "up")}
        cap = tin_capacity(self.plan(500.0)["down"], dem["down"])
        self.assertEqual(cap[TIN_VOICE], 10000.0 - 3000.0 - 1000.0)      # higher priority: lower tins keep only guarantees
        self.assertEqual(cap[TIN_BULK], 1000.0)                          # lowest priority: its guarantee
        q = predict_queues(self.plan(500.0), dem, {})
        self.assertLess(q["down"][TIN_VOICE][1], q["down"][TIN_BE][0])   # voice stays short, best effort saturates

    def test_gate_refuses_a_plan_that_starves_a_call(self):
        from intentqos.outcome import OutcomeController

        class T:
            req = __import__("intentqos.qoo", fromlist=["x"]).requirements()
            snapshot = {"apps": [{"ip": "192.168.1.5", "device": "laptop", "label": "voip", "tin": "voice", "qoo": 90.0,
                                  "latency_ms": {50: 40.0, 99: 70.0}, "loss_pct": 0.0, "latency_source": "estimated"}],
                        "queues": {"down": {"voice": {50: 1.0, 99: 2.0}}, "up": {"voice": {50: 1.0, 99: 2.0}}}}
        oc = OutcomeController(C.load(None), T())
        dem = {d: {0: 0.0, 1: 3000.0, 2: 0.0, 3: 5000.0, 4: 5000.0} for d in ("down", "up")}   # downloads running
        good, bad = self.plan(3500.0), self.plan(500.0)
        for p in (good, bad):
            for d in p:
                p[d]["rate"] = 3600.0
                p[d]["guarantee"] = {0: 8.0, 1: p[d]["guarantee"][1] if p is good else 500.0, 2: 30.0,
                                     3: 30.0 if p is good else 2500.0, 4: 30.0 if p is good else 560.0}
        self.assertIsNone(oc.gate(good, good, dem, []))
        why = oc.gate(bad, good, dem, [])
        self.assertIsNotNone(why)
        self.assertIn("outcome gate", why)

    def test_safe_tuner_returns_to_the_safe_setting(self):
        from intentqos.outcome import SafeTuner
        st = SafeTuner([8.0, 10.0, 15.0, 20.0], 15.0, safety_min=60.0, dwell_s=1.0)
        self.assertIsNone(st.step(0.0, 70.0, 80.0))
        new = st.step(1.5, 72.0, 80.0)                   # dwell over: may move to a neighbour of a safe setting
        self.assertIn(new, (10.0, 15.0, 20.0))
        st.x = 8.0
        self.assertEqual(st.step(2.0, 50.0, 40.0), 15.0)  # unsafe sample: straight back to the safe seed


class EnforcementCommands(unittest.TestCase):
    def test_l4s_leaf_and_cake_mq(self):
        from unittest import mock
        from intentqos import TIN_BE, TIN_VOICE
        from intentqos import enforce as E
        cfg = C.load(None)
        with mock.patch.object(E.HtbBackend, "_probe_leaf", return_value=True):
            b = E.HtbBackend("lan0", "wan0", cfg)
        self.assertTrue(b.l4s and b.dualpi2)
        self.assertIn("dualpi2", b._leaf_cmd("lan0", TIN_VOICE, 1000))
        self.assertIn("ce_threshold_selector 0x1/0x3", b._leaf_cmd("lan0", TIN_BE, 1000))
        c = E.CakeBackend("lan0", "wan0", cfg, {"down": "cake_mq", "up": "cake"})
        self.assertEqual(c._qdisc("replace", "down", 90000)[8], "cake_mq")
        self.assertEqual(c._qdisc("change", "up", 9000)[6], "cake")

    def test_bulk_offload_ruleset(self):
        from unittest import mock
        from intentqos import enforce as E
        cfg = C.load(None, {"enforce": {"flow_offload": "software-bulk"}})
        m = E.FlowMarker(E.CakeBackend("lan0", "wan0", cfg), cfg, ["192.168.1.0/24"])
        m.offload_ok = True
        with mock.patch.object(E, "run") as run:
            m.setup()
        rules = run.call_args_list[-1][0][1]
        self.assertIn("flowtable iqft", rules)
        self.assertIn("meta mark 0x51004", rules)
        self.assertNotIn("flags offload", rules)          # software fast path: still shaped at egress


class IdentityAndCellular(unittest.TestCase):
    def test_identity_rules(self):
        from intentqos.identity import guess, private_mac
        self.assertEqual(guess(vendor="android-dhcp-13")[0], "phone")
        self.assertEqual(guess(hostname="LAPTOP-7Q2")[0], "laptop")
        self.assertEqual(guess(hostname="PS5-123")[0], "console")
        self.assertEqual(guess(mac="b8:27:eb:00:11:22")[0], "iot")
        self.assertTrue(private_mac("da:a1:19:00:00:01"))
        self.assertEqual(guess(opts="1,3,6", fingerprints={"1,3,6": "printer"})[0], "printer")

    def test_cellular_ceiling(self):
        import json
        from intentqos.cellular import CellularCeiling, ceiling_kbit, parse_mmcli_signal
        sig = parse_mmcli_signal(json.dumps({"modem": {"signal": {"lte": {"rsrp": "-95", "snr": "12.0"},
                                                                  "5g": {"rsrp": "--"}}}}))
        self.assertEqual((sig["tech"], sig["sinr"]), ("lte", 12.0))
        self.assertGreater(ceiling_kbit(20, 20, 0.35), ceiling_kbit(0, 20, 0.35))
        seq = iter([12.0, 12.0, 4.0])
        out = lambda args: "" if "--signal-setup=2" in args else json.dumps(  # noqa: E731
            {"modem": {"signal": {"lte": {"snr": str(next(seq))}}}})
        cc = CellularCeiling(C.load(None, {"cellular": {"enabled": True}}), runner=out)
        self.assertFalse(cc.poll(0.0)["fade"])
        self.assertFalse(cc.poll(3.0)["fade"])
        self.assertTrue(cc.poll(6.0)["fade"])            # 8 dB drop within 10 s: a fade


class KernelEngineCommands(unittest.TestCase):
    """What the engine tells the kernel flow engine over the helper's stdin (no kernel needed)."""

    def helper(self):
        import io
        import types
        from intentqos.capture import FlowTable, HelperCapture
        h = HelperCapture("br-lan", FlowTable(["192.168.1.0/24"]), "/bin/true", ["192.168.1.0/24"], kernel_obj="/x/iq-flow.bpf.o", wan="eth0")
        h.proc = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        return h

    def test_arguments_and_class_table(self):
        h = self.helper()
        self.assertEqual(h.args[-4:], ["-e", "/x/iq-flow.bpf.o", "-w", "eth0"])
        h.classes({0: "1:10", 1: "1:11", 2: "1:12", 3: "1:13", 4: "1:14"},
                  {0: "ef", 1: "ef", 2: "af41", 3: "cs0", 4: "cs1"}, True)
        self.assertEqual(h.proc.stdin.getvalue(), "P 1 65552 46 65553 46 65554 34 65555 0 65556 8\n")

    def test_verdict_lines(self):
        h = self.helper()
        h.verdicts({("192.168.1.5", "203.0.113.9", 6, 51000, 443): 1, ("fd00::5", "2001:db8::1", 17, 5000, 3478): None})
        self.assertEqual(h.proc.stdin.getvalue().splitlines(),
                         ["M 4 192.168.1.5 203.0.113.9 6 51000 443 1", "M 6 fd00::5 2001:db8::1 17 5000 3478 255"])
        h.clear_verdicts()
        self.assertTrue(h.proc.stdin.getvalue().endswith("C\n"))

    def test_marker_hands_changes_to_the_kernel(self):
        import types
        from intentqos.enforce import FlowMarker
        sent = []
        k = types.SimpleNamespace(kernel=True, classes=lambda *a: sent.append(("P",) + a),
                                  clear_verdicts=lambda: sent.append(("C",)), verdicts=lambda c: sent.append(("M", dict(c))))
        be = types.SimpleNamespace(priority=lambda t: f"1:{10 + t}", lan="br-lan", wan="eth0")
        m = FlowMarker(be, C.load(None), ["192.168.1.0/24"])
        m.attach_kernel(k)
        self.assertEqual([x[0] for x in sent], ["P", "C", "M"])
        import intentqos.enforce as E
        orig = E.subprocess.run
        E.subprocess.run = lambda *a, **kw: types.SimpleNamespace(returncode=0)
        try:
            key = ("192.168.1.5", "203.0.113.9", 6, 51000, 443)
            m.sync({key: 1})
            self.assertEqual(sent[-1], ("M", {key: 1}))
            m.sync({})
            self.assertEqual(sent[-1], ("M", {key: None}))
        finally:
            E.subprocess.run = orig


class DriftProofClassifier(unittest.TestCase):
    """Phase-wise forests in the router format, adaptive conformal, weak labels."""

    MODEL = os.path.join(ROOT, "models", "flowclf.iqm")

    @unittest.skipUnless(os.path.exists(os.path.join(ROOT, "models", "flowclf.iqm")), "no exported model")
    def test_router_model_routes_by_flow_age(self):
        from intentqos.treemodel import TreeModel
        m = TreeModel(self.MODEL)
        if len(m.phases) < 2:
            self.skipTest("model exported without phases")
        self.assertEqual(m.phase_of(0.5), 0)
        self.assertEqual(m.phase_of(None), len(m.phases) - 1)
        self.assertEqual(m.phase_of(10 ** 6), len(m.phases) - 1)
        x = [0.0] * len(m.features) if hasattr(m, "features") else [0.0] * 27
        for ph in range(len(m.phases)):
            p = m.proba(x, ph)
            self.assertAlmostEqual(sum(p), 1.0, places=5)
        (label, prob, pset, ph), = m.predict([x], ages=[0.5])
        self.assertEqual(ph, 0)
        self.assertIn(label, m.classes)

    def test_adaptive_conformal_widens_after_misses_and_narrows_after_hits(self):
        from intentqos.classifier import AdaptiveConformal
        scores = [i / 100 for i in range(100)]
        a = AdaptiveConformal(0.1, scores, 0.9, gamma=0.05)
        q0 = a.qhat()
        for _ in range(10):
            a.feedback(False, "household")
        self.assertGreater(a.qhat(), q0)                 # missing the truth -> larger sets
        q1 = a.qhat()
        for _ in range(200):
            a.feedback(True, "settled (self-check)")
        self.assertLess(a.qhat(), q1)
        info = a.info()
        self.assertEqual(info["feedback"], 210)
        self.assertEqual(info["by_source"]["household"]["misses"], 10)
        self.assertGreaterEqual(info["alpha"], 0.005)

    def test_ms365_optimize_endpoints_become_call_labels(self):
        import json as _json
        from intentqos.weaklabels import WeakLabels, from_ms365
        eps = [{"id": 11, "serviceArea": "Skype", "category": "Optimize", "udpPorts": "3478-3481",
                "ips": ["52.112.0.0/14", "2603:1063::/38"]},
               {"id": 1, "serviceArea": "Exchange", "category": "Optimize", "tcpPorts": "443", "ips": ["13.107.6.152/31"]},
               {"id": 46, "serviceArea": "Common", "category": "Default", "udpPorts": "443", "ips": ["20.0.0.0/8"]}]
        entries = from_ms365(eps)
        self.assertTrue(all(e["labels"] == ["voip", "video"] for e in entries))
        self.assertFalse(any(e["net"].startswith("20.") for e in entries))      # Default category is not a call
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "weak.json")
            with open(p, "w") as f:
                _json.dump({"fetched": time.time(), "source": "test", "entries": entries}, f)
            w = WeakLabels({"weak_labels": {"path": p}})
            w.load()
            self.assertIsNotNone(w.lookup("52.113.1.9", 17))
            self.assertIsNone(w.lookup("13.107.6.152", 17))                     # TCP-only Optimize set, asked for UDP
            self.assertIsNone(w.lookup("8.8.8.8", 17))


if __name__ == "__main__":
    unittest.main()
