"""Process orchestration helpers for experiments (namespaces, timed flows, engine, cleanup)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from . import testbed as TB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


class Run:
    def __init__(self, outdir: str):
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.procs: list[subprocess.Popen] = []
        self.timers: list[threading.Timer] = []
        self.t0 = None
        self.engine = None
        self.watchdog = None
        self.access: dict = {}
        self.marks: list[dict] = []
        self.lock = threading.Lock()

    # -------------------------------------------------------------- processes
    def spawn(self, ns: str, args: list[str], log: str | None = None) -> subprocess.Popen:
        out = open(os.path.join(self.outdir, log), "a") if log else subprocess.DEVNULL
        p = subprocess.Popen(["ip", "netns", "exec", ns] + args, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
        with self.lock:
            self.procs.append(p)
        return p

    def server(self) -> None:
        self.spawn(TB.NS_SRV, [PY, "-m", "testbed.traffic", "server", "--out", self.outdir], "server.log")
        time.sleep(0.6)

    def start_clock(self) -> None:
        self.t0 = time.time()
        self.mark("t0")

    def mark(self, what: str, **kw) -> None:
        with self.lock:
            self.marks.append({"t": time.time(), "what": what, **kw})

    def at(self, t: float, fn, *args) -> None:
        delay = max(0.0, self.t0 + t - time.time())
        tm = threading.Timer(delay, fn, args)
        tm.daemon = True
        tm.start()
        self.timers.append(tm)

    def flow(self, t: float, kind: str, name: str, device: str, duration: float, **opts) -> None:
        args = [PY, "-m", "testbed.traffic", kind, "--name", name, "--src", TB.device_ip(device),
                "--dst", TB.SERVER4, "--duration", f"{duration:.2f}", "--out", self.outdir]
        for k, v in opts.items():
            args += [f"--{k.replace('_', '-')}", str(v)]

        def go():
            self.mark("flow", name=name, kind=kind, device=device)
            self.spawn(TB.NS_CL, args, f"{name}.log")
        self.at(t, go)

    # -------------------------------------------------------------- engine
    def start_engine(self, config: str, sets: list[str], ts_name: str = "timeseries.jsonl") -> None:
        data = os.path.join(self.outdir, "engine-data")
        os.makedirs(data, exist_ok=True)
        args = [PY, "-m", "intentqos.daemon", "--config", config, "--timeseries", os.path.join(self.outdir, ts_name),
                "--set", f"data.dir={data}", "--set", f"intents.store_path={data}/intents.json",
                "--set", f"classifier.overrides_path={data}/overrides.json",
                "--set", f"classifier.corrections_path={data}/corrections.jsonl"]
        for s in sets:
            args += ["--set", s]
        self.engine = self.spawn(TB.NS_GW, args, "engine.log")
        self._cpu_budget(self.engine.pid)
        self.mark("engine_start")

    @staticmethod
    def _cpu_budget(pid: int) -> None:
        """Field-calibrated profile: give the engine only a router-class CPU budget (cgroup CFS quota)."""
        from . import profile as PR
        P = PR.active()
        if not P or not P.get("engine_cpu"):
            return
        cg = "/sys/fs/cgroup/cpu/iq_engine"
        try:
            os.makedirs(cg, exist_ok=True)
            with open(os.path.join(cg, "cpu.cfs_period_us"), "w") as f:
                f.write("100000")
            with open(os.path.join(cg, "cpu.cfs_quota_us"), "w") as f:
                f.write(str(int(P["engine_cpu"]["quota"] * 100000)))
            with open(os.path.join(cg, "cgroup.procs"), "w") as f:
                f.write(str(pid))
        except OSError as e:
            print("cpu budget not applied:", e)

    def start_watchdog(self, config: str) -> None:
        data = os.path.join(self.outdir, "engine-data")
        os.makedirs(data, exist_ok=True)
        # the watchdog writes into the same events file as the engine
        self.watchdog = self.spawn(TB.NS_GW, [PY, "-m", "intentqos.watchdog", "--config", config,
                                              "--interval", "0.25", "--set", f"data.dir={data}"], "watchdog.log")

    def kill_engine(self, sig=signal.SIGKILL) -> None:
        if self.engine and self.engine.poll() is None:
            self.mark("engine_kill", sig=int(sig))
            os.killpg(self.engine.pid, sig)

    def api(self, method: str, path: str, body: dict | None = None, timeout: float = 3.0) -> dict:
        req = urllib.request.Request(f"http://{TB.GW_MGMT_IP}:8080{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}")
        except OSError as e:
            return {"error": str(e)}

    def wait_api(self, timeout: float = 15.0) -> bool:
        t = time.time() + timeout
        while time.time() < t:
            r = self.api("GET", "/api/healthz", timeout=1.0)
            if r.get("ok"):
                return True
            time.sleep(0.3)
        return False

    # -------------------------------------------------------------- cleanup
    def stop(self, grace: float = 2.5) -> None:
        for tm in self.timers:
            tm.cancel()
        with self.lock:
            procs = list(self.procs)
        # engine gets SIGTERM first so it can write its exit state
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        t = time.time() + grace
        for p in procs:
            try:
                p.wait(timeout=max(0.1, t - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        with open(os.path.join(self.outdir, "marks.json"), "w") as f:
            json.dump({"t0": self.t0, "marks": self.marks}, f, indent=1)
