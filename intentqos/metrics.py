"""Module 7 (engine side) -- metrics, fairness, history and Prometheus exposition.

Fairness is reported with Jain's index J = (sum x)^2 / (n * sum x^2), bounded
1/n .. 1 (Jain, Chiu & Hawe, DEC-TR-301, 1984). Plain Jain ignores demand, so we
compute it over *demand-normalised* allocations x_i = got_i / min(demand_i, fair_i)
where fair_i is the max-min fair share (Ware et al., "Beyond Jain's Fairness
Index", HotNets 2019, motivate this). The evaluation harness additionally
reports their "harm" metric (degradation versus running alone).
"""
from __future__ import annotations

import collections
import threading
import time


def jain(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    s = sum(xs)
    q = sum(x * x for x in xs)
    if q <= 0:
        return 1.0
    return (s * s) / (len(xs) * q)


def max_min_fair(demands: list[float], capacity: float) -> list[float]:
    """Max-min fair shares for the given demands and capacity (progressive filling)."""
    n = len(demands)
    share = [0.0] * n
    remaining = set(range(n))
    cap = capacity
    while remaining and cap > 1e-9:
        each = cap / len(remaining)
        done = [i for i in remaining if demands[i] - share[i] <= each]
        if not done:
            for i in remaining:
                share[i] += each
            break
        for i in done:
            cap -= demands[i] - share[i]
            share[i] = demands[i]
            remaining.discard(i)
    return share


def demand_normalised_jain(got: list[float], demand: list[float], capacity: float) -> float | None:
    if not got:
        return None
    fair = max_min_fair(demand, capacity)
    xs = [g / f if f > 0 else 1.0 for g, f in zip(got, fair)]
    return jain([min(1.5, x) for x in xs])


def harm(alone: float, together: float, more_is_better: bool = True) -> float:
    """Ware et al. harm in [0,1]: fractional degradation versus running alone."""
    if alone is None or together is None:
        return None
    if more_is_better:
        return max(0.0, (alone - together) / alone) if alone > 0 else 0.0
    return max(0.0, (together - alone) / together) if together > 0 else 0.0


class History:
    """Fixed-size time series ring for the dashboard (1 sample per policy tick)."""

    def __init__(self, seconds: int = 600, tick: float = 0.5):
        self.buf: collections.deque = collections.deque(maxlen=int(seconds / tick))
        self.lock = threading.Lock()

    def add(self, row: dict) -> None:
        with self.lock:
            self.buf.append(row)

    def since(self, t: float) -> list[dict]:
        with self.lock:
            return [r for r in self.buf if r["t"] > t]


class Events:
    def __init__(self, path: str | None, keep: int = 500):
        self.buf: collections.deque = collections.deque(maxlen=keep)
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0

    def emit(self, kind: str, msg: str, level: str = "info") -> None:
        import json
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), "kind": kind, "level": level, "msg": msg}
            self.buf.append(ev)
            if self.path:
                try:
                    with open(self.path, "a") as f:
                        f.write(json.dumps(ev) + "\n")
                except OSError:
                    pass

    def recent(self, n: int = 100) -> list[dict]:
        with self.lock:
            return list(self.buf)[-n:]


def prometheus(snap: dict) -> str:
    """Render the engine snapshot in Prometheus text exposition format."""
    out = []

    def g(name, help_, val, labels=None):
        if val is None:
            return
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        out.append(f"# HELP intentqos_{name} {help_}\n# TYPE intentqos_{name} gauge\nintentqos_{name}{lab} {val}")

    est = snap.get("estimator", {})
    for d in ("down", "up"):
        x = est.get(d, {})
        g(f"shaper_rate_kbit_{d}", f"current shaper rate ({d})", x.get("rate_kbit"))
        g(f"achieved_kbit_{d}", f"achieved throughput ({d})", x.get("achieved_kbit"))
        g(f"delay_delta_ms_{d}", f"delay rise above unloaded baseline ({d})", x.get("delay_delta_ms"))
        g(f"jitter_ms_{d}", f"probe delay jitter, RFC 3550 smoothing ({d})", x.get("jitter_ms"))
        g(f"shaper_loss_pct_{d}", f"packets dropped by the shaper, percent ({d})", snap.get("loss_pct", {}).get(d))
        g(f"realtime_loss_pct_{d}", f"packets dropped in the realtime classes (intent, voice, video), percent ({d})",
          snap.get("loss_rt_pct", {}).get(d))
    for d, tins in snap.get("tins", {}).items():
        for name, t in tins.items():
            g("tin_kbit", "per-class throughput", t.get("kbit"), {"dir": d, "tin": name})
            g("tin_backlog_bytes", "per-class queue backlog", t.get("backlog"), {"dir": d, "tin": name})
            g("tin_queue_delay_ms", "per-class queue delay estimate", t.get("queue_ms"), {"dir": d, "tin": name})
            g("tin_drops_total", "per-class drops", t.get("drops"), {"dir": d, "tin": name})
    g("fairness_jain", "demand-normalised Jain index across devices (down)", snap.get("fairness", {}).get("jain"))
    g("flows_active", "active flows", len(snap.get("flows", [])))
    s = snap.get("safety", {})
    g("rollbacks_total", "policy rollbacks", s.get("rollbacks"))
    g("shield_interventions_total", "safety-envelope corrections", s.get("shield_interventions"))
    g("capture_kernel_drops_total", "packets dropped by the capture socket", snap.get("capture", {}).get("kernel_drops"))
    c = snap.get("classifier", {}).get("stats", {})
    for k in ("ml", "rules", "abstain", "user", "drift_alarms"):
        g(f"classifier_{k}_total", f"classification decisions ({k})", c.get(k))
    g("intents_active", "active intent leases", sum(1 for i in snap.get("intents", []) if i.get("status") == "active"))
    return "\n".join(out) + "\n"
