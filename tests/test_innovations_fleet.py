"""Tests for the IntentQoS 2.0 fleet and intent additions (stdlib only, no root, no network beyond loopback).

    python3 -m unittest tests.test_innovations_fleet -v
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from intentqos import config as C  # noqa: E402
from intentqos import privagg  # noqa: E402


class SequentialRollouts(unittest.TestCase):
    """mSPRT: always-valid p-values for the rollout guard."""

    def test_harmless_update_rarely_fails_however_often_it_is_checked(self):
        from controller.seqtest import SequentialGuard
        rnd = random.Random(3)
        false_alarms = 0
        for trial in range(60):
            g = SequentialGuard()
            a, b, p = [], [], 1.0
            for _look in range(30):                     # 30 looks per stage, same distribution in both arms
                a += [rnd.gauss(20, 5) for _ in range(4)]
                b += [rnd.gauss(20, 5) for _ in range(4)]
                p = g.update(("r", trial), a, b, tau=5.0)
            false_alarms += p < 0.05
        self.assertLessEqual(false_alarms, 6)           # about 5% of 60 at most, with room for chance

    def test_harmful_update_is_caught_early(self):
        from controller.seqtest import SequentialGuard
        rnd = random.Random(4)
        g, a, b, caught = SequentialGuard(), [], [], None
        for look in range(30):
            a += [rnd.gauss(35, 5) for _ in range(4)]    # canary 15 ms worse
            b += [rnd.gauss(20, 5) for _ in range(4)]
            if g.update(("r", 0), a, b, tau=5.0) < 0.05:
                caught = look
                break
        self.assertIsNotNone(caught)
        self.assertLess(caught, 5)

    def test_p_value_never_rises(self):
        from controller.seqtest import SequentialGuard
        g = SequentialGuard()
        p1 = g.update(("k",), [30, 31, 32, 33], [20, 21, 19, 20], 5.0)
        p2 = g.update(("k",), [20, 21, 19, 20, 20], [20, 21, 19, 20, 21], 5.0)
        self.assertLessEqual(p2, p1)
        self.assertEqual(g.looks[("k",)], 2)


class PrivateAggregation(unittest.TestCase):
    def apps(self, *pairs):
        return [{"label": lbl, "qoo": q} for lbl, q in pairs]

    def test_shares_add_up_and_one_share_alone_says_nothing(self):
        h = privagg.qoo_histogram(self.apps(("voip", 55.0), ("web", 95.0), ("web", 81.0)))
        self.assertEqual(sum(h), 3)
        lead, helper = privagg.split(h)
        self.assertEqual(privagg.combine(lead, helper), h)
        lead2, _ = privagg.split(h)
        self.assertNotEqual(lead, lead2)                 # fresh randomness each time
        self.assertTrue(any(x > 10 ** 6 for x in lead))  # looks like noise, not counts

    def _controller(self, homes=6):
        from controller import security as S
        from controller.core import Controller
        tmp = tempfile.TemporaryDirectory()
        ctl = Controller({"data_dir": tmp.name, "telemetry_interval_s": 15, "brand": {}, "retention": {}})
        db = ctl.db
        db.x("INSERT INTO plans (code, name, down_mbit, up_mbit, boost_minutes) VALUES ('P100','Home 100',100,20,480)")
        gws = []
        for i in range(homes):
            sid = db.x("INSERT INTO subscribers (account, name, phone, plan_id, region, pop, olt) VALUES (?,?,?,1,'R','P','OLT-A')",
                       (f"A{i}", f"Sub {i}", f"98000000{i:02d}"))
            db.x("INSERT INTO gateways (serial, claim_hash, subscriber_id) VALUES (?,?,?)", (f"S{i}", S.h(f"C{i}"), sid))
            ctl.enroll({"serial": f"S{i}", "claim_code": f"C{i}"}, "test")
            gws.append(db.one("SELECT * FROM gateways WHERE serial=?", (f"S{i}",)))
        return tmp, ctl, gws

    def _rec(self, t, apps):
        h = privagg.qoo_histogram(apps)
        lead, helper = privagg.split(h)
        return {"t": t, "grade": "A", "p95_add_d": 5, "rate_d": 90000, "plan": {"d": 100000, "u": 20000},
                "est": {"d": "low", "blind": False}, "safety": {"fail": 0}, "cap": {"drops": 0}, "cpu": 3,
                "agg": {"task": "qoo_hist_v1", "leader": lead, "helper": helper, "demo_both_shares": True}}

    def test_controller_releases_only_batches_of_five_homes_and_keeps_shares_out_of_telemetry(self):
        tmp, ctl, gws = self._controller(6)
        try:
            t = time.time()
            for g in gws[:4]:
                ctl.sync(ctl.gw(g["id"]), {"records": [self._rec(t, self.apps(("voip", 50.0), ("web", 90.0)))]})
            res = ctl.fleet_private_qoo()["batches"][0]
            self.assertFalse(res["released"])
            self.assertEqual(res["homes"], 4)
            for g in gws[4:]:
                ctl.sync(ctl.gw(g["id"]), {"records": [self._rec(t, self.apps(("voip", 90.0), ("web", 90.0)))]})
            out = ctl.fleet_private_qoo()
            res = out["batches"][0]
            self.assertTrue(res["released"] and res["plausible"])
            self.assertEqual(res["histogram"]["voip"]["40-59"], 4)
            self.assertEqual(res["histogram"]["voip"]["80-100"], 2)
            self.assertAlmostEqual(res["poor_share"]["voip"], 4 / 6, places=3)
            self.assertIn("Demonstration", out["privacy"])
            stored = ctl.db.val("SELECT rec FROM telemetry LIMIT 1")
            self.assertNotIn("leader", stored)            # shares are filed apart from the home's telemetry
        finally:
            tmp.cleanup()

    def test_implausible_total_is_withheld(self):
        tmp, ctl, gws = self._controller(5)
        try:
            t = time.time()
            for i, g in enumerate(gws):
                rec = self._rec(t, self.apps(("web", 90.0)))
                if i == 0:                                # a dishonest gateway: its helper share is garbage
                    rec["agg"]["helper"] = [random.getrandbits(60) for _ in rec["agg"]["helper"]]
                ctl.sync(ctl.gw(g["id"]), {"records": [rec]})
            res = ctl.fleet_private_qoo()["batches"][0]
            self.assertTrue(res["released"])
            self.assertFalse(res["plausible"])
            self.assertIsNone(res["histogram"])
        finally:
            tmp.cleanup()

    def test_separate_helper_service(self):
        from controller.privagg import Aggregators, report_id, serve_helper
        tmp = tempfile.TemporaryDirectory()
        srv = serve_helper(os.path.join(tmp.name, "helper.db"), "127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            from controller.db import DB
            db = DB(os.path.join(tmp.name, "leader.db"))
            agg = Aggregators(db, url)
            import urllib.request
            t = time.time()
            for i in range(5):
                h = privagg.qoo_histogram(self.apps(("gaming", 30.0 + i)))
                lead, helper = privagg.split(h)
                agg.take({"id": i + 1, "device_id": f"dev{i}"}, "OLT-B", {"t": t, "agg": {"task": "qoo_hist_v1", "leader": lead}})
                req = urllib.request.Request(url + "/upload", method="POST", headers={"Content-Type": "application/json"},
                                             data=json.dumps({"task": "qoo_hist_v1", "device": f"dev{i}", "t": t,
                                                              "share": helper}).encode())
                urllib.request.urlopen(req, timeout=5).read()
            self.assertEqual(len(report_id("dev0", t)), 24)
            res = agg.collect("qoo_hist_v1", "OLT-B", t - 10, t + 10)
            self.assertTrue(res["released"])
            self.assertEqual(res["histogram"]["gaming"]["20-39"], 5)
            # the helper refuses to sum fewer than five homes, whoever asks
            small = json.loads(urllib.request.urlopen(urllib.request.Request(
                url + "/aggregate", method="POST", data=json.dumps({"task": "qoo_hist_v1",
                                                                     "reports": [report_id("dev0", t)]}).encode()),
                timeout=5).read())
            self.assertIn("error", small)
        finally:
            srv.shutdown()
            tmp.cleanup()

    def test_agent_summary_reads_the_engine_snapshot(self):
        from intentqos.agent import Agent
        st = {"experience": {"apps": [{"label": "voip", "qoo": 62.0}, {"label": "web", "qoo": 95.0}],
                             "path": {"gsv": {"g": 11.2, "v_p50": 3.0, "v_p99": 41.0, "loss": 0.001}},
                             "devices": {"192.168.1.5": {"lan_rtt_ms": {"50": 4.0, "99": 28.0}}}},
              "outcome": {"objective": 80.1, "worst_protected": 62.0, "gated": 1},
              "bottlenecks": {"devices": {"192.168.1.5": {"where": "wifi"}, "192.168.1.6": {"where": "line"}}},
              "l4s": {"flows": 2}, "offers": [{"id": "x"}], "camara_sessions": 1}
        x = Agent._experience_summary(st)
        self.assertEqual((x["g"], x["v99"], x["worst"]["label"]), (11.2, 41.0, "voip"))
        self.assertEqual(x["bottlenecks"], {"wifi": 1, "line": 1})
        self.assertEqual((x["lan_rtt_p99_max"], x["l4s"], x["offers"], x["camara"]), (28.0, 2, 1, 1))


class Localisation(unittest.TestCase):
    """ΔQ G/V of a home compared with its OLT peers."""

    def setUp(self):
        self.tmp, self.ctl, self.gws = PrivateAggregation()._controller(6)
        for g in self.gws:
            self.ctl.db.x("UPDATE gateways SET online=1 WHERE id=?", (g["id"],))

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, gw, **xp):
        rec = {"t": time.time(), "grade": "A", "p95_add_d": 5, "rate_d": 90000, "plan": {"d": 100000, "u": 20000},
               "xp": {"g": 10.0, "v99": 5.0, "bottlenecks": {}, "lan_rtt_p99_max": 3.0, **xp}}
        self.ctl.db.x("INSERT INTO latest (gw, t, rec) VALUES (?,?,?) ON CONFLICT(gw) DO UPDATE SET t=excluded.t, rec=excluded.rec",
                      (gw["id"], rec["t"], json.dumps(rec)))

    def test_shared_congestion_points_at_the_olt(self):
        for g in self.gws:
            self.put(g, v99=80.0)
        d = self.ctl.diagnose(self.gws[0]["id"])
        self.assertEqual(d["localisation"]["segment"], "olt")

    def test_one_slow_home_points_at_its_access_line(self):
        for g in self.gws:
            self.put(g)
        self.put(self.gws[0], v99=70.0)
        d = self.ctl.diagnose(self.gws[0]["id"])
        self.assertEqual(d["localisation"]["segment"], "access")
        self.assertEqual(d["where"], "home-line")

    def test_raised_floor_points_upstream(self):
        for g in self.gws:
            self.put(g)
        self.put(self.gws[0], g=48.0)
        self.assertEqual(self.ctl.diagnose(self.gws[0]["id"])["localisation"]["segment"], "upstream")

    def test_slow_lan_round_trip_points_at_wifi(self):
        for g in self.gws:
            self.put(g)
        self.put(self.gws[0], bottlenecks={"wifi": 1}, lan_rtt_p99_max=40.0)
        d = self.ctl.diagnose(self.gws[0]["id"])
        self.assertEqual(d["localisation"]["segment"], "home-wifi")
        self.assertIn("Wi-Fi", d["verdict"])


def _engine(tmp):
    from intentqos.intents import IntentManager
    cfg = C.load(None)
    cfg["intents"]["store_path"] = os.path.join(tmp, "intents.json")
    eng = types.SimpleNamespace()
    eng.intents = IntentManager(cfg, lambda p: p, lambda *a, **k: None)
    eng.estimator = types.SimpleNamespace(down=types.SimpleNamespace(rate=100000.0, load=0.2, state="low"),
                                          up=types.SimpleNamespace(rate=20000.0), enabled=True)
    return eng


class RouterCamara(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = _engine(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_lifecycle(self):
        from intentqos.camara import QoDSessions
        q = QoDSessions(self.eng)
        s = q.create({"device": {"ipv4Address": {"privateAddress": "192.168.1.20"}}, "qosProfile": "QOS_E",
                      "duration": 600, "x-intentqos": {"network_extension": {"olt": "OLT-A", "simulated": True}}})
        self.assertIn(s["qosStatus"], ("AVAILABLE", "REQUESTED"))
        self.assertEqual(s["x-intentqos"]["network_extension"]["olt"], "OLT-A")
        s2 = q.extend(s["sessionId"], {"requestedAdditionalDuration": 300})
        self.assertGreater(s2["duration"], s["duration"])
        self.assertEqual(len(q.retrieve({"device": {"ipv4Address": {"privateAddress": "192.168.1.20"}}})), 1)
        q.delete(s["sessionId"])
        self.assertEqual(q.get(s["sessionId"])["qosStatus"], "UNAVAILABLE")

    def test_errors_use_camara_codes(self):
        from intentqos.camara import CamaraError, QoDSessions
        q = QoDSessions(self.eng)
        with self.assertRaises(CamaraError) as e:
            q.create({"device": {"ipv4Address": {"privateAddress": "192.168.1.20"}}, "qosProfile": "QOS_X"})
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(CamaraError) as e:
            q.get("nope")
        self.assertEqual(e.exception.status, 404)


class OperatorCamara(unittest.TestCase):
    """The controller's CAMARA API: finds the home by phone, relays to it, reserves the (simulated) access network."""

    def setUp(self):
        self.tmp, self.ctl, self.gws = PrivateAggregation()._controller(5)
        self.home = _engine(self.tmp.name)
        from intentqos.camara import CamaraError, QoDSessions
        router = QoDSessions(self.home)

        def relay(gw_id, method, path, body=None, by="", timeout=12.0):
            base = "/camara/quality-on-demand/v1/sessions"
            try:
                if method == "POST" and path == base:
                    return {"ok": True, "status": 201, "body": router.create(body)}
                sid = path[len(base) + 1:].split("/")[0]
                if method == "POST" and path.endswith("/extend"):
                    return {"ok": True, "status": 200, "body": router.extend(sid, body)}
                if method == "GET":
                    return {"ok": True, "status": 200, "body": router.get(sid)}
                if method == "DELETE":
                    router.delete(sid)
                    return {"ok": True, "status": 204, "body": None}
            except CamaraError as e:
                return {"ok": True, "status": e.status, "body": e.body()}
            return {"ok": False, "error": "unexpected"}
        self.ctl.relay = relay

    def tearDown(self):
        self.tmp.cleanup()

    def body(self, prof="QOS_E"):
        return {"device": {"phoneNumber": "+91 9800000003", "ipv4Address": {"privateAddress": "192.168.1.30"}},
                "qosProfile": prof, "duration": 900}

    def test_session_spans_home_and_access(self):
        nb = self.ctl.camara
        s = nb.create(self.body(), "telehealth-app")
        self.assertTrue(s["sessionId"].startswith("op-"))
        seg = s["x-intentqos"]["segments"]
        self.assertEqual(seg["access"], "RESERVED")
        self.assertTrue(s["x-intentqos"]["access_network"]["simulated"])
        self.assertEqual(s["device"]["phoneNumber"], "+91 9800000003")
        nb.delete(s["sessionId"], "telehealth-app")
        self.assertEqual(nb.list(), [])
        self.assertEqual(sum(1 for r in nb.net.res.values()), 0)

    def test_access_network_refuses_when_full_and_releases_on_home_refusal(self):
        from intentqos.camara import CamaraError
        nb = self.ctl.camara
        nb.net.capacity = 100000                          # 20% of 100 Mbit/s = 20 Mbit/s of priority reservations
        with self.assertRaises(CamaraError) as e:
            nb.create(self.body("QOS_L"), "app")          # 40 Mbit/s
        self.assertEqual(e.exception.code, "QUALITY_ON_DEMAND.UNAVAILABLE")
        with self.assertRaises(CamaraError):
            nb.create({**self.body(), "device": {"phoneNumber": "+91 9999999999",
                                                 "ipv4Address": {"privateAddress": "192.168.1.30"}}}, "app")
        self.assertEqual(len(nb.net.res), 0)


