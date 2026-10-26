#!/usr/bin/env python3
"""Classifier accuracy on the traffic of experiment runs (needs runs made with --keep-raw).

    python3 -m testbed.classeval --results results-india_ftth_evening

The ground truth is what the traffic generator was asked to produce (voip, video, gaming, abr -> streaming,
web, bulk); the prediction is the engine's per-tick label log (timeseries.jsonl). Every (flow, tick) counts
once, from the flow's first labelled tick to its end. Two figures per arm:

* scheduling-class accuracy of the COMMITTED label (the one that drives the shaper), the figure the
  classifier report gives for held-out lab sessions;
* the share of ticks where the model abstained, split into "kept its class" (ml-hold) and "rules" fallback.

Writes <results>/CLASSIFIER_FIELD.md and classifier_field.json. EMULATION: the traffic is generated, but the
network conditions are those of the run's profile.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os

from intentqos import LABEL_TO_TIN

KIND_TO_LABEL = {"voip": "voip", "video": "video", "gaming": "gaming", "abr": "streaming", "web": "web",
                 "bulk": "bulk"}
ARMS = ("adaptive_ml", "adaptive_ml_bulk", "adaptive_ml_intent", "adaptive_rules")


def truth_map(run: str) -> dict:
    """(proto, src, sport) -> true label, from the generator's flow descriptors."""
    m = {}
    for f in glob.glob(os.path.join(run, "*.json")):
        if os.path.basename(f) in ("metrics.json", "marks.json"):
            continue
        try:
            d = json.load(open(f))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict) or "kind" not in d or "src" not in d or "proto" not in d:
            continue
        lab = KIND_TO_LABEL.get(d["kind"])
        if lab is None:
            continue
        ports = d.get("sports") or ([d["sport"]] if "sport" in d else [])
        for p in ports:
            m[(d["proto"], d["src"], int(p))] = lab
    return m


def parse_fid(fid: str):
    """'tcp:192.168.50.12:60345-203.0.113.10:9001' -> ('tcp', '192.168.50.12', 60345)"""
    try:
        proto, rest = fid.split(":", 1)
        a = rest.split("-", 1)[0]
        ip, port = a.rsplit(":", 1)
        return proto, ip, int(port)
    except ValueError:
        return None


def evaluate(results: str, scenarios: tuple | None = None) -> dict:
    out = {}
    for arm in ARMS:
        tot = ok_cls = ok_lab = 0
        src = collections.Counter()
        per_class = collections.defaultdict(lambda: [0, 0])
        runs = 0
        for run in sorted(glob.glob(os.path.join(results, "runs", "*", arm, "seed*"))):
            if scenarios and run.split(os.sep)[-3] not in scenarios:
                continue
            ts = os.path.join(run, "timeseries.jsonl")
            tm = truth_map(run)
            if not tm or not os.path.exists(ts):
                continue
            runs += 1
            for line in open(ts):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for fid, v in (row.get("labels") or {}).items():
                    k = parse_fid(fid)
                    if k is None or k not in tm:
                        continue
                    truth = tm[k]
                    committed = v[1] if v[1] else v[0]
                    tot += 1
                    good = LABEL_TO_TIN.get(committed, 3) == LABEL_TO_TIN.get(truth, 3)
                    ok_cls += good
                    ok_lab += committed == truth
                    src[v[2]] += 1
                    per_class[truth][0] += 1
                    per_class[truth][1] += good
        if tot:
            out[arm] = {"runs": runs, "flow_ticks": tot, "class_accuracy": ok_cls / tot, "label_accuracy": ok_lab / tot,
                        "sources": {k: v / tot for k, v in src.items()},
                        "per_class": {c: {"ticks": n, "class_accuracy": g / n} for c, (n, g) in per_class.items()}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results", default="results")
    a = ap.parse_args(argv)
    res = evaluate(a.results)
    # matched comparison: the scenarios where both the ML and the rules arm ran
    both = sorted({p.split(os.sep)[-2] for p in glob.glob(os.path.join(a.results, "runs", "*", "adaptive_rules"))})
    matched = {k: v for k, v in evaluate(a.results, tuple(both)).items() if k in ("adaptive_ml", "adaptive_rules")}
    json.dump({"all": res, "matched": {"scenarios": both, "arms": matched}},
              open(os.path.join(a.results, "classifier_field.json"), "w"), indent=1)
    L = ["# Classifier accuracy on this experiment's traffic", "",
         f"Results: `{a.results}`. Truth = the flow kind the generator produced; prediction = the engine's committed "
         "label each policy tick (the one that drives the shaper). Every flow-tick counts once. EMULATION.", "",
         "| arm | runs | flow-ticks | scheduling-class accuracy | label accuracy | model confident | model unsure, kept its class | rules |",
         "|---|---|---|---|---|---|---|---|"]
    for arm, r in res.items():
        s = r["sources"]
        L.append(f"| {arm} | {r['runs']} | {r['flow_ticks']} | {r['class_accuracy']:.3f} | {r['label_accuracy']:.3f} | "
                 f"{s.get('ml', 0):.3f} | {s.get('ml-hold', 0):.3f} | {s.get('rules', 0) + s.get('rules-fallback', 0):.3f} |")
    L += ["", "Per true class (scheduling-class accuracy):", "", "| arm | " + " | ".join(KIND_TO_LABEL.values()) + " |",
          "|---|" + "---|" * len(KIND_TO_LABEL)]
    for arm, r in res.items():
        pc = r["per_class"]
        L.append(f"| {arm} | " + " | ".join(f"{pc[c]['class_accuracy']:.3f}" if c in pc else "" for c in KIND_TO_LABEL.values())
                 + " |")
    L += ["", f"Matched comparison (same scenarios, {', '.join(both)}; same seeds):", "",
          "| arm | flow-ticks | scheduling-class accuracy |", "|---|---|---|"]
    for arm, r in matched.items():
        L.append(f"| {arm} | {r['flow_ticks']} | {r['class_accuracy']:.3f} |")
    open(os.path.join(a.results, "CLASSIFIER_FIELD.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
