#!/usr/bin/env python3
"""Generate a labelled, header-only flow dataset in the namespace testbed.

Each session randomises the access-link rate, whether the gateway is shaping,
and a mix of flows (VoIP, gaming, video calls, adaptive streaming, browsing,
bulk downloads/uploads) with randomised parameters. The recorder (ml/record.py)
captures feature snapshots on the gateway; labels are joined afterwards from
the generators' own 5-tuples. No payload is captured; labels come from the
fact that WE generated the traffic (consented ground truth).

    sudo python3 ml/gen_dataset.py --sessions 18 --seconds 50 --out ml/data/dataset.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from testbed import testbed as TB  # noqa: E402
from testbed.runner import PY, Run  # noqa: E402

KIND_LABEL = {"voip": "voip", "gaming": "gaming", "video": "video", "abr": "streaming", "web": "web", "bulk": "bulk"}
LADDERS = ["0.8,1.5,3,6", "1.5,3,6,10", "1.5,3,6,10,16", "0.5,1,2,4,8"]


def random_flow(rnd: random.Random, kind: str, seconds: float):
    start = rnd.uniform(0, seconds * 0.4)
    dur = rnd.uniform(seconds * 0.35, seconds - start)
    o = {}
    if kind == "voip":
        o = {"pps": rnd.choice([33, 50, 50]), "size": rnd.randint(80, 230)}
    elif kind == "gaming":
        o = {"pps_up": rnd.randint(20, 128), "size_up": rnd.randint(50, 180),
             "pps_down": rnd.randint(15, 64), "size_down": rnd.randint(80, 600)}
    elif kind == "video":
        o = {"fps": rnd.choice([15, 24, 30, 30]), "kbps": rnd.randint(400, 3500), "down_kbps": rnd.randint(500, 4500)}
    elif kind == "bulk":
        o = {"conns": rnd.randint(1, 8), "direction": "up" if rnd.random() < 0.3 else "down"}
    elif kind == "abr":
        o = {"ladder": rnd.choice(LADDERS), "chunk": rnd.choice([2, 4, 6]), "buffer_target": rnd.randint(8, 30),
             "seed": rnd.randint(0, 10_000)}
        dur = max(dur, 25.0)
        start = min(start, seconds - dur)
    elif kind == "web":
        o = {"seed": rnd.randint(0, 10_000)}
    return start, dur, o


def session(idx: int, seconds: float, outdir: str, rnd: random.Random, snaps: str) -> None:
    down, up = rnd.choice([(100, 20), (50, 10), (25, 5), (12, 3), (80, 15)])
    TB.set_isp_rate(down, up)
    TB.reset_gateway()
    run = Run(outdir)
    run.server()
    shaping = rnd.random() < 0.5
    if shaping:
        run.start_engine(os.path.join(ROOT, "config/testbed.toml"),
                         ["classifier.mode=rules", "api.listen=0.0.0.0:8080",
                          f"link.down_mbit={down}", f"link.up_mbit={up}"])
        time.sleep(2.0)
    rec = subprocess.Popen(["ip", "netns", "exec", TB.NS_GW, PY, os.path.join(ROOT, "ml/record.py"),
                            "--iface", TB.GW_LAN, "--lan", "192.168.50.0/24", "--out", snaps,
                            "--session", f"s{idx:02d}"], cwd=ROOT, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    run.start_clock()
    devices = list(TB.DEVICES)
    rnd.shuffle(devices)
    kinds = ["voip", "gaming", "video", "abr", "web", "bulk"]
    n = rnd.randint(4, 8)
    plan = []
    for i in range(n):
        kind = kinds[(idx + i) % len(kinds)] if i < len(kinds) else rnd.choice(kinds)
        dev = devices[i % len(devices)]
        st, du, o = random_flow(rnd, kind, seconds)
        name = f"s{idx:02d}_{i}_{kind}"
        run.flow(st, kind, name, dev, du, **o)
        plan.append((name, kind))
    time.sleep(seconds + 4)
    rec.terminate()
    rec.wait(timeout=5)
    run.stop()
    TB.reset_gateway()
    print(f"session {idx}: isp {down}/{up} shaping={shaping} flows={[k for _, k in plan]}", flush=True)


def join(outdir: str, snaps: str, out: str) -> dict:
    labels = {}
    for p in glob.glob(os.path.join(outdir, "**", "*.json"), recursive=True):
        try:
            with open(p) as f:
                d = json.load(f)
        except (ValueError, OSError):
            continue
        if not isinstance(d, dict) or "kind" not in d or "src" not in d:
            continue
        lab = KIND_LABEL.get(d["kind"])
        proto = 17 if d.get("proto") == "udp" else 6
        ports = d.get("sports") or [d.get("sport")]
        for sp in ports:
            if sp:
                labels[(d["src"], d["dst"], proto, int(sp), int(d["dport"]))] = lab
    counts: dict[str, int] = {}
    n = 0
    with open(snaps) as fi, open(out, "w") as fo:
        for line in fi:
            r = json.loads(line)
            lab = labels.get(tuple(r["key"]))
            if lab is None:
                continue
            r["label"] = lab
            fo.write(json.dumps(r) + "\n")
            counts[lab] = counts.get(lab, 0) + 1
            n += 1
    return {"rows": n, "per_label": counts, "labelled_flows": len(labels)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", type=int, default=18)
    ap.add_argument("--seconds", type=float, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(ROOT, "ml/data/dataset.jsonl"))
    ap.add_argument("--work", default="/tmp/intentqos-dataset")
    ap.add_argument("--keep-testbed", action="store_true")
    a = ap.parse_args(argv)
    rnd = random.Random(a.seed)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    subprocess.run(["rm", "-rf", a.work])
    os.makedirs(a.work)
    snaps = os.path.join(a.work, "snapshots.jsonl")
    TB.up(100, 20, 250, 20)
    try:
        for i in range(a.sessions):
            session(i, a.seconds, os.path.join(a.work, f"s{i:02d}"), rnd, snaps)
    finally:
        if not a.keep_testbed:
            TB.down()
    summary = join(a.work, snaps, a.out)
    summary["sessions"] = a.sessions
    with open(a.out.replace(".jsonl", ".summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
