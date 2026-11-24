#!/usr/bin/env python3
"""An evening in the emulated home, with ground truth: every household device runs its real-looking traffic at once,
first with IntentQoS bypassed (the router's own bounded test window, as the Test Kit uses) and then with it on.

    household.py OUTDIR [--seconds 120] [--arms off,on] [--tag evening]

Devices and flows (servers in the emulated internet; one-way delay from kernel timestamps, one clock):
  laptop  (5 GHz)   video call: video 30 fps ~1.8/2.2 Mbit/s + G.711 audio 50 pps each way
  pc      (wired)   ISO download (4 TCP connections) and cloud backup upload (2 connections)
  tv1     (5 GHz)   streaming (adaptive bitrate 1.5/3/6/10 Mbit/s)
  tv2/tv3 (2.4 GHz) streaming, same ladder
  console (5 GHz)   online game (60 pps up, 30 pps down)
  phone   (5 GHz)   web browsing bursts
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from testbed import analysis as A          # noqa: E402
from testbed.traffic import flow_hash      # noqa: E402

WORK = os.environ.get("SYSSIM_WORK", "/home/claude/syssim")
DST = "104.18.20.10"
NS = {"laptop": "w-lap", "pc": "w-pc", "tv1": "w-tv1", "tv2": "w-tv2", "tv3": "w-tv3", "console": "w-con",
      "phone": "w-phone"}


def ip_of(ns: str) -> str:
    out = subprocess.run(f"ip -n {ns} -4 -o addr show eth0", shell=True, capture_output=True, text=True).stdout
    return out.split()[3].split("/")[0]


def router_api(path: str, body: dict | None = None) -> str:
    key = open(os.path.join(WORK, "router.key")).read().strip()
    data = f"-H 'Content-Type: application/json' -d '{json.dumps(body)}'" if body is not None else ""
    return subprocess.run(f"ip netns exec w-ops curl -s -m 10 -H 'Authorization: Bearer {key}' {data} "
                          f"http://192.168.8.1:8080{path}", shell=True, capture_output=True, text=True).stdout


def mark(timeline: str, **k) -> None:
    with open(timeline, "a") as f:
        f.write(json.dumps({"t": time.time(), **k}) + "\n")


def flows(arm: str, dur: float) -> list[tuple[float, str, str, str, list[str]]]:
    p = f"{arm}-"
    return [
        (0.0, "pc", "bulk", p + "iso_download", ["--conns", "4", "--direction", "down"]),
        (0.0, "pc", "bulk", p + "cloud_backup", ["--conns", "2", "--direction", "up"]),
        (0.0, "tv1", "abr", p + "tv1", ["--ladder", "1.5,3,6,10"]),
        (1.0, "tv2", "abr", p + "tv2", ["--ladder", "1.5,3,6,10"]),
        (2.0, "tv3", "abr", p + "tv3", ["--ladder", "1.5,3,6,10"]),
        (5.0, "laptop", "video", p + "call_video", []),
        (5.0, "laptop", "voip", p + "call_audio", []),
        (5.0, "console", "gaming", p + "game", []),
        (5.0, "phone", "web", p + "phone_web", []),
    ]


def run_arm(arm: str, outdir: str, dur: float, timeline: str, seed: int) -> dict:
    os.makedirs(outdir, exist_ok=True)
    if arm == "off":
        r = router_api("/api/bypass", {"on": True, "minutes": (dur + 60) / 60, "by": "systemsim household test"})
    else:
        r = router_api("/api/bypass", {"on": False, "by": "systemsim household test"})
    mark(timeline, event=f"household_{arm}_bypass", reply=r[:200])
    time.sleep(10)
    t0 = time.time()
    mark(timeline, event=f"household_{arm}_start", seconds=dur)
    procs = []
    for delay, dev, kind, name, opts in flows(arm, dur):
        ns = NS[dev]
        d = dur - delay
        cmd = (f"cd {REPO} && exec ip netns exec {ns} {sys.executable} -m testbed.traffic {kind} --name {name} "
               f"--src {ip_of(ns)} --dst {DST} --duration {d:.1f} --out {outdir} --seed {seed} " + " ".join(opts))
        wait = t0 + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        procs.append(subprocess.Popen(cmd, shell=True, stdout=open(os.path.join(outdir, name + ".log"), "w"),
                                      stderr=subprocess.STDOUT))
    for p in procs:
        p.wait()
    mark(timeline, event=f"household_{arm}_end")
    # uplink receive records are kept by the server, which writes them once a flow has gone quiet
    time.sleep(15)
    collect_uplink(arm, outdir, dur)
    return summarize(arm, outdir, t0, dur)


def collect_uplink(arm: str, outdir: str, dur: float) -> None:
    for _d, _dev, kind, name, _o in flows(arm, dur):
        srcs = []
        if kind in ("voip", "video", "gaming"):
            try:
                fh = json.load(open(os.path.join(outdir, name + ".json")))["flow_hash"]
            except (OSError, ValueError, KeyError):
                fh = flow_hash(name)
            srcs = [os.path.join(WORK, "servers", f"up_{fh}.json")]
        elif kind == "bulk":
            srcs = glob.glob(os.path.join(WORK, "servers", f"upbulk_{name}_*.json"))
        for src in srcs:
            if os.path.exists(src):
                shutil.copy(src, outdir)


def summarize(arm: str, outdir: str, t0: float, dur: float) -> dict:
    a, b = t0 + 20, t0 + dur - 5          # steady state: after start-up, before the flows end
    p = f"{arm}-"
    out: dict = {"arm": arm, "window_s": [20, dur - 5]}
    for name, mos in (("call_audio", True), ("call_video", False), ("game", False)):
        rec = A.realtime_records(outdir, p + name)
        if rec is None:
            continue
        for d in ("down", "up"):
            if d in rec:
                m = A.realtime_metrics(rec[d], a, b, mos=mos)
                if m:
                    out[f"{name}_{d}"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}
    for name in ("iso_download", "cloud_backup"):
        m = A.bulk_metrics(A.bulk_series(outdir, p + name), a, b)
        if m:
            out[name] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}
    for tv in ("tv1", "tv2", "tv3"):
        m = A.abr_metrics(outdir, p + tv, 20.0)
        if m:
            out[tv] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}
    m = A.web_metrics(outdir, p + "phone_web")
    if m:
        out["phone_web"] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in m.items()}
    json.dump(out, open(os.path.join(outdir, "summary.json"), "w"), indent=1)
    return out


def resummarize(outdir: str, dur: float) -> dict:
    """Re-collect uplink records and recompute both arms from the run's timeline (after the fact)."""
    tl = [json.loads(l) for l in open(os.path.join(outdir, "..", "timeline.jsonl"))]
    res = {}
    for arm in ("off", "on"):
        t0 = [r["t"] for r in tl if r.get("event") == f"household_{arm}_start"][-1]
        collect_uplink(arm, os.path.join(outdir, arm), dur)
        res[arm] = summarize(arm, os.path.join(outdir, arm), t0, dur)
    json.dump(res, open(os.path.join(outdir, "household.json"), "w"), indent=1)
    return res


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "resummarize":
        print(json.dumps(resummarize(sys.argv[2], 120))[:4000])
        raise SystemExit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--arms", default="off,on")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    timeline = os.path.join(os.path.dirname(a.outdir.rstrip("/")), "..", "timeline.jsonl")
    timeline = os.path.join(a.outdir, "timeline.jsonl") if not os.path.isdir(os.path.dirname(timeline)) else timeline
    res = {}
    for arm in a.arms.split(","):
        res[arm] = run_arm(arm, os.path.join(a.outdir, arm), a.seconds, os.path.join(a.outdir, "..", "timeline.jsonl"),
                           a.seed)
        print(json.dumps(res[arm])[:3000], flush=True)
    router_api("/api/bypass", {"on": False, "by": "systemsim household test"})
    json.dump(res, open(os.path.join(a.outdir, "household.json"), "w"), indent=1)
