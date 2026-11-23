"""Home-kit tests: the Python 3.9 config reader, the controller's kit mode (pairing, PIN lockout, reports),
the router installer's shell syntax and the router payload. Stdlib only, no root, no network.

    python3 -m unittest tests.test_kit -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from intentqos import config as C  # noqa: E402
from intentqos import uciconf as U  # noqa: E402


class MiniToml(unittest.TestCase):
    def test_matches_tomllib_on_shipped_configs(self):
        for f in ("config/testbed.toml", "config/example-router.toml"):
            p = os.path.join(ROOT, f)
            with open(p, "rb") as fh:
                self.assertEqual(C._mini_toml(open(p).read()), tomllib.load(fh), f)

    def test_matches_tomllib_on_router_config(self):
        cfg = U.to_config(U.parse_show("intentqos.agent=intentqos\nintentqos.agent.enabled='1'\n"
                                       "intentqos.agent.controller_url='https://192.168.8.10:8443'\n"
                                       "intentqos.agent.claim_code='KIT-ab'\nintentqos.agent.discover='1'\n"))
        text = U.dump_toml(cfg)
        self.assertEqual(C._mini_toml(text), tomllib.loads(text))
        self.assertTrue(cfg["agent"]["discover"])
        self.assertEqual(cfg["agent"]["engine_api"], "http://192.168.1.1:8080")   # the API listens on the LAN address


class KitMode(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, "pki"))
        with open(os.path.join(self.d, "pki", "ca.pem"), "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n")
        from controller.core import Controller
        from controller.kit import Kit
        from controller.server import DEFAULTS
        cfg = dict(DEFAULTS, data_dir=self.d, listen="0.0.0.0:8443", ui_listen="0.0.0.0:8088", kit={"enabled": True})
        self.ctl = Controller(cfg)
        db = self.ctl.db
        db.x("INSERT INTO plans (code, name, down_mbit, up_mbit, boost_minutes, price_inr) VALUES ('HOME-100','Home 100',100,20,480,699)")
        db.x("INSERT INTO profiles (name, version, settings, description, created_by, created) VALUES ('default',1,'{}','','t',0)")
        self.kit = Kit(self.ctl, cfg)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_router_pairing_creates_gateway_plan_and_household(self):
        r = self.kit.pair({"pin": self.kit.pin, "role": "router", "serial": "IQ-AABBCCDDEEFF", "lan_ip": "192.168.8.1",
                           "router_key": "K", "down_mbit": 100, "up_mbit": 100}, "192.168.8.20")
        self.assertTrue(r["ok"])
        self.assertTrue(r["claim_code"].startswith("KIT-"))
        self.assertIn("BEGIN CERTIFICATE", r["ca_pem"])
        self.assertEqual(len(r["release_pubkey"]), 64)
        g = self.ctl.db.one("SELECT * FROM gateways WHERE serial='IQ-AABBCCDDEEFF'")
        self.assertIsNotNone(g)
        plan = self.ctl.db.one("SELECT p.* FROM plans p JOIN subscribers s ON s.plan_id=p.id WHERE s.id=?", (g["subscriber_id"],))
        self.assertEqual((plan["down_mbit"], plan["up_mbit"]), (100, 100))       # a plan matching the real line
        self.assertEqual(self.kit.routers(with_keys=True)[0]["key"], "K")

    def test_wrong_pins_lock_pairing(self):
        for _ in range(5):
            with self.assertRaises(PermissionError):
                self.kit.pair({"pin": "000000" if self.kit.pin != "000000" else "111111", "role": "testkit"}, "10.0.0.2")
        with self.assertRaises(PermissionError) as e:
            self.kit.pair({"pin": self.kit.pin, "role": "testkit"}, "10.0.0.2")          # even the right PIN, for 5 minutes
        self.assertIn("locked", str(e.exception))

    def test_testkit_token_and_reports(self):
        r = self.kit.pair({"pin": self.kit.pin, "role": "testkit", "name": "Test laptop"}, "10.0.0.2")
        who = self.kit.token_ok("Bearer " + r["token"])
        out = self.kit.save_result(who, {"title": "Line test", "verdict": "better", "summary": "p99 80 -> 13 ms"})
        self.assertEqual(self.kit.results()[0]["id"], out["id"])
        self.assertEqual(self.kit.result(out["id"])["uploaded_by"], "Test laptop")
        with self.assertRaises(PermissionError):
            self.kit.token_ok("Bearer kt_wrong")

    def test_bad_serial_rejected(self):
        with self.assertRaises(ValueError):
            self.kit.pair({"pin": self.kit.pin, "role": "router", "serial": "x; rm -rf /"}, "10.0.0.2")


class RouterPayload(unittest.TestCase):
    def test_installer_scripts_parse(self):
        sh = shutil.which("busybox") and ["busybox", "sh"] or (shutil.which("dash") and ["dash"]) or ["sh"]
        for f in ("router/kit/install.sh", "router/kit/files/usr/lib/intentqos/uninstall.sh",
                  "router/kit/files/usr/sbin/intentqos-ctl", "router/openwrt/files/intentqos.init"):
            r = subprocess.run(sh + ["-n", os.path.join(ROOT, f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f}: {r.stderr}")

    def test_payload_contents(self):
        out = tempfile.mkdtemp()
        try:
            p = subprocess.run([sys.executable, os.path.join(ROOT, "router", "build_payload.py"), out], check=True,
                               capture_output=True, text=True).stdout.split()[0]
            names = tarfile.open(p).getnames()
            for need in ("intentqos-router/install.sh", "intentqos-router/bin/iq-capture.aarch64",
                         "intentqos-router/files/usr/lib/intentqos/intentqos/daemon.py",
                         "intentqos-router/files/usr/lib/intentqos/models/flowclf.iqm",
                         "intentqos-router/files/etc/init.d/intentqos", "intentqos-router/files/usr/lib/intentqos/uninstall.sh"):
                self.assertIn(need, names)
            self.assertFalse(any("__pycache__" in n for n in names))
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
