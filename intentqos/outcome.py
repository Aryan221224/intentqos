"""Experience-driven control: steer by each app's Quality of Outcome, predict before applying.

Three parts, all wrapped by the existing shield (policy.shield) and transactional apply:

1. **Objective.** The weighted QoO of the apps that are live right now (experience.py), with calls,
   games and leased devices weighted most. The deterministic policy (policy.decide) still builds every
   plan; this module chooses its two free parameters - realtime headroom and the bulk share of the
   remainder - from a small grid, by predicted outcome.
2. **Predict, then apply.** For a candidate plan, each tin's queueing delay is predicted with the
   ΔQ queue model (deltaq.queue_model) from the tin's demand and the capacity HTB would give it, and
   corrected by a per-tin factor learned from measured queue delays. An app's predicted latency is its
   measured latency with the current queue part swapped for the predicted one (ΔQ composes by
   addition, so the other parts - LAN, internet path - stay). The **outcome gate** refuses a plan that
   is predicted to push a protected app (call, game, lease) below QoO 60 and 10 points under where it
   is now. Rate cuts (the estimator's bloat response) are never refused: they only lower delay.
3. **Safe per-home tuning** (opt-in, [outcome] safe_tuning = true). The estimator's bloat threshold is
   tuned per home with a SafeOpt-style rule (Sui et al., ICML 2015): a Gaussian-process model of the
   objective and of a safety function (the worst protected app's QoO), and only settings whose lower
   confidence bound on safety stays above the limit, next to settings already tried, are tried.

The prediction is a model, not a measurement; the console shows predicted and measured side by side.
"""
from __future__ import annotations

import copy
import math
import time

from . import TIN_BE, TIN_BULK, TIN_INTENT, TIN_NAMES, TIN_VIDEO, TIN_VOICE
from .deltaq import queue_model
from .policy import decide
from .qoo import score

PRIO = {TIN_INTENT: 0, TIN_VOICE: 0, TIN_VIDEO: 1, TIN_BE: 2, TIN_BULK: 3}
WEIGHT = {"voip": 3.0, "gaming": 3.0, "video": 3.0, "web": 1.5, "streaming": 1.0, "bulk": 0.5}
PROTECTED = ("voip", "gaming", "video")
HEADROOM = (1.15, 1.3, 1.6, 2.0)
BULK_SHARE = (0.15, 0.25, 1 / 3, 0.5)


def tin_capacity(plan_d: dict, demand_d: dict) -> dict:
    """kbit/s each tin could get under HTB: its guarantee at least; above that, what higher-priority tins'
    demand and lower-priority tins' guarantees in use leave over."""
    R, g = plan_d["rate"], plan_d["guarantee"]
    out = {}
    for t in g:
        used = 0.0
        for u in g:
            if u == t:
                continue
            if PRIO[u] < PRIO[t] or (PRIO[u] == PRIO[t] and u < t):
                used += demand_d.get(u, 0.0)
            else:
                used += min(demand_d.get(u, 0.0), g[u])
        out[t] = max(g[t], R - used)
    return out


def predict_queues(plan: dict, demand: dict, calib: dict, aqm_ms: float = 5.0) -> dict:
    """{dir: {tin: (p50_ms, p99_ms, loss)}} predicted for `plan`."""
    out = {}
    for d in ("down", "up"):
        cap = tin_capacity(plan[d], demand[d])
        out[d] = {}
        for t, c in cap.items():
            q = queue_model(demand[d].get(t, 0.0), c, aqm_target_ms=aqm_ms)
            k = calib.get((d, t), 1.0)
            out[d][t] = (q.pct(50) * k, q.pct(99) * k, q.loss)
    return out


