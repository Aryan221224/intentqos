"""Lab server: the console's bridge to the emulated home network.

Runs in the root namespace (as root) next to the testbed and

  * serves the console and proxies /api/* and /metrics to the engine in the gateway namespace,
  * lets the console inject conditions by hand: change the ISP link rate, start/stop household
    traffic presets (video call, ISO download, cloud backup, three TVs, a game, browsing),
    restart or kill the engine (to show the watchdog), switch classifier,
  * runs baseline-vs-optimised experiments (testbed/scenarios.py) from the console and
    serves their reports and figures.

    sudo python3 testbed/testbed.py up
    sudo python3 -m testbed.labd            # then open http://<host>:8090/

GET  /lab/status                                   testbed, engine, ISP rate, running traffic, experiment job
POST /lab/condition   {"down":20,"up":5}            change the emulated access-link capacity
POST /lab/traffic     {"preset":"iso","action":"start"|"stop"}
POST /lab/reset                                    stop all traffic, link back to the plan rate
POST /lab/engine      {"action":"restart"|"kill"|"stop", "classifier":"ml"|"rules"}
GET  /lab/experiments                              scenario catalogue, committed results, lab jobs
POST /lab/experiments/run {"scenario":"s2_wan_drop","seeds":1,"arms":["noqos","adaptive_ml"]}
GET  /lab/experiments/<id>                         job progress and, when done, its summary
GET  /lab/figures/<results|lab/<id>>/<file>.png    report figures
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from testbed import testbed as TB  # noqa: E402
from testbed.scenarios import ARMS, SCENARIOS  # noqa: E402

PY = sys.executable
CONSOLE = os.path.join(ROOT, "intentqos", "web", "console.html")
CONFIG = os.path.join(ROOT, "config", "testbed.toml")
ENGINE = f"http://{TB.GW_MGMT_IP}:8080"
LAB_RESULTS = os.path.join(ROOT, "results-lab")

PRESETS = {
    "call": {"name": "Video call on the laptop", "flows": [
        ("voip", "laptop", {}), ("video", "laptop", {"kbps": 1800, "down_kbps": 2200})]},
    "iso": {"name": "ISO download on the desktop", "flows": [("bulk", "desktop", {"conns": 6, "direction": "down"})]},
    "backup": {"name": "Cloud backup from the NAS", "flows": [("bulk", "nas", {"conns": 2, "direction": "up"})]},
    "tvs": {"name": "Three TVs streaming", "flows": [
        ("abr", "tv1", {"ladder": "1.5,3,6,10,16"}), ("abr", "tv2", {"ladder": "1.5,3,6,10,16"}),
        ("abr", "tv3", {"ladder": "1.5,3,6,10,16"})]},
    "game": {"name": "Online game on the console", "flows": [("gaming", "console", {})]},
    "browse": {"name": "Browsing on the phone", "flows": [("web", "phone", {})]},
}

# The 80 s video demo (the lab console's "Play the demo"): the golden path of scripts/demo.py, driven through the
# same lab controls, with a caption per step for the person watching.
DEMO_STEPS = [
    (0, "traffic", "call", "A work video call starts on the laptop."),
    (2, "traffic", "tvs", "Three TVs start streaming."),
    (4, "traffic", "game", "An online game starts on the console."),
    (10, "traffic", "iso", "A big ISO download starts on the desktop."),
    (12, "traffic", "backup", "A cloud backup starts on the NAS. Bulk moves to the bottom lane; the call stays smooth."),
    (28, "rate", (20, 5), "The fibre drops to 20/5 Mbit/s. IntentQoS measures the new line from delay and reshapes."),
    (44, "lease", None, "\"Boost my work call\": a 30-minute lease for the laptop, inside the household's budget."),
    (62, "nominal", None, "The line recovers. The shaper probes back up in small steps."),
    (80, "end", None, "Demo finished: the call stayed usable through a download, a backup and a line drop."),
]


SCEN_INFO = {
    "s1_iso_during_call": {"short": "ISO download during a video call", "duration_s": 50,
                           "story": "A large ISO download and a cloud backup start ten seconds into a video call."},
    "s2_wan_drop": {"short": "Link drops from 100 to 20 Mbit/s", "duration_s": 55,
                    "story": "Mid-evening, the ISP link drops from 100/20 to 20/5 Mbit/s while a call, a TV, a "
                             "download and a backup are running."},
    "s3_tvs_and_gaming": {"short": "Three TVs, a game and an update", "duration_s": 55,
                          "story": "Three TVs stream adaptive video while a console plays online and downloads an "
                                   "update."},
    "s4_intent": {"short": "Prioritise one work call", "duration_s": 55,
                  "story": "Two video calls compete on a 3 Mbit/s uplink with a backup; one call is prioritised "
                           "through the intent API."},
    "s5_faults": {"short": "Engine crash and lost reflectors", "duration_s": 60,
                  "story": "The engine is killed with SIGKILL (the watchdog must restore a safe state), then "
                           "restarted; later the delay reflectors are black-holed (blind mode)."},
}
ARM_TEXT = {"noqos": "No QoS (modem buffer only)", "static": "Static QoS (fixed shaper, rules)",
            "adaptive_rules": "IntentQoS, rule classifier", "adaptive_ml": "IntentQoS, ML classifier",
            "adaptive_ml_intent": "IntentQoS + intent lease"}


class Lab:
    def __init__(self, logdir: str, engine_python: str = PY, engine_sets: list[str] | None = None):
        self.engine_python = engine_python
        self.engine_sets = engine_sets or []
        self.logdir = logdir
        os.makedirs(logdir, exist_ok=True)
        self.lock = threading.RLock()
        self.engine: subprocess.Popen | None = None
        self.watchdog: subprocess.Popen | None = None
        self.server: subprocess.Popen | None = None
        self.traffic: dict[str, dict] = {}
        self.classifier = "ml"
        self.job: dict | None = None
        self.jobs: list[dict] = []
        self.traffic_out = os.path.join(logdir, "traffic")
        self.agent: subprocess.Popen | None = None
        self.speedsrv: subprocess.Popen | None = None
        self.with_agent = False
        self.demo: dict = {"running": False}
        self._demo_stop = threading.Event()

    # ------------------------------------------------------------ the 80 s video demo
    def demo_start(self) -> dict:
        with self.lock:
            if self.demo.get("running"):
                return self.demo_status()
            self._demo_stop = threading.Event()
            self.demo = {"running": True, "started": time.time(), "caption": "Starting the demo…", "step": 0,
                         "of": len(DEMO_STEPS), "length_s": DEMO_STEPS[-1][0]}
        threading.Thread(target=self._demo_run, args=(self._demo_stop,), daemon=True, name="lab-demo").start()
        return self.demo_status()

    def demo_stop(self) -> None:
        self._demo_stop.set()
        with self.lock:
            self.demo = {"running": False, "caption": "Demo stopped."}

    def demo_status(self) -> dict:
        with self.lock:
            d = dict(self.demo)
        if d.get("running"):
            d["t"] = round(time.time() - d["started"], 1)
        return d

    def _cancel_demo_leases(self) -> None:
        try:
            with urllib.request.urlopen(ENGINE + "/api/state", timeout=6) as r:
                st = json.load(r)
            for it in st.get("intents") or []:
                if isinstance(it, dict) and it.get("requester") == "demo":
                    urllib.request.urlopen(urllib.request.Request(ENGINE + "/api/intents/" + it["id"], method="DELETE"), timeout=6).close()
        except Exception:
            pass

    def _demo_run(self, stop: threading.Event) -> None:
        t0 = time.time()
        try:
            self.traffic_stop()
            self._cancel_demo_leases()                # a retake starts clean
            st = TB.status()
            nominal = (st.get("nominal_down", 100), st.get("nominal_up", 20))
            TB.set_isp_rate(*nominal)
            for i, (at, kind, arg, caption) in enumerate(DEMO_STEPS):
                if stop.wait(max(0.0, t0 + at - time.time())):
                    return
                try:
                    if kind == "traffic":
                        self.traffic_start(arg, 120)
                    elif kind == "rate":
                        TB.set_isp_rate(*arg)
                    elif kind == "nominal":
                        TB.set_isp_rate(*nominal)
                    elif kind == "lease":
                        body = json.dumps({"target": "laptop", "duration_s": 1800, "goal": "low_latency", "priority": 3,
                                           "requester": "demo", "label": "work call"}).encode()
                        req = urllib.request.Request(ENGINE + "/api/intents", data=body, method="POST",
                                                     headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=6) as r:
                            json.load(r)
                except Exception as e:                # a step that fails is reported, the demo goes on
                    caption += f" ({e})"
                with self.lock:
                    if stop.is_set():
                        return
                    self.demo.update(caption=caption, step=i + 1)
            with self.lock:
                self.demo.update(running=False)
        except Exception as e:
            with self.lock:
                self.demo = {"running": False, "caption": f"Demo stopped: {e}"}

    # ------------------------------------------------------------ processes
    def _spawn(self, ns: str, args: list[str], log: str) -> subprocess.Popen:
        out = open(os.path.join(self.logdir, log), "a")
        return subprocess.Popen(["ip", "netns", "exec", ns] + args, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)

    @staticmethod
    def _kill(p: subprocess.Popen | None, sig=signal.SIGTERM, wait: float = 3.0) -> None:
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, sig)
            p.wait(timeout=wait)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def ensure_server(self) -> None:
        if self.server is None or self.server.poll() is not None:
            shutil.rmtree(self.traffic_out, ignore_errors=True)
            os.makedirs(self.traffic_out, exist_ok=True)
            self.server = self._spawn(TB.NS_SRV, [PY, "-m", "testbed.traffic", "server", "--out", self.traffic_out],
                                      "server.log")
            time.sleep(0.5)

    def start_agent(self) -> None:
        """The router agent (connects the lab gateway to the controller), when the lab runs with --agent."""
        with self.lock:
            if not self.with_agent or (self.agent is not None and self.agent.poll() is None):
                return
            self.agent = self._spawn(TB.NS_GW, [self.engine_python, "-m", "intentqos.agent", "--config", CONFIG], "agent.log")

    def start_speedsrv(self) -> None:
        if self.speedsrv is None or self.speedsrv.poll() is not None:
            self.speedsrv = self._spawn(TB.NS_SRV, [PY, "-m", "testbed.speedsrv"], "speedsrv.log")

    def start_engine(self, classifier: str | None = None) -> None:
        with self.lock:
            if classifier:
                self.classifier = classifier
            self._kill(self.engine)
            extra = []
            for kv in self.engine_sets:
                extra += ["--set", kv]
            self.engine = self._spawn(TB.NS_GW, [self.engine_python, "-m", "intentqos.daemon", "--config", CONFIG,
                                                 "--set", f"classifier.mode={self.classifier}"] + extra, "engine.log")
            if self.watchdog is None or self.watchdog.poll() is not None:
                self.watchdog = self._spawn(TB.NS_GW, [self.engine_python, "-m", "intentqos.watchdog", "--config", CONFIG,
                                                       "--interval", "0.25"], "watchdog.log")
        self.start_agent()

    def kill_engine(self) -> None:
        """SIGKILL, so the engine cannot clean up: the watchdog has to restore the safe state."""
        with self.lock:
            if self.engine and self.engine.poll() is None:
                os.killpg(self.engine.pid, signal.SIGKILL)

    def stop_engine(self) -> None:
        with self.lock:
            self._kill(self.engine)
            self._kill(self.watchdog)

    def engine_running(self) -> bool:
        return self.engine is not None and self.engine.poll() is None

    # ------------------------------------------------------------ traffic presets
    def traffic_start(self, preset: str, duration: float = 3600.0) -> dict:
        if preset not in PRESETS:
            raise ValueError(f"preset must be one of {list(PRESETS)}")
        with self.lock:
            self.traffic_stop(preset)
            self.ensure_server()
            procs = []
            for i, (kind, dev, opts) in enumerate(PRESETS[preset]["flows"]):
                name = f"lab_{preset}_{i}_{uuid.uuid4().hex[:4]}"
                args = [PY, "-m", "testbed.traffic", kind, "--name", name, "--src", TB.device_ip(dev),
                        "--dst", TB.SERVER4, "--duration", str(duration), "--out", self.traffic_out,
                        "--seed", str(int(time.time()) % 1000 + i)]
                for k, v in opts.items():
                    args += [f"--{k.replace('_', '-')}", str(v)]
                procs.append(self._spawn(TB.NS_CL, args, "traffic.log"))
            self.traffic[preset] = {"procs": procs, "started": time.time(), "duration": duration}
            return {"preset": preset, "flows": len(procs)}

    def traffic_stop(self, preset: str | None = None) -> None:
        with self.lock:
            for k in ([preset] if preset else list(self.traffic)):
                t = self.traffic.pop(k, None)
                if t:
                    for p in t["procs"]:
                        self._kill(p, wait=1.0)
            # per-packet records of stopped flows are not needed by the live console
            for f in os.listdir(self.traffic_out) if os.path.isdir(self.traffic_out) else []:
                if f.endswith(".json"):
                    try:
                        os.remove(os.path.join(self.traffic_out, f))
                    except OSError:
                        pass

    def traffic_status(self) -> dict:
        out = {}
        with self.lock:
            for k, t in list(self.traffic.items()):
                alive = sum(1 for p in t["procs"] if p.poll() is None)
                if not alive:
                    self.traffic.pop(k)
                    continue
                out[k] = {"name": PRESETS[k]["name"], "flows": alive,
                          "remaining_s": round(t["started"] + t["duration"] - time.time())}
        return out

    # ------------------------------------------------------------ experiments
    def run_experiment(self, scenario: str, seeds: int, arms: list[str] | None) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {list(SCENARIOS)}")
        if self.job and self.job["state"] == "running":
            raise ValueError("an experiment is already running")
        allowed = SCENARIOS[scenario][1]
        arms = [a for a in (arms or allowed) if a in allowed] or allowed
        seeds = max(1, min(10, int(seeds)))
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + scenario.split("_")[0]
        out = os.path.join(LAB_RESULTS, jid)
        job = {"id": jid, "scenario": scenario, "arms": arms, "seeds": seeds, "state": "running",
               "started": time.time(), "total": seeds * len(arms), "done": 0, "log": [], "out": out,
               "eta_s": seeds * len(arms) * (SCEN_INFO[scenario]["duration_s"] + 8)}
        self.job = job
        self.jobs.insert(0, job)
        threading.Thread(target=self._job, args=(job,), daemon=True).start()
        return self._public(job)

    def _job(self, job: dict) -> None:
        # the experiment needs the testbed to itself: live traffic and the live engine stop for its duration
        self.traffic_stop()
        self.stop_engine()
        if self.server:
            self._kill(self.server)
            self.server = None
        try:
            cmd = [PY, "-m", "testbed.run_eval", "--reuse-testbed", "--scenarios", job["scenario"],
                   "--seeds", str(job["seeds"]), "--arms", ",".join(job["arms"]), "--out", job["out"]]
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            job["pid"] = p.pid
            for line in p.stdout:
                line = line.rstrip()
                if re.search(r"\] seed \d+ ", line):
                    job["done"] += 1
                    job["log"].append(line.split("] ", 1)[-1])
            p.wait()
            job["log"].append("computing statistics and figures")
            r = subprocess.run([PY, "-m", "testbed.report", "--results", job["out"]], cwd=ROOT,
                               capture_output=True, text=True)
            job["state"] = "done" if r.returncode == 0 else "failed"
            if r.returncode:
                job["log"].append((r.stderr or r.stdout)[-400:])
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["log"].append(f"{type(e).__name__}: {e}")
        finally:
            job["finished"] = time.time()
            TB.reset_gateway()
            TB.set_isp_rate(100, 20)
            self.start_engine()

    @staticmethod
    def _public(job: dict) -> dict:
        d = {k: v for k, v in job.items() if k not in ("out",)}
        d["log"] = job["log"][-12:]
        return d

    def job_detail(self, jid: str) -> dict | None:
        job = next((j for j in self.jobs if j["id"] == jid), None)
        if job is None:
            return None
        d = self._public(job)
        sp = os.path.join(job["out"], "summary.json")
        if os.path.exists(sp):
            with open(sp) as f:
                d["summary"] = json.load(f)
            d["figures"] = sorted(f"lab/{jid}/{x}" for x in os.listdir(job["out"]) if x.endswith(".png"))
        return d


def catalogue() -> list[dict]:
    out = []
    for k, (fn, arms) in SCENARIOS.items():
        out.append({"id": k, **SCEN_INFO.get(k, {}), "doc": (fn.__doc__ or "").strip(), "arms": arms,
                    "arm_text": {a: ARM_TEXT.get(a, a) for a in arms}})
    return out


def make_handler(lab: Lab):
    class H(BaseHTTPRequestHandler):
        server_version = "IntentQoS-lab"
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body, default=str)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if 0 < n <= 65536 else b""

        def _proxy(self, method: str):
            data = self._body() if method in ("POST", "PUT", "DELETE") else None
            hdr = {"Content-Type": "application/json"}
            if self.headers.get("Authorization"):
                hdr["Authorization"] = self.headers["Authorization"]
            req = urllib.request.Request(ENGINE + self.path, data=data, method=method, headers=hdr)
            try:
                with urllib.request.urlopen(req, timeout=6) as r:
                    return self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
            except urllib.error.HTTPError as e:
                return self._send(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
            except OSError as e:
                return self._send(503, {"error": f"engine not reachable ({e})", "engine_down": True})

        def _route(self, method: str):
            u = urlparse(self.path)
            p = u.path
            if p.startswith("/api/") or p == "/metrics" or p == "/classic":
                return self._proxy(method)
            if method == "GET" and p in ("/", "/index.html"):
                with open(CONSOLE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and p.startswith("/fonts/"):
                fp = os.path.join(os.path.dirname(CONSOLE), "fonts", os.path.basename(p))
                if fp.endswith(".woff") and os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "font/woff")
                return self._send(404, {"error": "not found"})
            if not p.startswith("/lab/"):
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self._body() or b"{}") if method == "POST" else {}
            except ValueError:
                return self._send(400, {"error": "bad JSON"})
            try:
                if p == "/lab/status" and method == "GET":
                    st = TB.status()
                    return self._send(200, {
                        "lab": True, "testbed_up": bool(st.get("namespaces")),
                        "isp": {"down_mbit": st.get("down_mbit"), "up_mbit": st.get("up_mbit"),
                                "nominal_down": st.get("nominal_down", st.get("down_mbit")),
                                "nominal_up": st.get("nominal_up", st.get("up_mbit")),
                                "buffer_ms": st.get("isp_buffer_ms"), "delay": st.get("delay_method")},
                        "engine": {"running": lab.engine_running(), "classifier": lab.classifier,
                                   "python": lab.engine_python,
                                   "watchdog": lab.watchdog is not None and lab.watchdog.poll() is None},
                        "traffic": lab.traffic_status(),
                        "demo": lab.demo_status(),
                        "presets": {k: v["name"] for k, v in PRESETS.items()},
                        "job": Lab._public(lab.job) if lab.job else None})
                if p == "/lab/condition" and method == "POST":
                    dn, up = float(body.get("down", 100)), float(body.get("up", 20))
                    if not (0.5 <= dn <= 1000 and 0.2 <= up <= 1000):
                        raise ValueError("rates out of range")
                    TB.set_isp_rate(dn, up)
                    return self._send(200, {"down_mbit": dn, "up_mbit": up})
                if p == "/lab/traffic" and method == "POST":
                    if body.get("action", "start") == "stop":
                        lab.traffic_stop(body.get("preset"))
                        return self._send(200, {"ok": True})
                    return self._send(200, lab.traffic_start(body.get("preset", ""),
                                                             float(body.get("duration_s", 3600))))
                if p == "/lab/demo" and method == "POST":
                    if body.get("action", "start") == "stop":
                        lab.demo_stop()
                        return self._send(200, {"ok": True})
                    return self._send(200, lab.demo_start())
                if p == "/lab/reset" and method == "POST":
                    lab.demo_stop()
                    lab.traffic_stop()
                    st = TB.status()
                    TB.set_isp_rate(st.get("nominal_down", 100), st.get("nominal_up", 20))
                    return self._send(200, {"ok": True})
                if p == "/lab/engine" and method == "POST":
                    act = body.get("action", "restart")
                    if act == "kill":
                        lab.kill_engine()
                    elif act == "stop":
                        lab.stop_engine()
                    else:
                        lab.start_engine(body.get("classifier"))
                    return self._send(200, {"ok": True, "action": act})
                if p == "/lab/experiments" and method == "GET":
                    summ = None
                    sp = os.path.join(ROOT, "results", "summary.json")
                    if os.path.exists(sp):
                        with open(sp) as f:
                            summ = json.load(f)
                    figs = sorted(f"results/{x}" for x in os.listdir(os.path.join(ROOT, "results"))
                                  if x.endswith(".png")) if os.path.isdir(os.path.join(ROOT, "results")) else []
                    return self._send(200, {"catalogue": catalogue(), "summary": summ, "figures": figs,
                                            "jobs": [Lab._public(j) for j in lab.jobs]})
                if p == "/lab/experiments/run" and method == "POST":
                    return self._send(200, lab.run_experiment(body.get("scenario", ""), body.get("seeds", 1),
                                                              body.get("arms")))
                if p.startswith("/lab/experiments/") and method == "GET":
                    d = lab.job_detail(p.rsplit("/", 1)[-1])
                    return self._send(200 if d else 404, d or {"error": "no such job"})
                if p.startswith("/lab/figures/") and method == "GET":
                    rel = p[len("/lab/figures/"):]
                    base = ROOT if rel.startswith("results/") else LAB_RESULTS
                    rel = rel[len("lab/"):] if rel.startswith("lab/") else rel
                    full = os.path.realpath(os.path.join(base, rel))
                    if not full.startswith(os.path.realpath(base)) or not full.endswith(".png"):
                        return self._send(403, {"error": "forbidden"})
                    with open(full, "rb") as f:
                        return self._send(200, f.read(), "image/png")
            except (ValueError, KeyError, FileNotFoundError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:8090")
    ap.add_argument("--no-engine", action="store_true", help="do not start the engine (it is already running)")
    ap.add_argument("--logdir", default=os.path.join(ROOT, "results-lab", "logs"))
    ap.add_argument("--agent", action="store_true", help="also run the router agent (needs the controller: make controller)")
    ap.add_argument("--engine-python", default=PY,
                    help="Python interpreter for the engine and watchdog (e.g. an aarch64 musl build under qemu)")
    ap.add_argument("--presets", default="", metavar="NAME,NAME",
                    help="traffic presets to start once the engine is up, e.g. call,browse (a lived-in home for demos)")
    ap.add_argument("--engine-set", action="append", default=[], metavar="KEY=VALUE",
                    help="extra --set for the engine, e.g. capture.helper_path=router/iq-capture.aarch64")
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("labd must run as root (it drives the network namespaces)", file=sys.stderr)
        return 2
    if not TB.status().get("namespaces"):
        print("testbed is not up; starting it (100/20 Mbit/s, bloated modem)", flush=True)
        TB.up(100, 20, 250, 20)
    lab = Lab(a.logdir, a.engine_python, a.engine_set)
    lab.with_agent = a.agent
    lab.ensure_server()
    lab.start_speedsrv()
    if not a.no_engine:
        lab.start_engine()
    if a.presets:
        def _presets():
            time.sleep(8)                      # let the engine measure its idle baseline first
            for name in [x for x in a.presets.split(",") if x]:
                try:
                    lab.traffic_start(name)
                except Exception as e:         # a missing generator must not take the console down
                    print(f"preset {name}: {e}", flush=True)
        threading.Thread(target=_presets, daemon=True).start()
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(lab))
    srv.daemon_threads = True
    print(f"IntentQoS lab console on http://{host}:{port}/", flush=True)

    def _sig(_s, _f):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sig)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        lab.traffic_stop()
        lab.stop_engine()
        lab._kill(lab.agent)
        lab._kill(lab.speedsrv)
        lab._kill(lab.server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
