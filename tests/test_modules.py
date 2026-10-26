"""Unit tests for every engine module (stdlib unittest, no root, no network).

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intentqos import TIN_BE, TIN_BULK, TIN_INTENT, TIN_VIDEO, TIN_VOICE  # noqa: E402
from intentqos import config as C  # noqa: E402
from intentqos.capture import FlowTable, parse_frame  # noqa: E402
from intentqos.classifier import Classifier, PageHinkley, RuleClassifier  # noqa: E402
from intentqos.estimator import CapacityEstimator  # noqa: E402
from intentqos.features import FEATURE_NAMES, flow_features  # noqa: E402
from intentqos.intents import IntentManager, water_fill  # noqa: E402
from intentqos.metrics import demand_normalised_jain, jain, max_min_fair  # noqa: E402
from intentqos.policy import decide, shield, significant  # noqa: E402


def eth_ipv4(src, dst, proto, sport, dport, total_len, flags=0, vlan=False, payload=b"SECRETPAYLOAD"):
    eth = b"\x02" * 6 + b"\x04" * 6 + ((b"\x81\x00\x00\x05\x08\x00") if vlan else b"\x08\x00")
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total_len, 1, 0, 64, proto, 0,
                     socket.inet_aton(src), socket.inet_aton(dst))
    if proto == 6:
        l4 = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, flags, 1000, 0, 0)
    else:
        l4 = struct.pack("!HHHH", sport, dport, total_len - 20, 0)
    return eth + ip + l4 + payload


def eth_ipv6(src, dst, nh, sport, dport, payload_len, hop_by_hop=False):
    eth = b"\x02" * 6 + b"\x04" * 6 + b"\x86\xdd"
    first_nh = 0 if hop_by_hop else nh
    ip = struct.pack("!IHBB16s16s", 0x60000000, payload_len, first_nh, 64,
                     socket.inet_pton(socket.AF_INET6, src), socket.inet_pton(socket.AF_INET6, dst))
    ext = bytes([nh, 0]) + b"\x00" * 6 if hop_by_hop else b""
    l4 = struct.pack("!HHHH", sport, dport, 8, 0)
    return eth + ip + ext + l4


def cfg():
    c = C.load(None)
    tmp = tempfile.mkdtemp()
    c["_root"] = tmp
    return c


class TestTcParsing(unittest.TestCase):
    """The enforcer reads tc's class statistics; some iproute2 builds (seen on Ubuntu 24.04 in WSL)
    return unusable JSON, so the text form must give the same numbers."""

    TEXT = (
        "class htb 1:1 root rate 90Mbit ceil 90Mbit burst 22500b cburst 22500b\n"
        " Sent 255506884 bytes 232001 pkt (dropped 0, overlimits 10230 requeues 0) \n"
        " backlog 0b 0p requeues 0\n"
        " lended: 12258 borrowed: 0 giants: 0\n\n"
        "class htb 1:50 parent 1:1 leaf 50: prio 4 rate 4500Kbit ceil 90Mbit burst 22500b cburst 22500b\n"
        " Sent 7965080 bytes 35597 pkt (dropped 17, overlimits 0 requeues 0) \n"
        " backlog 12Kb 8p requeues 0\n")

    def test_text_matches_the_json_shape(self):
        from intentqos.enforce import htb_classes_text
        root, bulk = htb_classes_text(self.TEXT)
        self.assertEqual((root["handle"], root["rate"]), ("1:1", 90e6 / 8))
        self.assertEqual(bulk["rate"], 4.5e6 / 8)
        self.assertEqual(bulk["stats"], {"bytes": 7965080, "packets": 35597, "drops": 17, "backlog": 12288, "qlen": 8})

    def test_json_is_cut_out_of_surrounding_text(self):
        from unittest import mock
        from intentqos import enforce as E
        with mock.patch.object(E, "run", return_value='Warning: something\n[{"handle": "1:1", "rate": 5}]\n'):
            self.assertEqual(E.tc_json(["tc", "-j", "class", "show"]), [{"handle": "1:1", "rate": 5}])
        with mock.patch.object(E, "run", return_value="no json here"):
            with self.assertRaises(E.TcError):
                E.tc_json(["tc", "-j", "class", "show"])


class TestCapture(unittest.TestCase):
    def test_ipv4_tcp_udp_vlan(self):
        f = eth_ipv4("192.168.1.10", "1.2.3.4", 6, 40000, 443, 1500, flags=0x02)
        p = parse_frame(bytearray(f), len(f), len(f))
        self.assertEqual(p, (4, "192.168.1.10", "1.2.3.4", 6, 40000, 443, 1500, 0x02))
        f = eth_ipv4("1.2.3.4", "192.168.1.10", 17, 3478, 50000, 200, vlan=True)
        p = parse_frame(bytearray(f), len(f), len(f))
        self.assertEqual(p[:7], (4, "1.2.3.4", "192.168.1.10", 17, 3478, 50000, 200))

    def test_ipv6_with_extension_header(self):
        f = eth_ipv6("fd00::10", "2001:db8::1", 17, 5004, 3478, 180, hop_by_hop=True)
        p = parse_frame(bytearray(f), len(f), len(f))
        self.assertEqual(p[:6], (6, "fd00::10", "2001:db8::1", 17, 5004, 3478))

    def test_payload_never_parsed(self):
        # changing payload bytes must not change the parse result
        a = eth_ipv4("192.168.1.10", "1.2.3.4", 17, 1, 2, 300, payload=b"A" * 50)
        b = eth_ipv4("192.168.1.10", "1.2.3.4", 17, 1, 2, 300, payload=b"B" * 50)
        self.assertEqual(parse_frame(bytearray(a), len(a), len(a)), parse_frame(bytearray(b), len(b), len(b)))

    def test_flow_direction_and_canonical_key(self):
        t = FlowTable(["192.168.1.0/24"])
        up = parse_frame(bytearray(eth_ipv4("192.168.1.10", "1.2.3.4", 17, 5000, 3478, 200)), 100, 100)
        dn = parse_frame(bytearray(eth_ipv4("1.2.3.4", "192.168.1.10", 17, 3478, 5000, 900)), 100, 100)
        t.add(up, 1.0)
        t.add(dn, 1.01)
        self.assertEqual(len(t.flows), 1)
        f = next(iter(t.flows.values()))
        self.assertEqual(f.key, ("192.168.1.10", "1.2.3.4", 17, 5000, 3478))
        self.assertEqual((f.pk_up, f.by_up, f.pk_dn, f.by_dn), (1, 200, 1, 900))
        lan = parse_frame(bytearray(eth_ipv4("192.168.1.10", "192.168.1.11", 17, 1, 2, 100)), 100, 100)
        t.add(lan, 1.02)
        self.assertEqual(len(t.flows), 1, "LAN-to-LAN traffic is not shaped or tracked")

    def test_features_shape(self):
        t = FlowTable(["192.168.1.0/24"])
        for i in range(60):
            t.add(parse_frame(bytearray(eth_ipv4("192.168.1.10", "1.2.3.4", 17, 5000, 3478, 172)), 100, 100), i * 0.02)
            t.add(parse_frame(bytearray(eth_ipv4("1.2.3.4", "192.168.1.10", 17, 3478, 5000, 172)), 100, 100),
                  i * 0.02 + 0.005)
        t.roll_windows(1.2)
        f = next(iter(t.flows.values()))
        x = flow_features(f, 1.2)
        self.assertEqual(len(x), len(FEATURE_NAMES))
        self.assertAlmostEqual(x[FEATURE_NAMES.index("mean_up")], 172.0)
        self.assertEqual(x[FEATURE_NAMES.index("is_udp")], 1.0)


def make_flow(proto, pkts_per_window, size, windows, now0=0.0):
    t = FlowTable(["192.168.1.0/24"])
    tt = now0
    for w in range(windows):
        for i in range(pkts_per_window):
            tt = now0 + w * 0.5 + i * (0.5 / max(1, pkts_per_window))
            t.add(parse_frame(bytearray(eth_ipv4("1.2.3.4", "192.168.1.10", proto, 443, 50000, size)), 100, 100), tt)
        t.roll_windows(now0 + (w + 1) * 0.5)
    return t, next(iter(t.flows.values()))


class TestClassifier(unittest.TestCase):
    def test_rules_udp(self):
        r = RuleClassifier(cfg())
        _, f = make_flow(17, 25, 172, 4)
        self.assertEqual(r.classify(f, 2.0)[0], "voip")
        _, f = make_flow(17, 120, 1100, 4)
        self.assertEqual(r.classify(f, 2.0)[0], "video")

    def test_rules_bulk_needs_sustained_rate(self):
        r = RuleClassifier(cfg())
        t, f = make_flow(6, 400, 1500, 4)
        self.assertEqual(r.classify(f, 2.0)[0], "web", "not bulk before bulk_trigger_s")
        for w in range(4, 16):
            for i in range(400):
                t.add(parse_frame(bytearray(eth_ipv4("1.2.3.4", "192.168.1.10", 6, 443, 50000, 1500)), 100, 100),
                      w * 0.5 + i / 800)
            t.roll_windows((w + 1) * 0.5)
            lab = r.classify(f, (w + 1) * 0.5)[0]
        self.assertEqual(lab, "bulk")

    def test_page_hinkley_detects_shift(self):
        ph = PageHinkley(delta=0.02, threshold=8.0, min_n=50)
        fired = [ph.update(0.0) for _ in range(300)]
        self.assertFalse(any(fired))
        fired = [ph.update(1.0) for _ in range(100)]
        self.assertTrue(any(fired))

    def test_correction_overrides_and_anti_flap(self):
        c = cfg()
        c["classifier"]["mode"] = "rules"
        ev = []
        clf = Classifier(c, lambda p: os.path.join(c["_root"], p), lambda *a: ev.append(a))
        _, f = make_flow(17, 25, 172, 4)
        d = clf.classify([f], 2.0)[f.id()]
        self.assertEqual((d.label, d.committed), ("voip", "voip"))
        clf.correct(f, "gaming")
        d = clf.classify([f], 2.5)[f.id()]
        self.assertEqual((d.label, d.source, d.committed), ("gaming", "user", "gaming"))
        self.assertTrue(os.path.exists(clf.corrections_path))
        with self.assertRaises(ValueError):
            clf.correct(f, "not-a-label")

    def test_labels_survive_an_engine_restart(self):
        """A flow decided before a restart keeps its class when the restarted engine sees it mid-stream."""
        c = cfg()
        c["classifier"]["mode"] = "rules"
        res = lambda p: os.path.join(c["_root"], p)  # noqa: E731
        clf = Classifier(c, res, lambda *a: None)
        _, f = make_flow(17, 25, 172, 4)
        clf.rules.classify = lambda fl, now: ("streaming", "stub")
        self.assertEqual(clf.classify([f], 2.0)[f.id()].committed, "streaming")
        clf.save_labels()
        clf2 = Classifier(c, res, lambda *a: None)              # the restarted engine
        clf2.rules.classify = lambda fl, now: ("bulk", "stub")   # mid-stream it looks like a download
        for i in range(40):
            d = clf2.classify([f], 3.0 + i * 0.5)[f.id()]
        self.assertEqual((d.committed, d.restored, d.label), ("streaming", True, "bulk"))
        clf2.correct(f, "web")                                   # a person's correction still wins
        d = clf2.classify([f], 30.0)[f.id()]
        self.assertEqual((d.committed, d.restored), ("web", False))
        # stale label files are ignored
        import json as _j
        with open(clf.labels_path) as fh:
            doc = _j.load(fh)
        doc["t"] -= 3600
        with open(clf.labels_path, "w") as fh:
            _j.dump(doc, fh)
        self.assertEqual(Classifier(c, res, lambda *a: None).restored, {})

    def test_demotion_needs_sustained_evidence(self):
        """A streaming flow that briefly looks like a download is not demoted; a promotion is quick."""
        c = cfg()
        c["classifier"]["mode"] = "rules"
        c["classifier"]["commit_ticks"], c["classifier"]["demote_ticks"] = 2, 6
        clf = Classifier(c, lambda p: os.path.join(c["_root"], p), lambda *a: None)
        _, f = make_flow(17, 25, 172, 4)
        seq = iter([])
        clf.rules.classify = lambda fl, now: (next(seq), "stub")
        seq = iter(["streaming"] + ["bulk"] * 5 + ["streaming"])
        got = [clf.classify([f], 2.0 + i * 0.5)[f.id()].committed for i in range(7)]
        self.assertEqual(got, ["streaming"] * 7, "5 ticks of 'bulk' (< demote_ticks) must not demote")
        seq = iter(["bulk"] * 6)
        got = [clf.classify([f], 6.0 + i * 0.5)[f.id()].committed for i in range(6)]
        self.assertEqual(got[-1], "bulk", "sustained evidence demotes")
        self.assertEqual(got[:5], ["streaming"] * 5)
        seq = iter(["video"] * 2)
        got = [clf.classify([f], 9.0 + i * 0.5)[f.id()].committed for i in range(2)]
        self.assertEqual(got, ["bulk", "video"], "promotion after commit_ticks")


class TestEstimator(unittest.TestCase):
    def _est(self):
        c = cfg()
        c["estimator"]["reflectors"] = ["r1"]
        e = CapacityEstimator(c, lambda *a: None, prober=None)
        return c, e

    def test_bloat_cuts_below_achieved(self):
        c, e = self._est()
        now = 100.0
        for i in range(10):     # baseline 10 ms
            e.ingest([(now + i * 0.05, "r1", 20.0, 10.0, 10.0)])
        e.last_sample_t = now + 0.5
        e.tick(now + 0.5, 80000, 1000)
        for i in range(12):     # +60 ms on the download under load
            e.ingest([(now + 0.6 + i * 0.05, "r1", 80.0, 10.0, 70.0)])
        e.last_sample_t = now + 1.2
        rd, ru = e.tick(now + 1.2, 20000, 1000)
        self.assertLess(rd, 20000, "rate must drop below the achieved throughput")
        self.assertGreaterEqual(rd, e.down.min)
        self.assertAlmostEqual(ru, e.up.base, delta=e.up.base * 0.05)

    def test_increase_is_bounded_and_blind_mode_caps(self):
        c, e = self._est()
        now = 0.0
        e.ingest([(now, "r1", 20.0, 10.0, 10.0)])
        e.last_sample_t = now
        r0 = e.down.rate
        rd, _ = e.tick(now, r0 * 0.95, 0)
        self.assertLessEqual(rd - r0, max(r0 * c["estimator"]["increase_step_fraction"],
                                          e.down.max * c["estimator"]["increase_min_step_fraction"]) + 1)
        self.assertLessEqual(rd, e.down.max)
        rd, _ = e.tick(now + 5.0, 0, 0)       # no samples for 5 s
        self.assertTrue(e.blind)
        self.assertLessEqual(rd, e.down.base)


    def _drive(self, e, t0, seconds, delay_of_rate, offered):
        """Simulate: every 50 ms one probe whose download delay depends on our shaper rate; tick every 250 ms."""
        t = t0
        while t < t0 + seconds:
            for k in range(5):
                e.ingest([(t + k * 0.05, "r1", 20.0, 10.0, delay_of_rate(e.down.rate))])
            t += 0.25
            e.last_sample_t = t
            e.tick(t, min(offered, e.down.rate), 1000)
        return t

    def test_path_shift_is_relearned_not_chased(self):
        """Failover to a longer path: delay +25 ms whatever we send. Cut a few times, then re-learn, rate back."""
        c, e = self._est()
        t = self._drive(e, 0.0, 3.0, lambda r: 10.0, 60000)        # 10 ms baseline, busy link
        r_before = e.down.rate
        t = self._drive(e, t, 12.0, lambda r: 35.0, 60000)          # path now 25 ms longer, independent of rate
        self.assertEqual(e.down.rebaselines, 1)
        self.assertGreater(e.down.rate, 0.8 * r_before, "the cuts that chased the path change are undone")

    def test_path_shift_at_the_floor_is_relearned(self):
        """The longer path appears while we carry little traffic, so the first cuts land on the floor; the drain
        probe must still run there, re-learn the path and let the rate climb back (seen after 4G failovers)."""
        c, e = self._est()
        t = self._drive(e, 0.0, 3.0, lambda r: 10.0, 60000)
        t = self._drive(e, t, 1.0, lambda r: 130.0, 300)             # failover: a deep transient queue while we
        t = self._drive(e, t, 2.0, lambda r: 35.0, 300)              # carry little -> cut to the floor; then the
        t = self._drive(e, t, 25.0, lambda r: 35.0, 60000)            # path stays 25 ms longer, traffic returns
        self.assertGreaterEqual(e.down.rebaselines, 1)
        self.assertGreater(e.down.rate, 5 * e.down.min, "not stuck at the floor")

    def test_real_queue_is_not_relearned(self):
        """A real standing queue drains when we cut: the delay falls, so no re-baseline happens."""
        c, e = self._est()
        t = self._drive(e, 0.0, 3.0, lambda r: 10.0, 60000)
        cap = 30000.0                                                # the line fell to 30 Mbit/s
        t = self._drive(e, t, 12.0, lambda r: 10.0 + (60.0 if r > cap else 0.0), 60000)
        self.assertEqual(e.down.rebaselines, 0)
        self.assertLessEqual(e.down.rate, cap * 1.05)

class TestPolicy(unittest.TestCase):
    def test_floors_and_sum(self):
        c = cfg()
        dem = {"down": {TIN_INTENT: 0, TIN_VOICE: 200, TIN_VIDEO: 60000, TIN_BE: 5000, TIN_BULK: 90000},
               "up": {TIN_INTENT: 0, TIN_VOICE: 100, TIN_VIDEO: 1500, TIN_BE: 100, TIN_BULK: 15000}}
        plan = decide(c, {"down": 90000, "up": 18000}, dem, [])
        rep = shield(c, plan, None, {"down": (10000, 95000), "up": (2000, 19000)}, 0)
        for d in ("down", "up"):
            R = plan[d]["rate"]
            g = plan[d]["guarantee"]
            self.assertLessEqual(sum(g.values()), R * 1.0001)
            self.assertGreaterEqual(g[TIN_BULK], 0.05 * R * 0.999, "bulk keeps its floor")
            self.assertGreaterEqual(g[TIN_BE], 0.10 * R * 0.999)
            self.assertLessEqual(g[TIN_VOICE] + g[TIN_VIDEO] + g[TIN_INTENT], 0.6 * R + 1)
        self.assertIsNotNone(rep)

    def test_bulk_share_is_an_operator_setting(self):
        """remainder_bulk_share moves what is left between best effort and bulk; floors still hold."""
        z = {"down": {t: 0 for t in range(5)}, "up": {t: 0 for t in range(5)}}
        c = cfg()
        g1 = decide(c, {"down": 90000, "up": 18000}, z, [])["down"]["guarantee"]
        c["policy"]["remainder_bulk_share"] = 0.5
        g2 = decide(c, {"down": 90000, "up": 18000}, z, [])["down"]["guarantee"]
        self.assertGreater(g1[TIN_BE] - 0.10 * 90000, 1.9 * (g1[TIN_BULK] - 0.05 * 90000), "default: 2:1")
        self.assertAlmostEqual(g2[TIN_BE] - 0.10 * 90000, g2[TIN_BULK] - 0.05 * 90000, delta=1.0)
        self.assertAlmostEqual(sum(g1.values()), sum(g2.values()), delta=1.0)
        from intentqos.manage import validate
        self.assertEqual(validate({"policy.remainder_bulk_share": 0.5}), {"policy.remainder_bulk_share": 0.5})
        with self.assertRaises(ValueError):
            validate({"policy.remainder_bulk_share": 0.95})

    def test_shield_limits_steps_and_holds(self):
        c = cfg()
        z = {"down": {t: 0 for t in range(5)}, "up": {t: 0 for t in range(5)}}
        last = decide(c, {"down": 50000, "up": 10000}, z, [])
        plan = decide(c, {"down": 99000, "up": 1000}, z, [])
        rep = shield(c, plan, last, {"down": (10000, 95000), "up": (2000, 19000)}, 0)
        self.assertLessEqual(plan["down"]["rate"], 50000 * 1.15 + 1)
        self.assertGreaterEqual(plan["up"]["rate"], 2000)
        self.assertTrue(rep.interventions)
        plan2 = decide(c, {"down": 55000, "up": 10000}, z, [])
        rep2 = shield(c, plan2, last, {"down": (10000, 95000), "up": (2000, 19000)}, 999)
        self.assertTrue(rep2.held, "change budget exhausted -> hold")
        plan3 = decide(c, {"down": 40000, "up": 10000}, z, [])
        rep3 = shield(c, plan3, last, {"down": (10000, 95000), "up": (2000, 19000)}, 999)
        self.assertFalse(rep3.held, "decreases are always allowed (safe direction)")

    def test_hysteresis(self):
        c = cfg()
        z = {"down": {t: 0 for t in range(5)}, "up": {t: 0 for t in range(5)}}
        a = decide(c, {"down": 50000, "up": 10000}, z, [])
        b = decide(c, {"down": 50500, "up": 10000}, z, [])
        self.assertFalse(significant(b, a, 0.05))
        b = decide(c, {"down": 60000, "up": 10000}, z, [])
        self.assertTrue(significant(b, a, 0.05))


class TestIntents(unittest.TestCase):
    def test_water_fill(self):
        self.assertEqual(water_fill([2, 3], [1, 1], 10), [2, 3])
        a = water_fill([10, 10], [1, 1], 10)
        self.assertAlmostEqual(a[0], 5)
        a = water_fill([10, 2], [1, 1], 8)
        self.assertEqual(a, [6, 2])
        a = water_fill([10, 10], [3, 1], 8)
        self.assertAlmostEqual(a[0] / a[1], 3)
        self.assertAlmostEqual(sum(a), 8)

    def test_lease_lifecycle_budget_and_conflicts(self):
        c = cfg()
        c["intents"]["daily_budget_min"] = 90
        ev = []
        im = IntentManager(c, lambda p: os.path.join(c["_root"], p), lambda *a: ev.append(a))
        now = time.time()
        r = im.request({"target": "192.168.1.10", "duration_s": 3600, "demand_down_kbit": 3000,
                        "demand_up_kbit": 1500, "requester": "a"}, 20000, 5000, now=now)
        self.assertTrue(r["ok"], r)
        r2 = im.request({"target": "192.168.1.11", "duration_s": 3600, "requester": "a"}, 20000, 5000, now=now)
        self.assertFalse(r2["ok"])
        self.assertIn("budget", r2["reason"])
        r3 = im.request({"target": "192.168.1.11", "duration_s": 999999, "requester": "b"}, 20000, 5000, now=now)
        self.assertFalse(r3["ok"])
        # oversubscription -> bargaining shares boost capacity (5000*0.6 = 3000 up)
        r4 = im.request({"target": "192.168.1.12", "duration_s": 600, "demand_up_kbit": 2000, "demand_down_kbit": 1000,
                         "requester": "b", "priority": 2}, 20000, 5000, now=now)
        self.assertTrue(r4["ok"], r4)
        act = im.tick(now + 1, 20000, 5000)
        self.assertEqual(len(act), 2)
        self.assertLessEqual(sum(i.granted_up_kbit for i in act), 3000 + 1)
        act = im.tick(now + 3601, 20000, 5000)
        self.assertEqual(act, [], "leases expire on their own")
        dry = im.request({"target": "192.168.1.13", "duration_s": 60, "requester": "c"}, 20000, 5000,
                         now=now + 4000, dry_run=True)
        self.assertTrue(dry["ok"] and dry["dry_run"])


class TestMetrics(unittest.TestCase):
    def test_jain(self):
        self.assertAlmostEqual(jain([1, 1, 1, 1]), 1.0)
        self.assertAlmostEqual(jain([1, 0, 0, 0]), 0.25)
        self.assertEqual(max_min_fair([2, 10, 10], 12), [2, 5, 5])
        self.assertAlmostEqual(demand_normalised_jain([2, 5, 5], [2, 10, 10], 12), 1.0)


class TestAnalysis(unittest.TestCase):
    def test_emodel_and_jitter(self):
        import numpy as np
        from testbed.analysis import emodel_mos, rfc3550_jitter
        good = emodel_mos(np.full(50, 20.0), 0.0, 20.0)
        bad = emodel_mos(np.full(50, 400.0), 0.05, 20.0)
        self.assertGreater(good, 4.2)
        self.assertLess(bad, 3.0)
        tx = np.arange(100) * 0.02
        self.assertAlmostEqual(rfc3550_jitter(tx, tx + 0.01), 0.0)


if __name__ == "__main__":
    unittest.main()