class ProactiveOffers(unittest.TestCase):
    def test_offer_for_a_call_on_a_busy_line_then_accept(self):
        from intentqos.offers import Offers
        tmp = tempfile.TemporaryDirectory()
        try:
            eng = _engine(tmp.name)
            o = Offers()
            apps = [{"ip": "192.168.1.5", "label": "voip", "qoo": 92.0}]
            devices = [{"ip": "192.168.1.9", "elastic": True}]
            made = o.consider(apps, devices, eng.estimator, [], {"192.168.1.5": "Work laptop"})
            self.assertEqual(len(made), 1)
            self.assertIn("Work laptop", made[0]["reason"])
            self.assertEqual(o.consider(apps, devices, eng.estimator, [], {}), [])    # quiet period
            r = o.accept(made[0]["id"], eng, minutes=30)
            self.assertTrue(r["ok"])
            self.assertEqual(o.open(), [])
        finally:
            tmp.cleanup()

    def test_no_offer_on_a_quiet_line(self):
        from intentqos.offers import Offers
        est = types.SimpleNamespace(enabled=True, down=types.SimpleNamespace(load=0.1, state="low"))
        self.assertEqual(Offers().consider([{"ip": "a", "label": "voip", "qoo": 95.0}], [], est, [], {}), [])


