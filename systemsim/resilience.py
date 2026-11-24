#!/usr/bin/env python3
"""Real-world disturbances while the household is in use (EMULATION of the events; the router, engine, kit and
network stacks are real):

  controller_offline  the operator laptop sleeps for 2 minutes: the router must keep shaping on its own and
                      report again when the console is back
  power_cut           the router loses power (hard reset, no clean shutdown): time until the internet, the
                      clock (no battery RTC: NTP), the engine and the console report are back
  wifi_far            the work laptop moves two rooms away mid-call (more Wi-Fi delay and loss): does the
                      engine blame the Wi-Fi rather than the line?

    resilience.py OUTDIR [controller_offline,power_cut,wifi_far]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("SYSSIM_WORK", "/home/claude/syssim")
KEY = open(os.path.join(WORK, "router.key")).read().strip()
DST = "104.18.20.10"


def sh(cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def api(path: str, body: dict | None = None, ns: str = "w-ops", t: float = 5) -> tuple[int, str]:
    data = f"-H 'Content-Type: application/json' -d '{json.dumps(body)}'" if body is not None else ""
    r = sh(f"ip netns exec {ns} curl -s -m {t} -o - -w '\\n%{{http_code}}' -H 'Authorization: Bearer {KEY}' {data} "
           f"http://192.168.8.1:8080{path}", timeout=t + 5)
    body_, _, code = r.stdout.rpartition("\n")
    return int(code or 0), body_


class Log:
    def __init__(self, path: str):
        self.f = open(path, "a")

    def __call__(self, ev: str, **k):
        rec = {"t": time.time(), "event": ev, **k}
        self.f.write(json.dumps(rec) + "\n")
        self.f.flush()
        print(json.dumps(rec)[:400], flush=True)


def pinger(ns: str, dst: str, out: str, stop: threading.Event, every: float = 0.2) -> None:
    """ICMP every 200 ms with timestamps (-D); one line per reply, gaps are lost packets."""
    p = subprocess.Popen(f"ip netns exec {ns} ping -D -i {every} -W 1 {dst}", shell=True,
                         stdout=open(out, "w"), stderr=subprocess.DEVNULL)
    stop.wait()
    p.terminate()


def call(name: str, outdir: str, seconds: float) -> subprocess.Popen:
    lap = sh("ip -n w-lap -4 -o addr show eth0").stdout.split()[3].split("/")[0]
    repo = os.path.dirname(HERE)
    return subprocess.Popen(f"cd {repo} && ip netns exec w-lap {sys.executable} -m testbed.traffic voip --name {name} "
                            f"--src {lap} --dst {DST} --duration {seconds} --out {outdir}", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def controller_offline(outdir: str, log: Log) -> None:
    # the console's Python controller and its Go launcher are the operator laptop's programs
    # exact PIDs (a pattern match would also hit the shell running it): the console and its controller
    pids = []
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                cmd = open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if cmd[0].endswith(b"/intentqos-console") or (b"console/runtime" in cmd[0] and b"controller.server" in cmd):
                pids.append(p)
    log("console_processes", pids=pids)
    c = call("ctl-offline-call", outdir, 200)
    time.sleep(20)
    for p in pids:
        os.kill(int(p), 19)            # SIGSTOP: the laptop's lid is closed
    log("operator_laptop_asleep")
    t0 = time.time()
    states = []
    while time.time() - t0 < 120:
        code, body = api("/api/state")
        try:
            s = json.loads(body)
            states.append({"t": time.time(), "code": code, "backend": s.get("config", {}).get("backend"),
                           "paused": s.get("paused"), "down_rate_kbit": s.get("estimator", {}).get("down", {}).get("rate_kbit")})
        except ValueError:
            states.append({"t": time.time(), "code": code})
        time.sleep(10)
    log("router_while_console_away", samples=states)
    for p in pids:
        os.kill(int(p), 18)            # SIGCONT
    log("operator_laptop_awake")
    c.wait()


def power_cut(outdir: str, log: Log) -> None:
    stop = threading.Event()
    th = threading.Thread(target=pinger, args=("w-lap", DST, os.path.join(outdir, "power-cut-ping.txt"), stop))
    th.start()
    time.sleep(10)
    mon = os.path.join(WORK, "vm", "mt3000.mon")
    sh(f"python3 -c \"import socket,time; s=socket.socket(socket.AF_UNIX); s.connect('{mon}'); time.sleep(0.2); "
       f"s.recv(4096); s.sendall(b'system_reset\\n'); time.sleep(0.3)\"")
    t0 = time.time()
    log("power_cut")
    seen = {}
    while time.time() - t0 < 400 and len(seen) < 4:
        if "lan_ping" not in seen and sh("ip netns exec w-lap ping -c1 -W1 192.168.8.1").returncode == 0:
            seen["lan_ping"] = time.time() - t0
            log("router_answers_on_lan", s=round(seen["lan_ping"], 1))
        if "internet" not in seen and sh(f"ip netns exec w-lap ping -c1 -W1 {DST}").returncode == 0:
            seen["internet"] = time.time() - t0
            log("internet_back", s=round(seen["internet"], 1))
        if "engine" not in seen:
            code, body = api("/api/healthz", t=2)
            if code == 200:
                seen["engine"] = time.time() - t0
                log("engine_api_back", s=round(seen["engine"], 1), body=body[:200])
        if "shaping" not in seen and "engine" in seen:
            code, body = api("/api/state", t=3)
            try:
                s = json.loads(body)
                if s.get("estimator", {}).get("down", {}).get("rate_kbit"):
                    seen["shaping"] = time.time() - t0
                    log("shaping_active", s=round(seen["shaping"], 1), backend=s.get("config", {}).get("backend"))
            except ValueError:
                pass
        time.sleep(1)
    clock = sh("ip netns exec w-ops ssh -i {w}/ssh/id_ed25519 -o UserKnownHostsFile={w}/ssh/known_hosts "
               "-o ConnectTimeout=5 root@192.168.8.1 'date +%s; logread | grep -i -E \"ntpd|time\" | tail -3'"
               .format(w=WORK))
    log("router_clock_after_boot", out=clock.stdout[-600:], host_now=time.time())
    time.sleep(20)
    stop.set()
    th.join()
    log("power_cut_summary", **{k: round(v, 1) for k, v in seen.items()})


def wifi_far(outdir: str, log: Log) -> None:
    c = call("wifi-far-call", outdir, 200)
    time.sleep(40)
    sh(f"{sys.executable} {HERE}/wifi.py degrade laptop 12 0.02")
    log("laptop_moved_far", extra_ms=12, loss=0.02)
    t0 = time.time()
    exp = []
    while time.time() - t0 < 100:
        code, body = api("/api/experience")
        exp.append({"t": time.time(), "code": code, "body": body[:4000]})
        time.sleep(10)
    log("experience_while_far", samples=len(exp))
    with open(os.path.join(outdir, "wifi-far-experience.jsonl"), "w") as f:
        for e in exp:
            f.write(json.dumps(e) + "\n")
    sh(f"{sys.executable} {HERE}/wifi.py restore laptop")
    log("laptop_back_near")
    c.wait()


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    log = Log(os.path.join(out, "resilience.jsonl"))
    for step in (sys.argv[2] if len(sys.argv) > 2 else "controller_offline,power_cut,wifi_far").split(","):
        log(f"{step}_start")
        globals()[step](out, log)
        log(f"{step}_end")
