#!/usr/bin/env python3
"""Export the scikit-learn model bundle to the router format read by intentqos/treemodel.py.

    python3 ml/export_model.py models/flowclf.joblib models/flowclf.iqm [--check ml/data/dataset.jsonl]

Runs on the build machine (needs scikit-learn); the router only needs the .iqm file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys

import joblib
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from intentqos.features import FEATURE_NAMES  # noqa: E402
from intentqos.treemodel import MAGIC, TreeModel  # noqa: E402


def export(src: str, dst: str, release: str | None = None) -> dict:
    b = joblib.load(src)
    if b.get("features") != FEATURE_NAMES:
        raise SystemExit("model was trained on a different feature set")
    classes = [str(c) for c in b["classes"]]
    k = len(classes)
    blobs, members = [], []
    phases_in = b.get("phases") or [{"name": "all", "max_age": None, "model": b["model"], "qhat": b["qhat"],
                                     "cal_scores": []}]
    phases, seen = [], {}
    for ph in phases_in:
        if id(ph["model"]) not in seen:                 # a phase without its own forest shares one: store it once
            a0 = len(members)
            _members(ph["model"], k, blobs, members)
            seen[id(ph["model"])] = [a0, len(members)]
        phases.append({"name": ph["name"], "max_age": ph["max_age"], "members": seen[id(ph["model"])],
                       "qhat": float(ph["qhat"]), "cal_scores": list(ph.get("cal_scores") or [])})
    meta = dict(b.get("meta", {}))
    meta["exported_from"] = os.path.basename(src)
    if release:
        meta["release"] = release
    hdr = {"classes": classes, "features": FEATURE_NAMES, "qhat": float(b["qhat"]), "alpha": float(b["alpha"]),
           "members": members, "phases": phases, "meta": meta}
    hj = json.dumps(hdr, separators=(",", ":"), default=str).encode()
    body = MAGIC + struct.pack("<I", len(hj)) + hj + b"".join(blobs)
    with open(dst, "wb") as f:
        f.write(body)
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "trees": sum(len(m["trees"]) for m in members),
            "nodes": sum(sum(m["trees"]) for m in members), "phases": len(phases)}


def _members(model, k: int, blobs: list, members: list) -> None:
    for cc in model.calibrated_classifiers_:
        rf = cc.estimator
        if list(rf.classes_) != list(range(k)):
            raise SystemExit("unexpected class encoding in the forest")
        trees = []
        for est in rf.estimators_:
            t = est.tree_
            n = t.node_count
            feat = np.where(t.children_left == -1, -1, t.feature).astype("<i2")
            thr = t.threshold.astype("<f8")
            left = t.children_left.astype("<i4")
            right = t.children_right.astype("<i4")
            val = t.value[:, 0, :].astype("<f8")
            val = (val / np.maximum(val.sum(axis=1, keepdims=True), 1e-12)).astype("<f4")
            blobs += [feat.tobytes(), thr.tobytes(), left.tobytes(), right.tobytes(), val.tobytes()]
            trees.append(int(n))
        cals = [{"x": [float(v) for v in c.X_thresholds_], "y": [float(v) for v in c.y_thresholds_]}
                for c in cc.calibrators]
        members.append({"trees": trees, "calibrators": cals})


def check(src: str, dst: str, dataset: str, n: int = 2000) -> float:
    b = joblib.load(src)
    rows = []
    with open(dataset) as f:
        for line in f:
            rows.append(json.loads(line)["features"])
            if len(rows) >= n:
                break
    X = np.asarray(rows, dtype=float)
    tm = TreeModel(dst)
    worst = 0.0
    for i, ph in enumerate(b.get("phases") or [{"model": b["model"]}]):
        ref = ph["model"].predict_proba(X)
        got = np.asarray([tm.proba(list(x), i) for x in rows])
        worst = max(worst, float(np.abs(ref - got).max()))
    return worst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--release", default=None, help="release name written into the model header, e.g. 2026.10.01")
    ap.add_argument("--check", default=None, help="dataset JSONL to compare probabilities against scikit-learn")
    a = ap.parse_args()
    info = export(a.src, a.dst, a.release)
    print(f"wrote {a.dst}: {info['bytes'] / 1024:.0f} KiB, {info['trees']} trees, {info['nodes']} nodes, sha256 {info['sha256'][:16]}…")
    if a.check:
        d = check(a.src, a.dst, a.check)
        print(f"max |p_router - p_sklearn| over the check set: {d:.2e}")
        if d > 1e-5:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
