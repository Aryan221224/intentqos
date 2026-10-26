"""Module 2 -- Traffic classifier: deterministic baseline + calibrated ML with abstention.

* RuleClassifier  -- the deterministic baseline the brief asks us to beat. Its
  thresholds follow OpenWrt qosify's published defaults (average packet length
  500 B for priority boost, 100 pps sustained 5 s for bulk detection).
  Re-implemented from the documented behaviour; no qosify code is used.
* MLClassifier    -- a small random forest on header-only features with
  calibrated probabilities and split-conformal prediction sets. When the set
  holds more than one class the model ABSTAINS: a flow keeps the class it earned
  from an earlier confident decision if that class is still in the set, otherwise
  the rule label is used, so the ML only changes a flow's class when it is
  confident at a known coverage level (1 - alpha).
* Classifier      -- orchestration: user corrections first (confidence 1.0),
  then ML (or rules), anti-flap commit logic, and a Page-Hinkley drift detector
  on the abstention rate that raises an observable event.

Inputs : flows (capture.Flow) at each policy tick.
Outputs: {flow_id: Decision(label, confidence, source, set, committed)}.
Failure: model missing/corrupt -> rules only (logged); a bad prediction can be
         corrected from the dashboard/API and the correction applies immediately.
Success: macro-F1 and time-to-correct-label beat the rule baseline on held-out runs
         (see ml/train.py report) AND call latency improves in the scenarios.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from . import LABEL_TO_TIN, LABELS
from .capture import Flow
from .features import FEATURE_NAMES, flow_features, recent_avg_len, recent_pps


@dataclass
class Decision:
    label: str
    confidence: float
    source: str                       # user | ml | ml-hold | rules | rules-fallback
    pset: list = field(default_factory=list)
    committed: str | None = None      # label currently driving enforcement
    restored: bool = False            # committed label kept from before an engine restart


class RuleClassifier:
    """Deterministic, explainable baseline (qosify-inspired)."""

    def __init__(self, cfg: dict):
        c = cfg["classifier"]
        self.bulk_pps = c["bulk_trigger_pps"]
        self.bulk_s = c["bulk_trigger_s"]
        self.max_len = c["prio_max_avg_pkt_len"]
        self._bulk_since: dict[str, float] = {}
        self._bulk: set[str] = set()

    def classify(self, f: Flow, now: float) -> tuple[str, str]:
        fid = f.id()
        avg = recent_avg_len(f, 4)
        if f.proto == 17:
            # UDP: small packets -> realtime voice/game; larger -> interactive video
            return ("voip" if avg < self.max_len else "video"), f"udp avg_len={avg:.0f}B"
        pps = recent_pps(f, 4)
        if fid in self._bulk:
            if recent_pps(f, 10) < self.bulk_pps / 2:
                self._bulk.discard(fid)
            else:
                return "bulk", f"tcp sustained {pps:.0f}pps"
        if pps > self.bulk_pps:
            t0 = self._bulk_since.setdefault(fid, now)
            if now - t0 >= self.bulk_s:
                self._bulk.add(fid)
                return "bulk", f"tcp >{self.bulk_pps}pps for {self.bulk_s:.0f}s"
        else:
            self._bulk_since.pop(fid, None)
        return "web", f"tcp {pps:.0f}pps"

    def forget(self, fid: str) -> None:
        self._bulk_since.pop(fid, None)
        self._bulk.discard(fid)


class MLClassifier:
    """Loads the router-format model (.iqm, pure Python) or, on a build machine, the scikit-learn bundle."""

    def __init__(self, path: str):
        iqm = os.path.splitext(path)[0] + ".iqm"
        if path.endswith(".iqm") or os.path.exists(iqm):
            from .treemodel import TreeModel
            m = TreeModel(iqm if not path.endswith(".iqm") else path)
            if m.features != FEATURE_NAMES:
                raise ValueError("model was trained on a different feature set")
            self.runtime = "pure-python"
            self.model = m
            self.classes, self.qhat, self.alpha, self.meta, self.path = m.classes, m.qhat, m.alpha, m.meta, m.path
            return
        import joblib  # build machines only; routers use the .iqm export
        b = joblib.load(path)
        if b.get("features") != FEATURE_NAMES:
            raise ValueError("model was trained on a different feature set")
        self.runtime = "scikit-learn"
        self.model = b["model"]
        self.classes = list(b["classes"])
        self.qhat = float(b["qhat"])
        self.alpha = float(b["alpha"])
        self.meta = b.get("meta", {})
        self.path = path

    def predict(self, X: list[list[float]]):
        if self.runtime == "pure-python":
            return self.model.predict(X)
        import numpy as np
        P = self.model.predict_proba(np.asarray(X, dtype=float))
        out = []
        for p in P:
            pset = [self.classes[i] for i, pi in enumerate(p) if 1.0 - pi <= self.qhat]
            j = int(p.argmax())
            out.append((self.classes[j], float(p[j]), pset))
        return out


class PageHinkley:
    """Page-Hinkley change detector for an increase in the mean of a 0/1 stream."""

    def __init__(self, delta: float = 0.02, threshold: float = 8.0, min_n: int = 200):
        self.delta, self.threshold, self.min_n = delta, threshold, min_n
        self.reset()

    def reset(self):
        self.n = 0
        self.mean = 0.0
        self.cum = 0.0
        self.min_cum = 0.0

    def update(self, x: float) -> bool:
        self.n += 1
        self.mean += (x - self.mean) / self.n
        self.cum += x - self.mean - self.delta
        self.min_cum = min(self.min_cum, self.cum)
        if self.n >= self.min_n and self.cum - self.min_cum > self.threshold:
            self.reset()
            return True
        return False


class Classifier:
    def __init__(self, cfg: dict, resolve, emit):
        self.cfg = cfg
        c = cfg["classifier"]
        self.rules = RuleClassifier(cfg)
        self.ml: MLClassifier | None = None
        self.mode = c["mode"]
        self.commit_ticks = int(c["commit_ticks"])
        self.demote_ticks = max(self.commit_ticks, int(c.get("demote_ticks", self.commit_ticks)))
        self.emit = emit
        self.model_error = None
        self.overrides_path = resolve(c["overrides_path"])
        self.corrections_path = resolve(c["corrections_path"])
        self.lock = threading.Lock()
        self.overrides: dict[str, dict] = self._load_overrides()
        self.state: dict[str, dict] = {}     # fid -> {committed, pending, pending_n[, restored]}
        # Labels survive an engine restart. A flow that is already running when the engine starts is
        # seen from the middle: its "first packets" are full-size data packets, and a video stream then
        # looks like a download (found in the lab). The label decided from the flow's real start is
        # therefore saved every few seconds (to RAM on a router) and restored for the same 5-tuple.
        self.labels_path = c.get("labels_path") or os.path.join(resolve(cfg.get("data", {}).get("dir", "data")),
                                                                "labels.json")
        self.labels_path = resolve(self.labels_path)
        self.restored = self._load_labels(float(c.get("restore_max_age_s", 120)))
        self._labels_saved = 0.0
        self.drift = PageHinkley()
        self.stats = {"ml": 0, "rules": 0, "abstain": 0, "user": 0, "drift_alarms": 0}
        if self.mode == "ml":
            path = resolve(c["model_path"])
            try:
                self.ml = MLClassifier(path)
            except Exception as e:   # noqa: BLE001 -- any load failure => rules
                self.model_error = f"{type(e).__name__}: {e}"
                self.emit("classifier", f"ML model unavailable ({self.model_error}); using rule baseline", "warn")

    # ----- user corrections ------------------------------------------------
    def _load_overrides(self) -> dict:
        try:
            with open(self.overrides_path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _load_labels(self, max_age: float) -> dict[str, str]:
        try:
            with open(self.labels_path) as f:
                doc = json.load(f)
        except (FileNotFoundError, ValueError):
            return {}
        if time.time() - float(doc.get("t", 0)) > max_age:
            return {}
        return {k: v for k, v in doc.get("labels", {}).items() if v in LABELS}

    def save_labels(self, wall: float | None = None) -> None:
        wall = time.time() if wall is None else wall
        labels = {fid: st["committed"] for fid, st in self.state.items() if st.get("committed")}
        try:
            os.makedirs(os.path.dirname(self.labels_path) or ".", exist_ok=True)
            tmp = self.labels_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"t": wall, "labels": labels}, f)
            os.replace(tmp, self.labels_path)
        except OSError:
            pass
        self._labels_saved = wall

    def _save_overrides(self) -> None:
        os.makedirs(os.path.dirname(self.overrides_path), exist_ok=True)
        tmp = self.overrides_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.overrides, f, indent=1)
        os.replace(tmp, self.overrides_path)

    @staticmethod
    def _pattern(f: Flow, scope: str) -> str:
        l, r, p, lp, rp = f.key
        if scope == "flow":
            return f"flow|{f.id()}"
        return f"app|{l}|{p}|{rp}"      # same device, same protocol, same remote service port

    def correct(self, f: Flow, label: str, scope: str = "app", now: float | None = None) -> dict:
        if label not in LABELS:
            raise ValueError(f"unknown label {label!r}; expected one of {LABELS}")
        now = time.time() if now is None else now
        pat = self._pattern(f, scope)
        with self.lock:
            self.overrides[pat] = {"label": label, "created": now, "flow": f.id()}
            self._save_overrides()
            st = self.state.setdefault(f.id(), {})
            st.update({"committed": label, "pending": None, "pending_n": 0})
        # keep the corrected example (header features only) for the next retraining run
        os.makedirs(os.path.dirname(self.corrections_path), exist_ok=True)
        with open(self.corrections_path, "a") as fh:
            fh.write(json.dumps({"t": now, "label": label, "features": flow_features(f, time.monotonic()),
                                 "feature_names": FEATURE_NAMES}) + "\n")
        self.stats["user"] += 1
        self.emit("correction", f"{f.id()} relabelled as {label} (scope={scope})", "info")
        return {"pattern": pat, "label": label}

    def _override(self, f: Flow) -> str | None:
        o = self.overrides.get(f"flow|{f.id()}") or self.overrides.get(self._pattern(f, "app"))
        return o["label"] if o else None

    # ----- classification ----------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in ("ml", "rules"):
            raise ValueError("mode must be ml or rules")
        self.mode = mode
        self.emit("classifier", f"classifier mode -> {mode}", "info")

    def classify(self, flows: list[Flow], now: float) -> dict[str, Decision]:
        out: dict[str, Decision] = {}
        ml_batch, ml_flows = [], []
        rule_cache = {}
        for f in flows:
            if not f.hist:
                continue
            fid = f.id()
            ov = self._override(f)
            if ov:
                out[fid] = Decision(ov, 1.0, "user", [ov])
                continue
            rl, why = self.rules.classify(f, now)
            rule_cache[fid] = rl
            if self.mode == "ml" and self.ml is not None:
                ml_batch.append(flow_features(f, now))
                ml_flows.append(f)
            else:
                out[fid] = Decision(rl, 1.0, "rules", [rl])
        if ml_batch:
            for f, (lab, conf, pset) in zip(ml_flows, self.ml.predict(ml_batch)):
                fid = f.id()
                abstain = len(pset) != 1
                if self.drift.update(1.0 if abstain else 0.0):
                    self.stats["drift_alarms"] += 1
                    self.emit("drift", "abstention rate rose significantly (Page-Hinkley) -- traffic mix changed; "
                                       "consider collecting corrections and retraining", "warn")
                st = self.state.get(fid) or {}
                if abstain and st.get("ml_committed") and (not pset or st.get("committed") in pset):
                    # unsure now, but the class this flow earned from a confident decision is still in the
                    # prediction set (or the set is empty: no class is certain): keep it rather than hand an established flow to the rules (a video
                    # stream fetching back-to-back while its buffer refills looks like a download to rules)
                    self.stats["abstain"] += 1
                    self.stats["held"] = self.stats.get("held", 0) + 1
                    out[fid] = Decision(st["committed"], conf, "ml-hold", pset)
                elif abstain:
                    self.stats["abstain"] += 1
                    out[fid] = Decision(rule_cache[fid], conf, "rules-fallback", pset)
                else:
                    self.stats["ml"] += 1
                    out[fid] = Decision(lab, conf, "ml", pset)
        for d in out.values():
            if d.source == "rules":
                self.stats["rules"] += 1
        # anti-flap commit: a label must persist `commit_ticks` ticks before it moves the flow, and
        # `demote_ticks` before it moves the flow to a lower-priority tin (asymmetric hysteresis)
        live = set()
        for f in flows:
            fid = f.id()
            if fid not in out:
                continue
            live.add(fid)
            d = out[fid]
            st = self.state.get(fid)
            if st is None:
                st = self.state[fid] = {"committed": None, "pending": None, "pending_n": 0}
                prev = self.restored.pop(fid, None)
                if prev and d.source != "user":
                    # decided from the flow's start before the restart; today's view starts mid-stream
                    st.update(committed=prev, restored=True)
                    self.stats["restored"] = self.stats.get("restored", 0) + 1
            if st.get("restored") and d.source != "user":
                d.committed, d.restored = st["committed"], True
                continue
            if st["committed"] is None or d.source == "user":
                st.pop("restored", None)
                st["committed"] = d.label
                st["ml_committed"] = d.source == "ml"
                st["pending"], st["pending_n"] = None, 0
            elif d.label != st["committed"]:
                if st["pending"] == d.label:
                    st["pending_n"] += 1
                else:
                    st["pending"], st["pending_n"] = d.label, 1
                need = (self.demote_ticks if LABEL_TO_TIN.get(d.label, 3) > LABEL_TO_TIN.get(st["committed"], 3)
                        else self.commit_ticks)
                if st["pending_n"] >= need:
                    st["committed"] = d.label
                    st["ml_committed"] = d.source == "ml"
                    st["pending"], st["pending_n"] = None, 0
            else:
                st["pending"], st["pending_n"] = None, 0
                if d.source == "ml":
                    st["ml_committed"] = True
            d.committed = st["committed"]
        alive = {f.id() for f in flows}
        for fid in list(self.state):
            if fid not in live and fid not in alive:
                self.state.pop(fid, None)
                self.rules.forget(fid)
        if time.time() - self._labels_saved >= 5.0:
            self.save_labels()
        return out

    def info(self) -> dict:
        return {"mode": self.mode, "model_loaded": self.ml is not None, "model_error": self.model_error,
                "model_meta": self.ml.meta if self.ml else None,
                "model_runtime": self.ml.runtime if self.ml else None, "model_path": self.ml.path if self.ml else None,
                "alpha": self.ml.alpha if self.ml else None, "stats": dict(self.stats),
                "overrides": len(self.overrides), "restored_labels": self.stats.get("restored", 0)}
