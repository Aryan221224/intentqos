#!/usr/bin/env python3
"""Train the header-only flow classifier, calibrate it, fit the conformal threshold,
and benchmark it against the deterministic rule baseline on held-out sessions.

    python3 ml/train.py --data ml/data/dataset.jsonl --out models/flowclf.joblib
    python3 ml/train.py ... --corrections data/corrections.jsonl   # fold in user corrections

Split is BY SESSION (train 60 % / calibration 15 % / test 25 %) so snapshots of
one flow never leak across splits. Outputs a joblib bundle and a report
(JSON + Markdown + confusion-matrix PNG) under models/ and docs/.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from intentqos import LABEL_TO_TIN, LABELS, TIN_NAMES  # noqa: E402
from intentqos.features import FEATURE_NAMES  # noqa: E402


def load(path: str):
    rows = []
    with open(path) as f:
        for line in f:
            rows.append(json.loads(line))
    return rows


def tin(label):
    return LABEL_TO_TIN.get(label, LABEL_TO_TIN[None])


def macro_f1(y_true, y_pred, labels):
    f1s = {}
    for c in labels:
        tp = sum(1 for a, b in zip(y_true, y_pred) if a == c and b == c)
        fp = sum(1 for a, b in zip(y_true, y_pred) if a != c and b == c)
        fn = sum(1 for a, b in zip(y_true, y_pred) if a == c and b != c)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1s[c] = 2 * p * r / (p + r) if p + r else 0.0
    present = [c for c in labels if any(y == c for y in y_true)]
    return sum(f1s[c] for c in present) / max(1, len(present)), f1s


def ece(probs, y_idx, bins=10):
    conf = probs.max(axis=1)
    pred = probs.argmax(axis=1)
    acc = (pred == y_idx).astype(float)
    e = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        m = (conf > lo) & (conf <= hi)
        if m.any():
            e += m.mean() * abs(acc[m].mean() - conf[m].mean())
    return float(e)


def flow_timeline_metrics(test_rows, hybrid_labels, commit_ticks=2):
    """Per flow: time until the committed TIN is right, and share of snapshots with the right tin."""
    by_flow = collections.defaultdict(list)
    for r, h in zip(test_rows, hybrid_labels):
        by_flow[(r["session"], tuple(r["key"]))].append((r["age"], r["label"], r["rule"], h))
    out = {"rules": collections.defaultdict(list), "hybrid": collections.defaultdict(list)}
    share = {"rules": collections.defaultdict(list), "hybrid": collections.defaultdict(list)}
    for _k, seq in by_flow.items():
        seq.sort()
        truth = seq[0][1]
        for name, idx in (("rules", 2), ("hybrid", 3)):
            committed, pending, n = None, None, 0
            first_ok, ok = None, 0
            for age, _t, rl, hl in seq:
                lab = rl if idx == 2 else hl
                if committed is None:
                    committed = lab
                elif lab != committed:
                    if pending == lab:
                        n += 1
                    else:
                        pending, n = lab, 1
                    if n >= commit_ticks:
                        committed, pending, n = lab, None, 0
                else:
                    pending, n = None, 0
                good = tin(committed) == tin(truth)
                ok += good
                if good and first_ok is None:
                    first_ok = age
            out[name][truth].append(first_ok if first_ok is not None else float("inf"))
            share[name][truth].append(ok / len(seq))
    res = {}
    for name in ("rules", "hybrid"):
        res[name] = {}
        for c in LABELS:
            v = out[name].get(c, [])
            s = share[name].get(c, [])
            if not v:
                continue
            fin = [x for x in v if x != float("inf")]
            res[name][c] = {"flows": len(v), "never_correct": len(v) - len(fin),
                            "median_time_to_correct_s": round(float(np.median(fin)), 2) if fin else None,
                            "correct_tin_time_share": round(float(np.mean(s)), 4)}
        allv = [x for c in out[name] for x in out[name][c]]
        alls = [x for c in share[name] for x in share[name][c]]
        fin = [x for x in allv if x != float("inf")]
        res[name]["_all"] = {"flows": len(allv), "never_correct": len(allv) - len(fin),
                             "median_time_to_correct_s": round(float(np.median(fin)), 2) if fin else None,
                             "correct_tin_time_share": round(float(np.mean(alls)), 4)}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join(ROOT, "ml/data/dataset.jsonl"))
    ap.add_argument("--out", default=os.path.join(ROOT, "models/flowclf.joblib"))
    ap.add_argument("--alpha", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--trees", type=int, default=60)
    ap.add_argument("--depth", type=int, default=14)
    ap.add_argument("--corrections", default=None)
    ap.add_argument("--phases", default="2,10",
                    help="flow-age boundaries (s) for phase-wise forests, e.g. '2,10'; '' = one forest for all ages")
    a = ap.parse_args(argv)

    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import RandomForestClassifier
    import joblib

    rows = load(a.data)
    sessions = sorted({r["session"] for r in rows})
    rnd = random.Random(a.seed)
    rnd.shuffle(sessions)
    n = len(sessions)
    n_test = max(1, round(n * 0.25))
    n_cal = max(1, round(n * 0.15))
    test_s, cal_s = set(sessions[:n_test]), set(sessions[n_test:n_test + n_cal])
    train = [r for r in rows if r["session"] not in test_s and r["session"] not in cal_s]
    cal = [r for r in rows if r["session"] in cal_s]
    test = [r for r in rows if r["session"] in test_s]
    if a.corrections and os.path.exists(a.corrections):
        with open(a.corrections) as f:
            extra = [json.loads(x) for x in f]
        for e in extra:
            if e.get("feature_names") == FEATURE_NAMES:
                for _ in range(5):            # corrections are rare and valuable: up-weight by repetition
                    train.append({"features": e["features"], "label": e["label"], "session": "corr"})
    classes = [c for c in LABELS if any(r["label"] == c for r in train)]
    cidx = {c: i for i, c in enumerate(classes)}
    X = lambda rs: np.asarray([r["features"] for r in rs], dtype=float)  # noqa: E731
    Y = lambda rs: np.asarray([cidx[r["label"]] for r in rs])           # noqa: E731

    t0 = time.time()

    def fit(rs):
        rf_ = RandomForestClassifier(n_estimators=a.trees, max_depth=a.depth, min_samples_leaf=2,
                                     class_weight="balanced_subsample", n_jobs=2, random_state=a.seed)
        m_ = CalibratedClassifierCV(rf_, method="isotonic", cv=3)
        m_.fit(X(rs), Y(rs))
        for cc in m_.calibrated_classifiers_:
            cc.estimator.n_jobs = 1      # per-tick batches are small: thread start-up costs more than it saves
        return m_

    def conformal(m_, rs):
        P_ = m_.predict_proba(X(rs))
        sc_ = 1.0 - P_[np.arange(len(rs)), Y(rs)]
        k_ = math.ceil((len(sc_) + 1) * (1 - a.alpha)) / len(sc_)
        q_ = float(np.quantile(sc_, min(1.0, k_), method="higher"))
        keep = np.sort(sc_)[np.linspace(0, len(sc_) - 1, min(600, len(sc_))).astype(int)]
        return q_, [round(float(v), 5) for v in keep]

    single = fit(train)                                   # one forest for all ages (the 1.x model)
    raw = RandomForestClassifier(n_estimators=a.trees, max_depth=a.depth, min_samples_leaf=2,
                                 class_weight="balanced_subsample", n_jobs=2, random_state=a.seed).fit(X(train), Y(train))
    bounds = [float(x) for x in a.phases.split(",") if x.strip()]
    edges = [0.0] + bounds + [None]
    phases = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        inb = lambda r: r["age"] >= lo and (hi is None or r["age"] < hi)  # noqa: E731
        tr = [r for r in train if r.get("session") == "corr" or inb(r)]
        ca = [r for r in cal if inb(r)]
        ok = len(tr) >= 300 and len(ca) >= 60 and {r["label"] for r in tr} >= set(classes)
        m = fit(tr) if ok and bounds else single
        cal_acc = None
        if m is not single:
            # keep the phase's own forest only if it beats the single forest on this phase's CALIBRATION rows
            # (the test sessions are not looked at for this choice)
            yc = Y(ca)
            own_acc = float(np.mean(m.predict_proba(X(ca)).argmax(axis=1) == yc))
            one_acc = float(np.mean(single.predict_proba(X(ca)).argmax(axis=1) == yc))
            cal_acc = {"own": round(own_acc, 4), "single": round(one_acc, 4)}
            if own_acc <= one_acc:
                m = single
        q, scores = conformal(m, ca if ok and bounds else cal)
        phases.append({"name": f"{lo:g}-{hi:g}s" if hi is not None else f">={lo:g}s", "max_age": hi, "model": m,
                       "qhat": q, "cal_scores": scores, "own": m is not single,
                       "rows": {"train": len(tr), "calibration": len(ca)}, "cal_accuracy": cal_acc})
    model = phases[-1]["model"]
    qhat = phases[-1]["qhat"]
    train_s = time.time() - t0

    def phase_idx(age):
        for i_, ph in enumerate(phases):
            if ph["max_age"] is None or age < ph["max_age"]:
                return i_
        return len(phases) - 1

    # ---------------- evaluation on held-out sessions ----------------
    Pt = np.zeros((len(test), len(classes)))
    qts = np.zeros(len(test))
    for i_, ph in enumerate(phases):
        idx = [j for j, r in enumerate(test) if phase_idx(r["age"]) == i_]
        if idx:
            Pt[idx] = ph["model"].predict_proba(X([test[j] for j in idx]))
            qts[idx] = ph["qhat"]
    Ps = single.predict_proba(X(test))
    yt = Y(test)
    pred = [classes[i] for i in Pt.argmax(axis=1)]
    truth = [r["label"] for r in test]
    rule = [r["rule"] for r in test]
    sets = [[classes[j] for j, p in enumerate(row) if 1 - p <= qts[i_]] for i_, row in enumerate(Pt)]
    pred_single = [classes[i] for i in Ps.argmax(axis=1)]
    early = [i for i, r in enumerate(test) if r["age"] < (bounds[0] if bounds else 2.0)]
    abstain = [len(s) != 1 for s in sets]
    hybrid = [rule[i] if abstain[i] else pred[i] for i in range(len(test))]
    cover = float(np.mean([truth[i] in sets[i] for i in range(len(test))]))
    ml_f1, ml_pc = macro_f1(truth, pred, LABELS)
    ru_f1, ru_pc = macro_f1(truth, rule, LABELS)
    hy_f1, hy_pc = macro_f1(truth, hybrid, LABELS)
    tin_acc = lambda p: float(np.mean([tin(a_) == tin(b_) for a_, b_ in zip(truth, p)]))  # noqa: E731
    sel = [i for i in range(len(test)) if not abstain[i]]
    t1 = time.time()
    for _ in range(20):
        model.predict_proba(X(test[:32]))
    infer_ms = (time.time() - t1) / 20 * 1000
    conf_m = {c: {d: 0 for d in classes} for c in classes}
    for t_, p_ in zip(truth, pred):
        if t_ in conf_m and p_ in conf_m[t_]:
            conf_m[t_][p_] += 1
    imp = np.mean([est.estimator.feature_importances_ for est in model.calibrated_classifiers_], axis=0)
    top_feat = sorted(zip(FEATURE_NAMES, imp), key=lambda x: -x[1])[:10]
    timeline = flow_timeline_metrics(test, hybrid)

    report = {
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": os.path.relpath(a.data, ROOT), "rows": {"train": len(train), "calibration": len(cal), "test": len(test)},
        "sessions": {"train": n - n_test - n_cal, "calibration": n_cal, "test": n_test},
        "classes": classes, "alpha": a.alpha, "qhat": round(qhat, 4),
        "model": f"RandomForest({a.trees} trees, depth {a.depth}) + isotonic calibration (3-fold)",
        "train_seconds": round(train_s, 1), "inference_ms_per_32_flows": round(infer_ms, 2),
        "label_level": {
            "rules": {"macro_f1": round(ru_f1, 4), "accuracy": round(float(np.mean([a_ == b_ for a_, b_ in zip(truth, rule)])), 4),
                      "per_class_f1": {k_: round(v, 3) for k_, v in ru_pc.items()}},
            "ml_argmax": {"macro_f1": round(ml_f1, 4), "accuracy": round(float(np.mean([a_ == b_ for a_, b_ in zip(truth, pred)])), 4),
                          "per_class_f1": {k_: round(v, 3) for k_, v in ml_pc.items()}},
            "hybrid_conformal": {"macro_f1": round(hy_f1, 4),
                                 "accuracy": round(float(np.mean([a_ == b_ for a_, b_ in zip(truth, hybrid)])), 4),
                                 "per_class_f1": {k_: round(v, 3) for k_, v in hy_pc.items()}},
        },
        "tin_level_accuracy": {"rules": round(tin_acc(rule), 4), "ml_argmax": round(tin_acc(pred), 4),
                               "hybrid_conformal": round(tin_acc(hybrid), 4)},
        "conformal": {"target_coverage": 1 - a.alpha, "empirical_coverage": round(cover, 4),
                      "abstain_rate": round(float(np.mean(abstain)), 4),
                      "selective_accuracy": round(float(np.mean([truth[i] == pred[i] for i in sel])), 4) if sel else None,
                      "mean_set_size": round(float(np.mean([len(s) for s in sets])), 3)},
        "calibration": {"ece_raw_rf": round(ece(raw.predict_proba(X(test)), yt), 4),
                        "ece_calibrated": round(ece(Pt, yt), 4)},
        "per_flow_timeline": timeline,
        "phases": [{"name": ph["name"], "max_age": ph["max_age"], "own_forest": ph["own"], "rows": ph["rows"],
                    "calibration_accuracy": ph["cal_accuracy"],
                    "qhat": round(ph["qhat"], 4)} for ph in phases],
        "phase_wise_vs_single": {
            "macro_f1": {"phase_wise": round(ml_f1, 4), "single": round(macro_f1(truth, pred_single, LABELS)[0], 4)},
            "early_flow_accuracy": {
                "phase_wise": round(float(np.mean([truth[i] == pred[i] for i in early])), 4) if early else None,
                "single": round(float(np.mean([truth[i] == pred_single[i] for i in early])), 4) if early else None,
                "rows": len(early)}},
        "confusion_ml": conf_m,
        "top_features": [[f, round(float(v), 4)] for f, v in top_feat],
        "payload_bytes_used": 0,
        "features": FEATURE_NAMES,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    joblib.dump({"model": model, "classes": classes, "qhat": qhat, "alpha": a.alpha, "features": FEATURE_NAMES,
                 "phases": [{k_: ph[k_] for k_ in ("name", "max_age", "model", "qhat", "cal_scores")} for ph in phases],
                 "meta": {k_: report[k_] for k_ in ("trained_at", "rows", "model", "tin_level_accuracy", "conformal",
                                                    "phases")}},
                a.out, compress=3)
    with open(a.out.replace(".joblib", ".report.json"), "w") as f:
        json.dump(report, f, indent=1)
    _plot_confusion(conf_m, classes, os.path.join(ROOT, "docs/img/classifier_confusion.png"))
    _markdown(report, os.path.join(ROOT, "docs/CLASSIFIER_REPORT.md"))
    print(json.dumps({k_: report[k_] for k_ in ("rows", "label_level", "tin_level_accuracy", "conformal", "calibration",
                                                  "inference_ms_per_32_flows", "phases", "phase_wise_vs_single")}, indent=1))
    print(json.dumps(timeline, indent=1))
    return 0


def _plot_confusion(cm, classes, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    M = np.array([[cm[t][p] for p in classes] for t in classes], dtype=float)
    Mn = M / np.maximum(1, M.sum(axis=1, keepdims=True))
    fig, ax = plt.subplots(figsize=(5.6, 4.6), dpi=150)
    ax.imshow(Mn, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(len(classes)), classes, rotation=35, ha="right")
    ax.set_yticks(range(len(classes)), classes)
    for i in range(len(classes)):
        for j in range(len(classes)):
            ax.text(j, i, f"{Mn[i, j] * 100:.0f}", ha="center", va="center", fontsize=8,
                    color="white" if Mn[i, j] > 0.55 else "#222")
    ax.set_xlabel("predicted")
    ax.set_ylabel("true")
    ax.set_title("Header-only classifier, held-out sessions (% of row)", fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _markdown(r, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = r["label_level"]
    tl = r["per_flow_timeline"]
    lines = [
        "# Classifier report (auto-generated by `ml/train.py`)", "",
        f"Trained {r['trained_at']} on `{r['data']}`: {r['rows']['train']} train / {r['rows']['calibration']} "
        f"calibration / {r['rows']['test']} test snapshots, split **by session** "
        f"({r['sessions']['train']}/{r['sessions']['calibration']}/{r['sessions']['test']}).", "",
        f"Model: {r['model']}. Features: {len(r['features'])} header-only statistics (sizes, timing, direction, "
        "protocol). **Payload bytes used: 0.** Ports and addresses are not features.", "",
        "## Held-out results", "",
        "| | macro-F1 (label) | accuracy (label) | accuracy (scheduling class) |", "|---|---|---|---|",
        f"| Rule baseline (qosify-style) | {L['rules']['macro_f1']:.3f} | {L['rules']['accuracy']:.3f} | {r['tin_level_accuracy']['rules']:.3f} |",
        f"| ML (argmax) | {L['ml_argmax']['macro_f1']:.3f} | {L['ml_argmax']['accuracy']:.3f} | {r['tin_level_accuracy']['ml_argmax']:.3f} |",
        f"| **Hybrid: ML when confident, rules when abstaining** | **{L['hybrid_conformal']['macro_f1']:.3f}** | "
        f"**{L['hybrid_conformal']['accuracy']:.3f}** | **{r['tin_level_accuracy']['hybrid_conformal']:.3f}** |", "",
        f"Conformal prediction (target coverage {r['conformal']['target_coverage']:.0%}): empirical coverage "
        f"{r['conformal']['empirical_coverage']:.3f}, abstain rate {r['conformal']['abstain_rate']:.3f}, "
        f"accuracy when not abstaining {r['conformal']['selective_accuracy']}.", "",
        f"Calibration (expected calibration error): raw RF {r['calibration']['ece_raw_rf']:.3f} -> "
        f"isotonic {r['calibration']['ece_calibrated']:.3f}.", "",
        f"Inference: {r['inference_ms_per_32_flows']} ms per batch of 32 flows.", "",
        "## Time to the right scheduling class (per flow, held-out)", "",
        "| class | flows | rules: median s to correct | rules: share of lifetime correct | hybrid: median s | hybrid: share correct |",
        "|---|---|---|---|---|---|",
    ]
    for c in list(LABELS) + ["_all"]:
        a_, b_ = tl["rules"].get(c), tl["hybrid"].get(c)
        if not a_ or not b_:
            continue
        lines.append(f"| {c if c != '_all' else '**all flows**'} | {a_['flows']} | {a_['median_time_to_correct_s']} | "
                     f"{a_['correct_tin_time_share']:.3f} | {b_['median_time_to_correct_s']} | {b_['correct_tin_time_share']:.3f} |")
    lines += ["", "## Most important features", ""]
    lines += [f"- `{f}` ({v:.3f})" for f, v in r["top_features"]]
    lines += ["", "![confusion](img/classifier_confusion.png)", "",
              "> Caveat: trained and tested on emulated traffic from `testbed/traffic.py` with randomised parameters. "
              "Accuracy on real household traffic will differ; record consented real traces with `ml/record.py` and "
              "use dashboard corrections (`data/corrections.jsonl`) to retrain."]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
