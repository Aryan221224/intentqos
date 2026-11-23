"""Does the classifier hold up on traffic it was not trained on, and does adaptive conformal keep its promise?

The model in models/flowclf.joblib was trained, calibrated and tested on dataset A (ml/data/dataset.jsonl).
This script replays a SECOND dataset generated later with a different seed (different access rates, app mixes
and timings: ml/data/dataset_b.jsonl) in time order, as a gateway would see it, and compares:

  * accuracy: phase-wise forests vs the single forest,
  * prediction-set coverage with the fixed split-conformal threshold vs adaptive conformal inference (ACI,
    Gibbs & Candes 2021), which nudges its miscoverage level after every labelled outcome,
  * the same under a synthetic covariate shift (packet sizes x1.3 and inter-arrival times x1.5, i.e. a different
    access network and encapsulation), labelled as synthetic,
  * ACI with feedback on only 10 % of decisions (a household corrects or a weak label arrives for a few flows).

Feedback here comes from the dataset's ground-truth labels; on a gateway it comes from household corrections,
weak labels (Microsoft 365 endpoints) and the self-check, so it is sparser than this. Results are emulation.

    python3 ml/eval_drift.py --model models/flowclf.joblib --data ml/data/dataset_b.jsonl --out docs/drift_eval.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

import joblib
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from intentqos.classifier import AdaptiveConformal  # noqa: E402
from intentqos.features import FEATURE_NAMES  # noqa: E402

F = {n: i for i, n in enumerate(FEATURE_NAMES)}
SIZE_FEATS = [F[n] for n in ("mean_up", "mean_dn", "max_up", "max_dn", "std_up", "std_dn", "early_mean", "early_max")]
IAT_FEATS = [F[n] for n in ("iat_ms_up", "iat_ms_dn", "early_iat_ms")]
RATE_FEATS = [F[n] for n in ("pps_up", "pps_dn")]
KBPS_FEATS = [F[n] for n in ("kbps_up", "kbps_dn")]


def phase_of(phases, age):
    for i, ph in enumerate(phases):
        if ph["max_age"] is None or age < ph["max_age"]:
            return i
    return len(phases) - 1


def replay(bundle, rows, shift=False, feedback_share=1.0, gamma=0.005, seed=7):
    classes = bundle["classes"]
    cidx = {c: i for i, c in enumerate(classes)}
    phases = bundle.get("phases") or [{"max_age": None, "model": bundle["model"], "qhat": bundle["qhat"],
                                       "cal_scores": []}]
    alpha = bundle.get("alpha", 0.1)
    X = np.asarray([r["features"] for r in rows], dtype=float)
    if shift:                                         # sizes x1.3 and gaps x1.5, with the rates that follow
        X[:, SIZE_FEATS] *= 1.3
        X[:, IAT_FEATS] *= 1.5
        X[:, RATE_FEATS] /= 1.5
        X[:, KBPS_FEATS] *= 1.3 / 1.5
        X[:, F["log_bytes"]] += np.log10(1.3 / 1.5)
    y = np.asarray([cidx[r["label"]] for r in rows])
    ph_idx = np.asarray([phase_of(phases, r["age"]) for r in rows])
    P = np.zeros((len(rows), len(classes)))
    for i, ph in enumerate(phases):
        m = ph_idx == i
        if m.any():
            P[m] = ph["model"].predict_proba(X[m])
    Ps = bundle["model"].predict_proba(X)            # the single forest (for phases without their own, identical)
    acis = [AdaptiveConformal(alpha, ph.get("cal_scores") or [], ph["qhat"], gamma) for ph in phases]
    rnd = random.Random(seed)
    fixed_cov, aci_cov, fixed_size, aci_size, fixed_abst, aci_abst = [], [], [], [], [], []
    for j in range(len(rows)):
        i = ph_idx[j]
        p = P[j]
        q_fixed = phases[i]["qhat"]
        q_aci = acis[i].qhat()
        s_fixed = [k for k in range(len(classes)) if 1 - p[k] <= q_fixed]
        s_aci = [k for k in range(len(classes)) if 1 - p[k] <= q_aci]
        fixed_cov.append(y[j] in s_fixed)
        aci_cov.append(y[j] in s_aci)
        fixed_size.append(len(s_fixed))
        aci_size.append(len(s_aci))
        fixed_abst.append(len(s_fixed) != 1)
        aci_abst.append(len(s_aci) != 1)
        if rnd.random() < feedback_share:
            acis[i].feedback(y[j] in s_aci, "replay")
    early = np.asarray([r["age"] < 2.0 for r in rows])
    acc = lambda Q, m=None: round(float(np.mean((Q.argmax(1) == y)[m] if m is not None else Q.argmax(1) == y)), 4)  # noqa: E731
    return {"rows": len(rows), "target_coverage": round(1 - alpha, 3),
            "accuracy": {"phase_wise": acc(P), "single": acc(Ps)},
            "early_flow_accuracy": {"phase_wise": acc(P, early), "single": acc(Ps, early), "rows": int(early.sum())},
            "fixed_conformal": {"coverage": round(float(np.mean(fixed_cov)), 4), "abstain_rate": round(float(np.mean(fixed_abst)), 4),
                                "mean_set_size": round(float(np.mean(fixed_size)), 3)},
            "adaptive_conformal": {"coverage": round(float(np.mean(aci_cov)), 4), "abstain_rate": round(float(np.mean(aci_abst)), 4),
                                   "mean_set_size": round(float(np.mean(aci_size)), 3), "feedback_share": feedback_share,
                                   "final_alpha": [a.info()["alpha"] for a in acis]}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", default=os.path.join(ROOT, "models", "flowclf.joblib"))
    ap.add_argument("--data", default=os.path.join(ROOT, "ml", "data", "dataset_b.jsonl"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "drift_eval.json"))
    a = ap.parse_args(argv)
    bundle = joblib.load(a.model)
    with open(a.data) as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    rows = [r for r in rows if r.get("label") in bundle["classes"]]
    rows.sort(key=lambda r: (r["t"], r["age"]))
    out = {"model": os.path.basename(a.model), "trained_on": (bundle.get("meta") or {}).get("rows"),
           "replayed": os.path.basename(a.data), "note": "emulation testbed traffic; feedback from ground-truth labels",
           "new_sessions": replay(bundle, rows),
           "new_sessions_synthetic_shift": replay(bundle, rows, shift=True),
           "new_sessions_synthetic_shift_10pct_feedback": replay(bundle, rows, shift=True, feedback_share=0.1)}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
