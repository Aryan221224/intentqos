"""Tests for the router-side additions (pure-Python model, Ed25519, managed config, UCI) and the
controller (enrolment, signed requests, telemetry, alerts, incident correlation, rollout guard,
consent). No root, no network: the controller is exercised in-process.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from intentqos import config as C  # noqa: E402
from intentqos import ed25519  # noqa: E402
from intentqos.agent import canonical, sign_request  # noqa: E402
from intentqos.manage import validate  # noqa: E402
from intentqos.treemodel import TreeModel  # noqa: E402
from intentqos.uciconf import dump_toml, parse_show, to_config  # noqa: E402


class TestRouterPieces(unittest.TestCase):
    def test_ed25519_rfc8032_vector(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        _, pk = ed25519.keypair(seed)
        self.assertEqual(pk.hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = ed25519.sign(seed, b"")
        self.assertTrue(sig.hex().startswith("e5564300c360ac72"))
        self.assertTrue(ed25519.verify(pk, b"", sig))
        self.assertFalse(ed25519.verify(pk, b"tampered", sig))

    def test_pure_python_model_matches_sklearn(self):
        try:
            import joblib
            import numpy as np
        except ImportError:
            self.skipTest("scikit-learn not installed (build machine only)")
        data = os.path.join(ROOT, "ml", "data", "dataset.jsonl")
        if not os.path.exists(data):
            self.skipTest("dataset not generated")
        rows = []
        with open(data) as f:
            for line in f:
                rows.append(json.loads(line)["features"])
                if len(rows) >= 300:
                    break
        ref = joblib.load(os.path.join(ROOT, "models", "flowclf.joblib"))["model"].predict_proba(np.asarray(rows))
        tm = TreeModel(os.path.join(ROOT, "models", "flowclf.iqm"))
        got = np.asarray([tm.proba(r) for r in rows])
        self.assertLess(float(abs(ref - got).max()), 1e-6)

    def test_managed_config_allow_list(self):
        ok = validate({"link.down_mbit": "200", "classifier.mode": "rules", "estimator.reflectors": ["1.1.1.1"]})
        self.assertEqual(ok["link.down_mbit"], 200.0)
        for bad in ({"interfaces.lan": "eth9"}, {"api.listen": "0.0.0.0:80"}, {"link.down_mbit": -5},
                    {"classifier.mode": "magic"}, {"estimator.reflectors": ["not-an-ip"]}):
            with self.assertRaises(ValueError):
                validate(bad)

    def test_uci_to_toml(self):
        text = ("intentqos.engine=intentqos\nintentqos.engine.lan='br-lan'\nintentqos.engine.wan='eth0'\n"
                "intentqos.engine.lan_subnet='192.168.1.0/24' 'fd00::/64'\nintentqos.engine.down_mbit='300'\n"
                "intentqos.engine.reflector='9.9.9.9'\nintentqos.agent=intentqos\nintentqos.agent.enabled='1'\n"
                "intentqos.agent.controller_url='https://qos.example'\n")
        cfg = to_config(parse_show(text))
        import tomllib
        back = tomllib.loads(dump_toml(cfg))
        self.assertEqual(back["interfaces"]["lan_subnets"], ["192.168.1.0/24", "fd00::/64"])
        self.assertEqual(back["link"]["down_mbit"], 300.0)
        self.assertEqual(back["estimator"]["reflectors"], ["9.9.9.9"])
        self.assertEqual(back["agent"]["controller_url"], "https://qos.example")
        self.assertEqual(back["data"]["state_dir"], "/etc/intentqos")


class _Hdr(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class TestController(unittest.TestCase):
    def setUp(self):
        from controller import security as S
        from controller.core import Controller
        self.S = S
        self.tmp = tempfile.TemporaryDirectory()
        self.ctl = Controller({"data_dir": self.tmp.name, "telemetry_interval_s": 15, "brand": {}, "retention": {},
                               "auto_notify_outages": True})
        db = self.ctl.db
        db.x("INSERT INTO plans (code, name, down_mbit, up_mbit, boost_minutes) VALUES ('P100','Home 100',100,20,480)")
        db.x("INSERT INTO profiles (name, version, settings, created) VALUES ('default',1,'{\"classifier.mode\":\"ml\"}',0)")
        self.gws = []
        for i in range(8):
            sid = db.x("INSERT INTO subscribers (account, name, phone, plan_id, region, pop, olt) VALUES (?,?,?,1,'R','P','OLT-A')",
                       (f"A{i}", f"Sub {i}", f"98000000{i:02d}"))
            db.x("INSERT INTO gateways (serial, claim_hash, subscriber_id, profile_id) VALUES (?,?,?,1)",
                 (f"S{i}", S.h(f"C{i}"), sid))
            r = self.ctl.enroll({"serial": f"S{i}", "claim_code": f"C{i}"}, "test")
            self.gws.append((db.one("SELECT * FROM gateways WHERE serial=?", (f"S{i}",)), r))

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, t, grade="A+", p95=3.0, rate=90000, state="low", **kw):
        return {"t": t, "grade": grade, "p95_add_d": p95, "rate_d": rate, "plan": {"d": 100000, "u": 20000},
                "est": {"d": state, "blind": False}, "safety": {"fail": 0}, "cap": {"drops": 0}, "cpu": 3, **kw}

    def test_offline_alert_clears_after_a_long_poll_reconnect(self):
        """Regression: /wait marked the gateway online first, so sync never closed the offline alert."""
        g, _ = self.gws[0]
        self.ctl.started -= 3600                         # past the start-up grace period
        self.ctl.db.x("UPDATE gateways SET online=1, last_seen=? WHERE id=?", (time.time() - 600, g["id"]))
        self.ctl.housekeeping()
        self.assertEqual(self.ctl.db.val("SELECT COUNT(*) FROM alerts WHERE gw=? AND rule='offline' AND status='open'", (g["id"],)), 1)
        self.ctl.wait(self.ctl.gw(g["id"]), 0.01)       # the agent's long-poll arrives first
        self.ctl.sync(self.ctl.gw(g["id"]), {"records": [self.rec(time.time())]})
        self.assertEqual(self.ctl.db.val("SELECT COUNT(*) FROM alerts WHERE gw=? AND rule='offline' AND status='open'", (g["id"],)), 0)

    def test_no_offline_storm_right_after_a_restart(self):
        g, _ = self.gws[1]
        self.ctl.db.x("UPDATE gateways SET online=1, last_seen=? WHERE id=?", (time.time() - 600, g["id"]))
        self.ctl.housekeeping()                          # controller just started: stale last_seen is not "offline" yet
        self.assertEqual(self.ctl.db.val("SELECT COUNT(*) FROM alerts WHERE gw=? AND rule='offline'", (g["id"],)), 0)

    def test_enrolment_rejects_bad_claim_and_pushes_signed_config(self):
        with self.assertRaises(PermissionError):
            self.ctl.enroll({"serial": "S1", "claim_code": "wrong"}, "test")
        g, r = self.gws[0]
        cmds = self.ctl.take_commands(g["id"])
        self.assertEqual(cmds[0]["kind"], "apply_config")
        env = cmds[0]["args"]["envelope"]
        from intentqos import release
        self.assertIsNone(release.check_instruction(env, "config", r["device_id"], bytes.fromhex(r["release_pubkey"]), 0))
        self.assertEqual(env["body"]["set"]["link.down_mbit"], 100)          # plan rate comes from billing

    def test_request_signatures_and_replay(self):
        g, r = self.gws[0]
        body = canonical({"records": []})
        ts, nonce = str(int(time.time())), "n1"
        sig = sign_request(bytes.fromhex(r["secret"]), "POST", "/v1/agent/sync", body, ts, nonce)
        h = _Hdr({"X-IQ-Device": r["device_id"], "X-IQ-Time": ts, "X-IQ-Nonce": nonce, "X-IQ-Signature": sig})
        self.assertEqual(self.ctl.auth_agent(h, "POST", "/v1/agent/sync", body)["id"], g["id"])
        with self.assertRaises(PermissionError):                       # same nonce again
            self.ctl.auth_agent(h, "POST", "/v1/agent/sync", body)
        h2 = _Hdr({**h, "X-IQ-Nonce": "n2"})
        with self.assertRaises(PermissionError):                       # signature no longer matches
            self.ctl.auth_agent(h2, "POST", "/v1/agent/sync", body)

    def test_alerts_open_and_correlate_into_an_incident(self):
        t = time.time()
        for k in range(5):
            for i, (g, _) in enumerate(self.gws):
                bad = i < 5
                self.ctl.sync(g, {"records": [self.rec(t + k * 15, grade="C" if bad else "A",
                                                       p95=80 if bad else 4, rate=30000 if bad else 90000,
                                                       state="bloat" if bad else "low")], "state": {}})
        open_ = self.ctl.db.q("SELECT gw, rule FROM alerts WHERE status='open'")
        self.assertTrue(any(a["rule"] == "capacity_shortfall" for a in open_))
        self.ctl.correlate()
        inc = self.ctl.db.one("SELECT * FROM incidents")
        self.assertIsNotNone(inc)
        self.assertEqual(inc["scope"], "OLT-A")
        self.assertEqual(inc["affected"], 5)
        d = self.ctl.diagnose(self.gws[0][0]["id"])
        self.assertEqual(d["where"], "network")

    def test_rollout_guard_halts_a_regression(self):
        self.ctl.db.x("INSERT INTO models (version, sha256, size, path, metrics, signature, created) VALUES "
                      "('bad','00',1,'/dev/null','{}','00',0)")
        rid = self.ctl.create_rollout("model", "bad", "bad model", [{"pct": 60, "min_minutes": 0}], "test")
        members = {m["gw"] for m in self.ctl.db.q("SELECT gw FROM rollout_members WHERE rollout_id=?", (rid,))}
        self.assertTrue(0 < len(members) < 8)
        self.ctl.db.x("UPDATE rollout_members SET status='ok'")
        t = time.time() + 1
        for k in range(3):
            for g, _ in self.gws:
                p = 40.0 + k if g["id"] in members else 4.0 + k * 0.1
                self.ctl.db.x("INSERT INTO telemetry (gw, t, rec) VALUES (?,?,?)", (g["id"], t + k, json.dumps({"p95_add_d": p})))
        ev = self.ctl.rollout_eval(rid)
        if len(members) >= 3 and 8 - len(members) >= 3:
            self.assertTrue(ev["breach"])
            self.ctl.rollout_tick()
            self.assertEqual(self.ctl.db.val("SELECT status FROM rollouts WHERE id=?", (rid,)), "rolled_back")

    def test_consent_controls_telemetry_level(self):
        g, _ = self.gws[0]
        self.ctl.set_consent(g["subscriber_id"], {"quality_improvement": False}, "A0")
        resp = self.ctl.sync(self.ctl.gw(g["id"]), {"records": [], "state": {}})
        self.assertEqual(resp["level"], "basic")
        self.assertTrue(any(c["kind"] == "set_level" for c in resp["commands"]))


if __name__ == "__main__":
    unittest.main()


class TestAccessControl(unittest.TestCase):
    """Router key on the engine API, staff sign-in lockout, lab quick sign-in only from this machine."""

    def _engine_api(self, require):
        import threading
        from http.server import ThreadingHTTPServer
        from intentqos import api as A
        c = C.load(None)
        c["_root"] = tempfile.mkdtemp()
        c["api"]["require_auth"] = require
        c["api"]["key_file"] = "data/admin.key"
        os.environ.pop(c["api"]["token_env"], None)

        class Eng:
            cfg = c
            snapshot = {"ok": 1}
            running = True

            def emit(self, *a):
                pass
        eng = Eng()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), A.make_handler(eng))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return c, f"http://127.0.0.1:{srv.server_address[1]}"

    @staticmethod
    def _call(url, method="GET", body=None, headers=None):
        import urllib.error
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with op.open(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), e.headers

    def test_router_key_guards_the_engine_api(self):
        c, url = self._engine_api(require=True)
        key = open(os.path.join(c["_root"], "data/admin.key")).read().strip()
        self.assertEqual(oct(os.stat(os.path.join(c["_root"], "data/admin.key")).st_mode & 0o777), "0o600")
        self.assertEqual(self._call(url + "/api/state")[0], 401)
        self.assertEqual(self._call(url + "/api/healthz")[0], 200)
        self.assertEqual(self._call(url + "/api/session", "POST", {"key": "AAAA-BBBB-CCCC-DDDD"})[0], 401)
        self.assertEqual(self._call(url + "/api/state", headers={"Authorization": "Bearer " + key})[0], 200)
        code, _b, h = self._call(url + "/api/session", "POST", {"key": key.lower()})
        self.assertEqual(code, 200)
        cookie = h["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertEqual(self._call(url + "/api/state", headers={"Cookie": cookie})[0], 200)

    def test_lab_engine_stays_open_without_require_auth(self):
        _c, url = self._engine_api(require=False)
        self.assertEqual(self._call(url + "/api/state")[0], 200)
        self.assertEqual(self._call(url + "/api/session")[1], {"auth_required": False, "signed_in": True})

    def test_staff_lockout(self):
        from controller.server import Lockout
        lk = Lockout(limit=3, window=60)
        for _ in range(3):
            self.assertEqual(lk.blocked("eve", "10.0.0.9"), 0)
            lk.fail("eve", "10.0.0.9")
        self.assertGreater(lk.blocked("eve", "10.0.0.9"), 0)
        self.assertGreater(lk.blocked("someone-else", "10.0.0.9"), 0, "same address is blocked too")
        self.assertEqual(lk.blocked("admin", "10.0.0.7"), 0)