class SafeTuner:
    """SafeOpt-style 1-D tuning of one parameter over a grid, with GP lower/upper confidence bounds."""

    def __init__(self, grid: list[float], start: float, safety_min: float, beta: float = 2.0,
                 length: float | None = None, dwell_s: float = 60.0):
        self.grid = sorted(grid)
        self.x = min(self.grid, key=lambda v: abs(v - start))
        self.safety_min = safety_min
        self.beta = beta
        self.length = length or (self.grid[-1] - self.grid[0]) / 3.0
        self.dwell = dwell_s
        self.obs: list[tuple[float, float, float]] = []   # (x, objective, safety)
        self.acc: list[tuple[float, float]] = []
        self.t0 = None
        self.safe_seed = self.x
        self.log: list = []

    def _gp(self, which: int):
        """Posterior mean/sd at every grid point from observations (RBF kernel, noise 4 QoO points)."""
        X = [o[0] for o in self.obs]
        Y = [o[which] for o in self.obs]
        if not X:
            return [(50.0, 50.0)] * len(self.grid)
        mu0 = sum(Y) / len(Y)
        k = lambda a, b: 400.0 * math.exp(-0.5 * ((a - b) / self.length) ** 2)  # noqa: E731  (variance 20^2)
        n = len(X)
        K = [[k(X[i], X[j]) + (16.0 if i == j else 0.0) for j in range(n)] for i in range(n)]
        L = _cholesky(K)
        alpha = _solve_chol(L, [y - mu0 for y in Y])
        out = []
        for x in self.grid:
            ks = [k(x, xi) for xi in X]
            mu = mu0 + sum(a * b for a, b in zip(ks, alpha))
            v = _solve_lower(L, ks)
            var = max(1e-6, 400.0 - sum(t * t for t in v))
            out.append((mu, math.sqrt(var)))
        return out

    def step(self, now: float, objective: float | None, safety: float | None) -> float | None:
        """Feed one sample; returns a new setting when it is time to move, else None."""
        if objective is None or safety is None:
            return None
        if self.t0 is None:
            self.t0 = now
        if safety < self.safety_min and self.x != self.safe_seed:
            self.log.append((round(now), self.x, "unsafe: back to the safe setting"))
            self.obs.append((self.x, objective, safety))
            self.x, self.acc, self.t0 = self.safe_seed, [], now
            return self.x
        self.acc.append((objective, safety))
        if now - self.t0 < self.dwell:
            return None
        o = sum(a for a, _ in self.acc) / len(self.acc)
        s = min(b for _, b in self.acc)
        self.obs.append((self.x, o, s))
        self.obs = self.obs[-30:]
        if s >= self.safety_min and o >= max((ob[1] for ob in self.obs if ob[0] == self.safe_seed), default=-1):
            self.safe_seed = self.x
        f, g = self._gp(1), self._gp(2)
        tried = {ob[0] for ob in self.obs if ob[2] >= self.safety_min}
        i_tried = [i for i, x in enumerate(self.grid) if x in tried] or [self.grid.index(self.safe_seed)]
        cands = {j for i in i_tried for j in (i - 1, i, i + 1) if 0 <= j < len(self.grid)}
        safe = [j for j in cands if g[j][0] - self.beta * g[j][1] >= self.safety_min or self.grid[j] in tried]
        if not safe:
            safe = [self.grid.index(self.safe_seed)]
        best = max(safe, key=lambda j: f[j][0] + self.beta * f[j][1])
        self.acc, self.t0 = [], now
        new = self.grid[best]
        if new != self.x:
            self.log.append((round(now), new, f"trying (objective {o:.0f}, worst protected app {s:.0f})"))
            self.log = self.log[-20:]
        self.x = new
        return new


