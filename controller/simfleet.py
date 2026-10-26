"""SIMULATED fleet: hundreds of homes that speak the real agent protocol to the controller.

Each simulated gateway enrols with its claim code, signs every request with its device secret and
sends telemetry shaped like a real gateway's (evening peaks, household types, plan rates). It answers
commands (config, model, consent level, line test) the way a gateway would. It exists so the
controller's fleet features -- incident correlation, rollouts with guardrails, reports -- can be
demonstrated at scale next to the one real lab gateway. Everything it produces is labelled
"simulated" in the controller.

    python3 -m controller.simfleet --config controller/controller.toml [--story]

Scenario control (localhost only):  POST http://127.0.0.1:8095/scenario
    {"kind": "olt_congestion", "olt": "GN-OLT-07", "minutes": 8, "severity": 0.35}
    {"kind": "olt_outage", "olt": "GZ-OLT-25", "minutes": 6}
    {"kind": "clear"}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controller.server import load_config  # noqa: E402
from intentqos.agent import canonical, sign_request  # noqa: E402

DEV_SETS = {
    "light": [("Phone", "phone"), ("Phone", "phone"), ("TV", "tv")],
    "family": [("Phone", "phone"), ("Phone", "phone"), ("Tablet", "tablet"), ("Living-room TV", "tv"), ("Laptop", "laptop")],
    "wfh": [("Work laptop", "laptop"), ("Phone", "phone"), ("Phone", "phone"), ("TV", "tv")],
    "gamer": [("Console", "console"), ("Gaming PC", "desktop"), ("Phone", "phone"), ("TV", "tv")],
    "streamer": [("TV", "tv"), ("Bedroom TV", "tv"), ("Phone", "phone"), ("Tablet", "tablet"), ("Phone", "phone")],
}
ACT = ["Streaming video", "Browsing", "Idle", "Idle", "On a video call", "Downloading or backing up", "Gaming"]


def diurnal(t: float) -> float:
    """Evening-peaked household load in IST (0.15 at 4am .. 1.0 at 21:30)."""
    h = (time.gmtime(t).tm_hour + time.gmtime(t).tm_min / 60 + 5.5) % 24
    return 0.15 + 0.85 * max(0.0, math.exp(-((h - 21.5) ** 2) / 8) + 0.35 * math.exp(-((h - 13) ** 2) / 10))


class Home:
    def __init__(self, spec: dict, rnd: random.Random):
        self.s = spec
        self.rnd = random.Random(rnd.random())
        self.dev_id = self.secret = None
        self.config_version = 0
        self.model = "2026.10.01"
        self.prev_model = None
        self.level = "standard"
        self.results: list = []
        self.next_t = time.time() + self.rnd.uniform(0, 15)
        self.devs = [dict(name=n, type=t, ip=f"192.168.1.{10 + i}") for i, (n, t) in enumerate(DEV_SETS[spec["household"]])]
        self.base_add = self.rnd.uniform(0.5, 4.0)
        self.events: list = []


class Fleet:
    def __init__(self, cfg: dict, url: str, ca: str | None):
        self.cfg = cfg
        self.url = url.rstrip("/")
        self.ctx = ssl.create_default_context(cafile=ca) if ca else None
        specs = json.load(open(os.path.join(cfg["data_dir"], "sim-homes.json")))
        rnd = random.Random(5)
        self.homes = [Home(s, rnd) for s in specs]
        # like real routers, simulated homes keep their credentials and installed versions across restarts
        self.state_path = os.path.join(cfg["data_dir"], "sim-state.json")
        try:
            saved = json.load(open(self.state_path))
        except (FileNotFoundError, ValueError):
            saved = {}
        for h in self.homes:
            st = saved.get(h.s["serial"])
            if st:
                h.dev_id, h.secret = st.get("dev_id"), st.get("secret")
                h.model, h.prev_model = st.get("model", h.model), st.get("prev_model")
                h.config_version = st.get("config_version", 0)
        self._saved = 0.0
        self.scen: list[dict] = []
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=12)
        self.sent = 0
        self.errors = 0

    # ---------------------------------------------------------------- protocol
    def _post(self, h: Home | None, path: str, body: dict, signed: bool = True):
        data = canonical(body)
        hd = {"Content-Type": "application/json", "User-Agent": "IntentQoS-simulated-agent"}
        if signed:
            ts, nonce = str(int(time.time())), os.urandom(8).hex()
            hd.update({"X-IQ-Device": h.dev_id, "X-IQ-Time": ts, "X-IQ-Nonce": nonce,
                       "X-IQ-Signature": sign_request(bytes.fromhex(h.secret), "POST", path, data, ts, nonce)})
        req = urllib.request.Request(self.url + path, data=data, headers=hd, method="POST")
        with urllib.request.urlopen(req, timeout=15, context=self.ctx) as r:
            return json.loads(r.read())

    def enroll(self, h: Home) -> None:
        r = self._post(None, "/v1/agent/enroll", {"serial": h.s["serial"], "claim_code": h.s["claim"], "hw_model": "SIM-ONT",
                                                  "arch": "simulated", "agent_version": "1.0.0", "engine": {"intentqos": "1.0.0"}},
                       signed=False)
        h.dev_id, h.secret = r["device_id"], r["secret"]

    # ---------------------------------------------------------------- scenarios
    def active(self, olt: str, kind: str):
        t = time.time()
        with self.lock:
            for s in self.scen:
                if s["kind"] == kind and s["olt"] == olt and s["start"] <= t < s["end"]:
                    return s
        return None

    # ---------------------------------------------------------------- telemetry
    def record(self, h: Home, t: float) -> dict:
        r = h.rnd
        plan_d, plan_u = h.s["down"] * 1000, h.s["up"] * 1000
        load = min(1.0, diurnal(t) * {"light": 0.5, "family": 0.8, "wfh": 0.75, "gamer": 0.85, "streamer": 0.9}[h.s["household"]]
                   * r.uniform(0.6, 1.25))
        cap = plan_d
        cong = self.active(h.s["olt"], "olt_congestion")
        if cong and h.rnd.random() < 0.9:
            cap = plan_d * cong.get("severity", 0.35)
        if h.s.get("line_fault"):
            cap = min(cap, plan_d * 0.4)
        busy = load > 0.45 or cap < plan_d
        rate_d = min(plan_d * 0.9, cap * 0.92) if busy else plan_d * 0.9
        ach = min(rate_d, plan_d * load) * r.uniform(0.85, 1.0)
        p95 = h.base_add + r.expovariate(1 / 2.5) * (1 + 2 * load)
        if cap < plan_d * 0.6:
            p95 += r.uniform(25, 90) if cong else r.uniform(10, 40)
        if h.model.endswith("-exp"):
            p95 += r.uniform(15, 45)
        grade = "A+" if p95 < 5 else "A" if p95 < 30 else "B" if p95 < 60 else "C" if p95 < 200 else "D"
        state = "bloat" if (cap < plan_d * 0.6 and r.random() < 0.4) else ("high" if busy else "low")
        tin = {"voice": round(r.uniform(50, 300)), "video": round(ach * r.uniform(0, 0.15)),
               "besteffort": round(ach * r.uniform(0.2, 0.6))}
        tin["bulk"] = max(0, round(ach - sum(tin.values())))
        rec = {"t": round(t, 1), "n": 30, "grade": grade, "p95_add_d": round(p95, 1), "p95_add_u": round(h.base_add * r.uniform(0.5, 2), 1),
               "add_d": round(p95 * 0.5, 2), "add_u": round(h.base_add * 0.4, 2), "add_d_max": round(p95 * 1.6, 1),
               "jit_d": round(r.uniform(0.2, 2.5) + p95 / 30, 2), "jit_u": round(r.uniform(0.2, 1.5), 2),
               "loss_d": round(r.uniform(0, 0.4) + (3 if cap < plan_d * 0.6 else 0), 3), "loss_u": round(r.uniform(0, 0.3), 3),
               "rate_d": round(rate_d), "rate_u": round(plan_u * 0.92), "ach_d": round(ach), "ach_u": round(plan_u * load * 0.3),
               "ach_d_max": round(ach * 1.1), "q_max": round(r.uniform(0, 20), 1), "jain": round(r.uniform(0.7, 1), 3) if load > 0.5 else None,
               "flows": int(5 + load * 30 * r.uniform(0.5, 1.5)), "devices": max(1, int(len(h.devs) * load + 0.5)),
               "leases": 1 if (h.s["household"] == "wfh" and 0.3 < load and r.random() < 0.3) else 0, "tin_d": tin,
               "est": {"d": state, "u": "low", "blind": False, "enabled": True}, "plan": {"d": plan_d, "u": plan_u},
               "safety": {"rollbacks": 0, "fail": 0, "shield": 0, "changes": int(r.uniform(0, 12))},
               "paused": False, "cap": {"drops": 0, "backend": "native helper"}, "cpu": round(r.uniform(1.5, 6) * (1 + load), 1),
               "rss": round(r.uniform(22, 30), 1),
               "classifier": {"mode": "ml", "abstain": 0, "ml": 0, "drift": 0},
               "versions": {"engine": "1.0.0", "agent": "1.0.0", "config": h.config_version, "model": h.model},
               "level": h.level, "simulated": True, "events": h.events[-5:]}
        h.events = []
        if h.level in ("standard", "diagnostic"):
            rec["dev"] = [{"ip": d["ip"], "name": d["name"], "type": d["type"],
                           "act": r.choice(ACT) if r.random() < load else "Idle", "kd": round(ach / len(h.devs) * r.uniform(0, 2), 1),
                           "ku": round(r.uniform(0, 500), 1), "fl": int(r.uniform(0, 8)), "today": int(r.uniform(1e7, 3e9)),
                           "paused": False, "lease": False} for d in h.devs]
            rec["intents"] = []
        return rec

    # ---------------------------------------------------------------- commands
    def execute(self, h: Home, c: dict) -> dict:
        k, a = c["kind"], c.get("args") or {}
        if k == "apply_config":
            h.config_version = a.get("version", h.config_version)
            h.events.append({"t": time.time(), "kind": "config", "level": "info", "msg": f"managed config v{h.config_version} applied"})
            return {"ok": True, "engine": {"version": h.config_version}}
        if k == "install_model":
            h.prev_model, h.model = h.model, a["version"]
            h.events.append({"t": time.time(), "kind": "model", "level": "info", "msg": f"classifier model switched to {a['version']}"})
            return {"ok": True, "version": a["version"]}
        if k == "rollback_model":
            h.model = a.get("version") or h.prev_model or "2026.10.01"
            return {"ok": True, "version": h.model}
        if k == "set_level":
            h.level = a.get("level", h.level)
            return {"ok": True, "level": h.level}
        if k == "line_test":
            plan = h.s["down"]
            cap = plan * (0.4 if h.s.get("line_fault") else (0.35 if self.active(h.s["olt"], "olt_congestion") else 0.9))
            return {"ok": True, "mbps": round(cap * h.rnd.uniform(0.85, 0.98), 1), "seconds": 8.0, "idle_added_ms": round(h.base_add, 1),
                    "loaded_added_ms_p95": round(h.base_add + h.rnd.uniform(2, 12), 1), "plan_down_mbit": plan, "simulated": True}
        if k == "relay":
            return {"ok": True, "status": 501, "body": {"error": "simulated home: the live console is available for real gateways only"}}
        if k == "snapshot":
            return {"ok": True, "snapshot": {"simulated": True}}
        return {"ok": False, "error": f"unknown command {k}"}

    # ---------------------------------------------------------------- loop
    def tick_home(self, h: Home) -> None:
        try:
            if not h.dev_id:
                self.enroll(h)
            elif getattr(h, "reenroll", False):
                h.reenroll = False
                self.enroll(h)
            t = time.time()
            if self.active(h.s["olt"], "olt_outage"):
                h.next_t = t + 15
                return
            body = {"records": [self.record(h, t)], "results": h.results, "buffered": 0, "dropped": 0,
                    "state": {"config_version": h.config_version, "model_version": h.model, "level": h.level, "agent": "1.0.0"}}
            h.results = []
            resp = self._post(h, "/v1/agent/sync", body)
            for c in resp.get("commands", []):
                h.results.append({"id": c["id"], "result": self.execute(h, c), "t": time.time()})
            if h.results:                         # report quickly, like the real agent does after acting
                self._post(h, "/v1/agent/result", {"results": h.results})
                h.results = []
            if resp.get("level") and h.level != "diagnostic":
                h.level = resp["level"]
            self.sent += 1
            h.next_t = t + float(resp.get("interval", 15)) * h.rnd.uniform(0.9, 1.1)
        except urllib.error.HTTPError as e:   # e.g. 401 after the controller was re-seeded: enrol again
            self.errors += 1
            h.next_t = time.time() + 5
            if e.code == 401:
                h.reenroll = True
        except Exception as e:  # noqa: BLE001
            self.errors += 1
            h.next_t = time.time() + 20
            if self.errors % 50 == 1:
                print(f"sim: {h.s['serial']}: {e}", flush=True)

    def save(self) -> None:
        doc = {h.s["serial"]: {"dev_id": h.dev_id, "secret": h.secret, "model": h.model, "prev_model": h.prev_model,
                               "config_version": h.config_version} for h in self.homes if h.dev_id}
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # holds device secrets
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, self.state_path)

    def run(self, story: bool) -> None:
        if story:
            self.story()
        while True:
            t = time.time()
            if t - self._saved > 10:
                try:
                    self.save()
                except OSError as e:
                    print(f"sim: could not save state: {e}", flush=True)
                self._saved = t
            due = [h for h in self.homes if h.next_t <= t]
            for h in due:
                h.next_t = t + 60                  # claimed; tick_home sets the real next time
                self.pool.submit(self.tick_home, h)
            time.sleep(0.25)

    def add(self, s: dict) -> dict:
        t = time.time()
        s = {**s, "start": t + float(s.get("delay_s", 0)), "end": t + float(s.get("delay_s", 0)) + float(s.get("minutes", 8)) * 60}
        with self.lock:
            if s["kind"] == "clear":
                self.scen = []
            else:
                self.scen.append(s)
        print(f"sim: scenario {s}", flush=True)
        return s

    def story(self) -> None:
        """A repeating evening: an OLT congestion every 40 minutes, a short outage every 90 minutes."""
        def loop():
            k = 0
            while True:
                self.add({"kind": "olt_congestion", "olt": "GN-OLT-07", "minutes": 9, "severity": 0.35, "delay_s": 180})
                if k % 2 == 1:
                    self.add({"kind": "olt_outage", "olt": "GZ-OLT-25", "minutes": 5, "delay_s": 1500})
                k += 1
                time.sleep(2400)
        threading.Thread(target=loop, daemon=True).start()


def control_server(fleet: Fleet, port: int) -> None:
    class C(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            with fleet.lock:
                sc = [s for s in fleet.scen if s["end"] > time.time()]
            self._send(200, {"homes": len(fleet.homes), "enrolled": sum(1 for h in fleet.homes if h.dev_id), "sent": fleet.sent,
                             "errors": fleet.errors, "scenarios": sc})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                self._send(200, fleet.add(json.loads(self.rfile.read(n) or b"{}")))
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})
    srv = ThreadingHTTPServer(("127.0.0.1", port), C)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(ROOT, "controller", "controller.toml"))
    ap.add_argument("--url", default="https://127.0.0.1:8443")
    ap.add_argument("--ca", default=None)
    ap.add_argument("--control-port", type=int, default=8095)
    ap.add_argument("--story", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    ca = a.ca or os.path.join(cfg["data_dir"], "pki", "ca.pem")
    fl = Fleet(cfg, a.url, ca if os.path.exists(ca) else None)
    control_server(fl, a.control_port)
    print(f"simulated fleet: {len(fl.homes)} homes -> {a.url} (control on 127.0.0.1:{a.control_port})", flush=True)
    fl.run(a.story)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