class PlainLanguage(unittest.TestCase):
    devices = {"192.168.1.5": "Work laptop", "192.168.1.6": "Living-room TV", "192.168.1.7": "Console"}

    def test_english_requests(self):
        from intentqos.nlintent import parse
        r = parse("keep my interview on the work laptop smooth for 45 minutes", self.devices)
        self.assertTrue(r["ok"])
        self.assertEqual((r["request"]["target"], r["request"]["goal"], r["request"]["duration_s"]),
                         ("192.168.1.5", "low_latency", 2700))
        r = parse("speed up the download on the TV for 2 hours", self.devices)
        self.assertEqual((r["request"]["target"], r["request"]["goal"], r["request"]["duration_s"]),
                         ("192.168.1.6", "throughput", 7200))

    def test_hindi_words_and_requester_device(self):
        from intentqos.nlintent import parse
        r = parse("meri call 1 ghante ke liye smooth rakho", self.devices, requester_ip="192.168.1.7")
        self.assertTrue(r["ok"])
        self.assertEqual((r["request"]["target"], r["request"]["duration_s"]), ("192.168.1.7", 3600))

    def test_unclear_requests_ask_back(self):
        from intentqos.nlintent import parse
        self.assertFalse(parse("hello", self.devices)["ok"])
        self.assertIn("which device", parse("boost the call for 10 minutes", self.devices)["reason"])

    def test_drafter_output_must_pass_the_schema(self):
        from intentqos.nlintent import draft_with
        bad = draft_with(lambda *a: {"target": "192.168.1.5", "goal": "low_latency", "duration_s": 3600,
                                     "priority": 9, "bypass_budget": True}, "x", self.devices)
        self.assertFalse(bad["ok"])
        self.assertIn("unexpected fields", bad["reason"])
        good = draft_with(lambda *a: {"target": "192.168.1.5", "goal": "throughput", "duration_s": 600}, "x", self.devices)
        self.assertTrue(good["ok"])