def _cholesky(A):
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = A[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            L[i][j] = math.sqrt(max(s, 1e-9)) if i == j else s / L[j][j]
    return L


def _solve_lower(L, b):
    y = []
    for i in range(len(b)):
        y.append((b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i])
    return y


def _solve_chol(L, b):
    y = _solve_lower(L, b)
    n = len(y)
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


class OutcomeController:
    def __init__(self, cfg: dict, tracker, backend_name: str = "htb"):
        o = cfg.get("outcome", {}) if isinstance(cfg.get("outcome"), dict) else {}
        self.enabled = bool(o.get("enabled", True))
        self.gate_on = bool(o.get("gate", True))
        self.period = float(o.get("period_s", 2.0))
        self.gate_min_qoo = float(o.get("gate_min_qoo", 60.0))
        self.gate_drop = float(o.get("gate_drop", 10.0))
        self.tracker = tracker
        self.backend = backend_name
        self.theta = {"realtime_headroom": cfg["policy"]["realtime_headroom"],
                      "remainder_bulk_share": cfg["policy"].get("remainder_bulk_share", 1 / 3)}
        self.calib: dict = {}
        self.last_t = 0.0
        self.last_pred: dict = {}
        self.gated = 0
        self.decisions: list = []
        self.objective = None
        self.worst = None
        st = bool(o.get("safe_tuning", False))
        self.tuner = SafeTuner(list(o.get("tune_grid", [8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0])),
                               cfg["estimator"]["bloat_threshold_ms"], float(o.get("tune_safety_min", 60.0)),
                               dwell_s=float(o.get("tune_dwell_s", 60.0))) if st else None

    # ------------------------------------------------------------------ helpers
    def cfg_for(self, cfg: dict) -> dict:
        """The config decide() should use: the policy with the chosen free parameters."""
        if not self.enabled:
            return cfg
        c = dict(cfg)
        c["policy"] = {**cfg["policy"], **self.theta}
        return c

    def _apps(self):
        return [a for a in (self.tracker.snapshot.get("apps") or []) if a.get("qoo") is not None]

    def objective_of(self, apps, pred_qoo: dict | None = None, boosted: set | None = None) -> float | None:
        tot = wsum = 0.0
        for i, a in enumerate(apps):
            w = WEIGHT.get(a["label"], 1.0) * (2.0 if boosted and a["ip"] in boosted else 1.0)
            q = pred_qoo[i] if pred_qoo is not None else a["qoo"]
            tot += w * q
            wsum += w
        return tot / wsum if wsum else None

    def predict_app_qoo(self, apps, plan, demand, cur_q, req) -> list[float]:
        pq = predict_queues(plan, demand, self.calib)
        out = []
        for a in apps:
            tin = next((k for k, v in TIN_NAMES.items() if v == a.get("tin")), TIN_BE)
            lat = dict(a.get("latency_ms") or {})
            new = {}
            for p, key in ((50, 0), (99, 1)):
                if p not in lat:
                    continue
                now_q = cur_q["down"].get(tin, (0, 0))[key] + cur_q["up"].get(tin, (0, 0))[key]
                pred_q = pq["down"][tin][key] + pq["up"][tin][key]
                new[p] = max(0.5, lat[p] - now_q + pred_q)
            if 90 in lat and 50 in new:
                new[90] = new[50] + (lat[90] - lat.get(50, lat[90]))
            loss = a.get("loss_pct")
            if a.get("label") in PROTECTED and loss is not None and a.get("latency_source") == "estimated":
                loss = max(loss, 100.0 * max(pq["down"][tin][2], pq["up"][tin][2]))
            s = score(req.get(a["label"], {}), new, loss, None)
            out.append(s["qoo"] if s["qoo"] is not None else a["qoo"])
        return out

    def _current_queues(self) -> dict:
        qs = self.tracker.snapshot.get("queues") or {}
        out = {}
        for d in ("down", "up"):
            out[d] = {}
            for t, name in TIN_NAMES.items():
                v = (qs.get(d) or {}).get(name) or {}
                out[d][t] = (v.get(50, 0.0), v.get(99, 0.0))
        return out

    def calibrate(self, applied: dict | None, demand: dict) -> None:
        if not applied:
            return
        pred = predict_queues(applied, demand, {})
        cur = self._current_queues()
        for d in ("down", "up"):
            for t in TIN_NAMES:
                p50 = pred[d][t][0]
                m50 = cur[d][t][0]
                if demand[d].get(t, 0.0) > 50 and p50 > 0.3 and m50 > 0:
                    r = max(0.5, min(4.0, m50 / p50))
                    self.calib[(d, t)] = 0.9 * self.calib.get((d, t), 1.0) + 0.1 * r

    # ------------------------------------------------------------------ per step
    def step(self, cfg: dict, rates: dict, demand: dict, intents, applied: dict | None, now: float | None = None,
             estimator=None) -> None:
        now = time.monotonic() if now is None else now
        if not self.enabled or now - self.last_t < self.period:
            return
        self.last_t = now
        apps = self._apps()
        boosted = {it.target for it in intents}
        self.objective = self.objective_of(apps, boosted=boosted)
        prot = [a["qoo"] for a in apps if a["label"] in PROTECTED or a["ip"] in boosted]
        self.worst = min(prot) if prot else None
        self.calibrate(applied, demand)
        if self.tuner is not None and estimator is not None:
            new = self.tuner.step(now, self.objective, self.worst)
            if new is not None:
                estimator.e["bloat_threshold_ms"] = new
        if not apps or self.backend != "htb":
            return                              # CAKE ignores per-tin guarantees: nothing to choose
        req = self.tracker.req
        cur_q = self._current_queues()
        best, best_val = None, None
        cur_val = None
        for h in HEADROOM:
            for bs in BULK_SHARE:
                c = copy.copy(cfg)
                c["policy"] = {**cfg["policy"], "realtime_headroom": h, "remainder_bulk_share": bs}
                plan = decide(c, rates, demand, intents)
                q = self.predict_app_qoo(apps, plan, demand, cur_q, req)
                val = self.objective_of(apps, q, boosted)
                # guard: no protected app predicted below the gate limit if it is above it now
                bad = any(a["qoo"] >= self.gate_min_qoo > qq for a, qq in zip(apps, q)
                          if a["label"] in PROTECTED or a["ip"] in boosted)
                if bad or val is None:
                    continue
                if (h, bs) == (self.theta["realtime_headroom"], self.theta["remainder_bulk_share"]):
                    cur_val = val
                if best_val is None or val > best_val + 1e-9:
                    best, best_val = (h, bs), val
        if best is None:
            return
        if cur_val is None or best_val > cur_val + 2.0:        # hysteresis: move only for a clear gain
            old = dict(self.theta)
            self.theta = {"realtime_headroom": best[0], "remainder_bulk_share": best[1]}
            if old != self.theta:
                self.decisions.insert(0, {"t": round(time.time(), 1), "theta": dict(self.theta),
                                          "predicted": round(best_val, 1),
                                          "current": round(cur_val, 1) if cur_val is not None else None})
                del self.decisions[20:]

    def gate(self, plan: dict, applied: dict | None, demand: dict, intents) -> str | None:
        """Reason to refuse `plan` (guarantee moves predicted to hurt a protected app), or None to allow it."""
        if not (self.enabled and self.gate_on) or applied is None or self.backend != "htb":
            return None
        apps = self._apps()
        boosted = {it.target for it in intents}
        prot = [(i, a) for i, a in enumerate(apps) if a["label"] in PROTECTED or a["ip"] in boosted]
        if not prot:
            return None
        cur_q = self._current_queues()
        q_new = self.predict_app_qoo(apps, plan, demand, cur_q, self.tracker.req)
        q_old = self.predict_app_qoo(apps, applied, demand, cur_q, self.tracker.req)
        for i, a in prot:
            if q_new[i] < self.gate_min_qoo and q_new[i] < q_old[i] - self.gate_drop:
                self.gated += 1
                return (f"outcome gate: {a['label']} on {a.get('device') or a['ip']} predicted QoO "
                        f"{q_old[i]:.0f} -> {q_new[i]:.0f}")
        return None

    def info(self) -> dict:
        return {"enabled": self.enabled, "objective": round(self.objective, 1) if self.objective is not None else None,
                "worst_protected": self.worst, "theta": {k: round(v, 3) for k, v in self.theta.items()},
                "gated": self.gated, "decisions": self.decisions[:10],
                "calibration": {f"{d}/{TIN_NAMES[t]}": round(k, 2) for (d, t), k in self.calib.items()},
                "safe_tuning": ({"setting_ms": self.tuner.x, "safe_seed_ms": self.tuner.safe_seed,
                                 "log": self.tuner.log[-8:]} if self.tuner else None)}
