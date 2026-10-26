#!/usr/bin/env python3
"""Latency-under-load test for the hardware trial (standard library only; Linux, macOS or Windows).

Run it on a laptop that sits BEHIND the test gateway (Raspberry Pi or Linux box running IntentQoS), once with
the gateway in plain-router mode (--label noqos) and once with IntentQoS running (--label intentqos):

    python3 latency_test.py --label noqos
    python3 latency_test.py --label intentqos

Each run pings a nearby anycast address while the line is idle, then while a multi-stream download fills the
line, then while an upload fills it. It writes results/<label>-<time>.json with per-phase ping percentiles,
loss and achieved throughput. hwtest/summarize.py turns two runs into the slide's hardware.json.
"""
import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

DOWN_URL = "https://speed.cloudflare.com/__down?bytes=200000000"
UP_URL = "https://speed.cloudflare.com/__up"
TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.I)


def ping_stream(target, stop, samples, interval):
    """Run the system ping until stop is set; append (wall time, rtt ms or None for a loss)."""
    win = platform.system() == "Windows"
    if win:
        cmd = ["ping", "-t", target]                       # Windows: one per second
    else:
        cmd = ["ping", "-i", str(interval), target]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    try:
        for line in p.stdout:
            if stop.is_set():
                break
            m = TIME_RE.search(line)
            if m:
                samples.append((time.time(), float(m.group(1))))
            elif re.search(r"timed out|unreachable|no answer", line, re.I):
                samples.append((time.time(), None))
    finally:
        p.terminate()


def download_worker(stop, counter):
    while not stop.is_set():
        try:
            with urllib.request.urlopen(DOWN_URL, timeout=30) as r:
                while not stop.is_set():
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    counter[0] += len(chunk)
        except Exception as e:                                      # keep loading; note the error once
            counter[1] = str(e)
            time.sleep(1)


def upload_worker(stop, counter):
    block = os.urandom(1 << 20)

    def body():
        while not stop.is_set():
            counter[0] += len(block)
            yield block
    while not stop.is_set():
        try:
            req = urllib.request.Request(UP_URL, data=body(), method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            counter[1] = str(e)
            time.sleep(1)


def stats(values):
    ok = sorted(v for v in values if v is not None)
    lost = sum(1 for v in values if v is None)
    if not ok:
        return {"n": len(values), "loss_pct": 100.0}

    def pct(q):
        return ok[min(len(ok) - 1, int(round(q / 100 * (len(ok) - 1))))]
    return {"n": len(values), "p50_ms": round(pct(50), 1), "p95_ms": round(pct(95), 1), "p99_ms": round(pct(99), 1),
            "max_ms": round(ok[-1], 1), "mean_ms": round(statistics.mean(ok), 1),
            "loss_pct": round(100 * lost / len(values), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=["noqos", "intentqos"])
    ap.add_argument("--target", default="1.1.1.1")
    ap.add_argument("--idle", type=int, default=30, help="seconds of idle line")
    ap.add_argument("--load", type=int, default=60, help="seconds of each load phase")
    ap.add_argument("--streams", type=int, default=6)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--note", default="", help="free text: line, plan, Wi-Fi or wired, gateway model")
    a = ap.parse_args()

    if not shutil.which("ping"):
        raise SystemExit("ping was not found on this machine; install it (Linux: sudo apt install iputils-ping)")
    samples, stop_ping = [], threading.Event()
    interval = 0.2
    pt = threading.Thread(target=ping_stream, args=(a.target, stop_ping, samples, interval), daemon=True)
    pt.start()
    phases = []

    def phase(name, seconds, worker=None):
        t0 = time.time()
        stop, counter, threads = threading.Event(), [0, None], []
        if worker:
            for _ in range(a.streams):
                th = threading.Thread(target=worker, args=(stop, counter), daemon=True)
                th.start()
                threads.append(th)
            time.sleep(3)                                            # let TCP ramp up before scoring
            t0, start_bytes = time.time(), counter[0]
        print(f"[{name}] {seconds} s ...", flush=True)
        time.sleep(seconds)
        t1 = time.time()
        stop.set()
        mbps = round((counter[0] - (start_bytes if worker else 0)) * 8 / (t1 - t0) / 1e6, 1) if worker else 0.0
        phases.append({"phase": name, "t0": t0, "t1": t1, "mbps": mbps, "error": counter[1]})
        time.sleep(3)                                                # drain before the next phase

    phase("idle", a.idle)
    phase("download", a.load, download_worker)
    if not a.no_upload:
        phase("upload", a.load, upload_worker)
    stop_ping.set()

    out = {"label": a.label, "target": a.target, "note": a.note, "host": platform.platform(),
           "started": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": []}
    for ph in phases:
        vals = [v for t, v in samples if ph["t0"] <= t <= ph["t1"]]
        out["phases"].append({"phase": ph["phase"], "throughput_mbps": ph["mbps"], "ping": stats(vals),
                              "load_error": ph["error"]})
    os.makedirs("results", exist_ok=True)
    path = os.path.join("results", f"{a.label}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(out, open(path, "w"), indent=1)
    for ph in out["phases"]:
        print(ph["phase"], ph["throughput_mbps"], "Mbps", ph["ping"])
    print("saved", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
