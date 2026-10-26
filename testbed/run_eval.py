#!/usr/bin/env python3
"""Automated baseline-vs-optimised experiments.

    sudo python3 -m testbed.run_eval --seeds 10                 # all scenarios, all arms
    sudo python3 -m testbed.run_eval --seeds 3 --scenarios s2_wan_drop --arms noqos,adaptive_ml

For every (seed, scenario, arm) the harness resets the testbed and gateway,
starts the arm, injects the scenario's condition on a seeded timeline, collects
evidence and writes results/runs/<scenario>/<arm>/seed<NN>/metrics.json.
Arm order is shuffled per seed to avoid order effects. Then run
`python3 -m testbed.report` to aggregate statistics, plots and the HTML report.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import shutil
import sys
import time
import traceback
import zlib

from . import profile as PR
from . import testbed as TB
from .runner import Run
from .scenarios import SCENARIOS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _slim(outdir: str) -> None:
    """Delete bulky per-packet generator records after metrics are computed."""
    for p in glob.glob(os.path.join(outdir, "*.json")):
        if os.path.basename(p) in ("metrics.json", "marks.json"):
            continue
        os.remove(p)
    for p in glob.glob(os.path.join(outdir, "*.log")):
        if os.path.basename(p) not in ("engine.log", "watchdog.log", "server.log"):
            os.remove(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--first-seed", type=int, default=1)
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--arms", default=None, help="comma list; default = each scenario's arms")
    ap.add_argument("--out", default=os.path.join(ROOT, f"results-{PR.active()['name']}" if PR.active() else "results"))
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip runs that already have metrics.json")
    ap.add_argument("--reuse-testbed", action="store_true",
                    help="use the testbed that is already up and leave it up afterwards (the console's lab uses this)")
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("run as root", file=sys.stderr)
        return 2
    scen = [s for s in a.scenarios.split(",") if s]
    st = TB.status() if a.reuse_testbed and TB.status().get("namespaces") else TB.up(100, 20, 250, 20)
    env = {"testbed": st, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "kernel": os.uname().release,
           "python": sys.version.split()[0], "profile": PR.active()}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "environment.json"), "w") as f:
        json.dump(env, f, indent=1)
    t_start = time.time()
    try:
        for seed in range(a.first_seed, a.first_seed + a.seeds):
            for sname in scen:
                fn, arms = SCENARIOS[sname]
                if a.arms:
                    arms = [x for x in a.arms.split(",") if x in arms]
                order = list(arms)
                random.Random(seed * 1000 + zlib.crc32(sname.encode()) % 997).shuffle(order)
                for arm in order:
                    outdir = os.path.join(a.out, "runs", sname, arm, f"seed{seed:02d}")
                    if a.resume and os.path.exists(os.path.join(outdir, "metrics.json")):
                        continue
                    shutil.rmtree(outdir, ignore_errors=True)
                    run = Run(outdir)
                    rnd = random.Random(seed * 7919 + zlib.crc32(sname.encode()) % 1009)   # same params for every arm
                    t = time.time()
                    try:
                        m = fn(run, arm, rnd)
                        m.update({"scenario": sname, "arm": arm, "seed": seed, "wall_s": round(time.time() - t, 1)})
                    except Exception as e:  # noqa: BLE001
                        traceback.print_exc()
                        m = {"scenario": sname, "arm": arm, "seed": seed, "error": f"{type(e).__name__}: {e}"}
                    finally:
                        run.stop()
                        TB.reset_gateway()
                        if PR.active():
                            TB.set_isp_rate(PR.active()["plan"]["down_mbit"], PR.active()["plan"]["up_mbit"])
                        else:
                            TB.set_isp_rate(100, 20)
                    with open(os.path.join(outdir, "metrics.json"), "w") as f:
                        json.dump(m, f, indent=1, default=float)
                    if not a.keep_raw:
                        _slim(outdir)
                    el = (time.time() - t_start) / 60
                    print(f"[{el:6.1f} min] seed {seed} {sname:22s} {arm:20s} "
                          f"{'ERROR ' + m['error'] if 'error' in m else 'ok'}", flush=True)
    finally:
        if not a.reuse_testbed:
            TB.down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