class UspModel(unittest.TestCase):
    def test_tr181_paths(self):
        from intentqos.usp import P, data_model
        eng = types.SimpleNamespace(snapshot={
            "version": "2.0.0", "paused": False, "config": {"backend": "htb"}, "l4s": {"mode": "off"},
            "estimator": {"down": {"rate_kbit": 90000, "state": "low"}, "up": {"rate_kbit": 18000}},
            "experience": {"apps": [{"device": "laptop", "label": "voip", "qoo": 88.0, "latency_ms": {"99": 40.0},
                                     "bottleneck": {"where": "line"}}], "path": {"rtt_ms": {"50": 12.0}}},
            "outcome": {"objective": 88.0, "worst_protected": 88.0, "gated": 0, "enabled": True},
            "intents": [], "budget": {"daily_min": 240}, "safety": {"rollbacks": 0, "shield_interventions": 0}})
        m = data_model(eng)
        v = m["values"]
        self.assertEqual(v[P + "AppNumberOfEntries"], 1)
        self.assertEqual(v[P + "App.1.Class"], "voip")
        self.assertEqual(v[P + "App.1.LatencyP99"], 40.0)
        self.assertEqual(m["schema"][P + "Enable"]["access"], "readWrite")


if __name__ == "__main__":
    unittest.main()
