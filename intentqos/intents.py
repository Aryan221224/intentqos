"""Module 6 -- Temporary intents as *leases* (PANE-style resource contracts).

An intent such as "prioritise my work call for 45 minutes" is not a toggle. It is
a lease with:
  * a scope   -- target device (LAN IP) and goal (low_latency | throughput),
  * a window  -- start/expiry, capped by max_duration_s (auto-expires, the
                 watchdog restores the safe state even if the engine dies),
  * a demand  -- kbit/s down/up it needs (measured or declared),
  * a budget  -- boost-minutes per requester per day (prevents permanent "priority"),
  * admission -- all intents together may reserve at most max_boost_fraction of
                 the link, so best-effort and bulk always keep a floor.

When concurrent intents oversubscribe the boost budget they are resolved by
weighted Nash bargaining: maximise sum_i w_i * log(a_i) subject to sum a_i <= C,
a_i <= d_i (w = priority). The solution is the water-filling allocation
a_i = min(d_i, lambda * w_i) -- deterministic, explainable, and every grant is logged.
Conflicts are detected by time-window AND scope overlap (two leases in different
time windows never conflict).

Inputs : REST requests; link capacity each tick; per-device measurements.
Outputs: active leases with granted kbit/s and an assurance status
         (satisfied / degraded / infeasible) closed-loop checked every tick.
Failure: invalid/over-budget/infeasible requests are rejected with a reason and
         the earliest time they could be granted. Leases persist across restarts.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field

GOALS = ("low_latency", "throughput")


@dataclass
class Intent:
    id: str
    requester: str
    target: str                       # LAN IP of the device
    goal: str = "low_latency"
    priority: int = 2                 # 1 (low) .. 3 (high)  -- the bargaining weight
    latency_ms: float = 100.0         # goal for low_latency intents (one-way, ms)
    demand_down_kbit: float = 3000.0
    demand_up_kbit: float = 2000.0
    start: float = 0.0                # wall-clock seconds
    expires: float = 0.0
    created: float = 0.0
    status: str = "active"            # scheduled | active | expired | cancelled
    label: str = ""
    granted_down_kbit: float = 0.0
    granted_up_kbit: float = 0.0
    assurance: str = "pending"        # pending | satisfied | degraded | infeasible
    assurance_detail: str = ""
    history: list = field(default_factory=list)

    def window_overlaps(self, other: "Intent") -> bool:
        return self.start < other.expires and other.start < self.expires


def water_fill(demands: list[float], weights: list[float], capacity: float) -> list[float]:
    """Weighted proportional-fair (Nash bargaining) allocation with caps at demand."""
    n = len(demands)
    alloc = [0.0] * n
    if n == 0 or capacity <= 0:
        return alloc
    if sum(demands) <= capacity:
        return list(demands)
    active = set(range(n))
    cap = capacity
    while active:
        wsum = sum(weights[i] for i in active)
        lam = cap / wsum if wsum > 0 else 0.0
        capped = [i for i in active if demands[i] <= lam * weights[i]]
        if not capped:
            for i in active:
                alloc[i] = lam * weights[i]
            break
        for i in capped:
            alloc[i] = demands[i]
            cap -= demands[i]
            active.discard(i)
    return alloc


class IntentManager:
    def __init__(self, cfg: dict, resolve, emit):
        c = cfg["intents"]
        self.max_duration = float(c["max_duration_s"])
        self.default_duration = float(c["default_duration_s"])
        self.budget_min = float(c["daily_budget_min"])
        self.max_boost = float(c["max_boost_fraction"])
        self.default_demand = c["default_demand_kbit"]
        self.path = resolve(c["store_path"])
        self.emit = emit
        self.lock = threading.Lock()
        self.intents: dict[str, Intent] = {}
        self.usage: dict[str, list] = {}   # requester -> [(day, minutes)]
        self._load()

    # ---------------------------------------------------------------- persistence
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            for x in d.get("intents", []):
                it = Intent(**x)
                self.intents[it.id] = it
            self.usage = d.get("usage", {})
        except (FileNotFoundError, ValueError, TypeError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        keep = [asdict(i) for i in self.intents.values() if i.status in ("active", "scheduled")
                or time.time() - i.expires < 86400]
        with open(tmp, "w") as f:
            json.dump({"intents": keep, "usage": self.usage}, f, indent=1)
        os.replace(tmp, self.path)

    # ---------------------------------------------------------------- budget
    @staticmethod
    def _day(t: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(t))

    def used_minutes(self, requester: str, now: float) -> float:
        day = self._day(now)
        return sum(m for d, m in self.usage.get(requester, []) if d == day)

    # ---------------------------------------------------------------- admission
    def _overlapping(self, cand: Intent) -> list[Intent]:
        return [i for i in self.intents.values() if i.status in ("active", "scheduled") and i.window_overlaps(cand)]

    def plan_grants(self, intents: list[Intent], cap_down: float, cap_up: float) -> dict[str, tuple[float, float]]:
        ws = [float(i.priority) for i in intents]
        gd = water_fill([i.demand_down_kbit for i in intents], ws, cap_down)
        gu = water_fill([i.demand_up_kbit for i in intents], ws, cap_up)
        return {i.id: (gd[k], gu[k]) for k, i in enumerate(intents)}

    def request(self, req: dict, rate_down_kbit: float, rate_up_kbit: float, now: float | None = None,
                dry_run: bool = False) -> dict:
        now = time.time() if now is None else now
        target = str(req.get("target", "")).strip()
        if not target:
            return {"ok": False, "reason": "target (device IP) is required"}
        goal = req.get("goal", "low_latency")
        if goal not in GOALS:
            return {"ok": False, "reason": f"goal must be one of {GOALS}"}
        dur = float(req.get("duration_s", self.default_duration))
        if dur <= 0:
            return {"ok": False, "reason": "duration must be positive"}
        if dur > self.max_duration:
            return {"ok": False, "reason": f"duration {dur:.0f}s exceeds the maximum lease of {self.max_duration:.0f}s"}
        prio = int(req.get("priority", 2))
        if not 1 <= prio <= 3:
            return {"ok": False, "reason": "priority must be 1, 2 or 3"}
        requester = str(req.get("requester", "household"))[:64]
        start = float(req.get("start", now))
        cand = Intent(
            id=os.urandom(5).hex(), requester=requester, target=target, goal=goal, priority=prio,
            latency_ms=float(req.get("latency_ms", 100.0)),
            demand_down_kbit=float(req.get("demand_down_kbit", self.default_demand["down"])),
            demand_up_kbit=float(req.get("demand_up_kbit", self.default_demand["up"])),
            start=start, expires=start + dur, created=now, label=str(req.get("label", ""))[:80],
            status="active" if start <= now else "scheduled")
        used = self.used_minutes(requester, now)
        if used + dur / 60.0 > self.budget_min:
            return {"ok": False, "reason": f"daily boost budget exceeded for '{requester}' "
                                           f"({used:.0f}/{self.budget_min:.0f} min used)"}
        with self.lock:
            others = self._overlapping(cand)
            cap_d, cap_u = rate_down_kbit * self.max_boost, rate_up_kbit * self.max_boost
            grants = self.plan_grants(others + [cand], cap_d, cap_u)
            gd, gu = grants[cand.id]
            frac = min(gd / cand.demand_down_kbit if cand.demand_down_kbit else 1.0,
                       gu / cand.demand_up_kbit if cand.demand_up_kbit else 1.0)
            conflicts = [o.id for o in others if o.target == cand.target or
                         grants[o.id][0] < o.demand_down_kbit - 1 or grants[o.id][1] < o.demand_up_kbit - 1]
            preview = {
                "candidate": {"id": cand.id, "granted_down_kbit": round(gd), "granted_up_kbit": round(gu),
                              "fraction_of_demand": round(frac, 3)},
                "others": {o.id: {"target": o.target, "granted_down_kbit": round(grants[o.id][0]),
                                  "granted_up_kbit": round(grants[o.id][1])} for o in others},
                "conflicts": conflicts,
                "boost_capacity_kbit": {"down": round(cap_d), "up": round(cap_u)},
            }
            if frac < 0.5:
                earliest = min((o.expires for o in others), default=now)
                return {"ok": False, "reason": "not enough boost capacity in this window "
                                               f"(only {frac * 100:.0f}% of the demand could be reserved)",
                        "earliest_full_grant": earliest, "preview": preview}
            if dry_run:
                return {"ok": True, "dry_run": True, "preview": preview}
            cand.granted_down_kbit, cand.granted_up_kbit = gd, gu
            self.intents[cand.id] = cand
            self.usage.setdefault(requester, []).append([self._day(now), dur / 60.0])
            self._save()
        self.emit("intent", f"lease {cand.id} for {target} ({goal}, prio {prio}, {dur / 60:.0f} min) granted "
                            f"{gd / 1000:.1f}/{gu / 1000:.1f} Mbit/s"
                            + (f"; renegotiated {len(conflicts)} overlapping lease(s)" if conflicts else ""), "info")
        return {"ok": True, "intent": asdict(cand), "preview": preview}

    def cancel(self, iid: str) -> bool:
        with self.lock:
            it = self.intents.get(iid)
            if not it or it.status not in ("active", "scheduled"):
                return False
            it.status = "cancelled"
            self._save()
        self.emit("intent", f"lease {iid} cancelled", "info")
        return True

    # ---------------------------------------------------------------- per tick
    def tick(self, now: float, rate_down_kbit: float, rate_up_kbit: float) -> list[Intent]:
        """Activate/expire leases and re-run bargaining against the current capacity."""
        changed = False
        with self.lock:
            for it in self.intents.values():
                if it.status == "scheduled" and it.start <= now:
                    it.status = "active"
                    changed = True
                    self.emit("intent", f"lease {it.id} for {it.target} started", "info")
                if it.status == "active" and now >= it.expires:
                    it.status = "expired"
                    changed = True
                    self.emit("intent", f"lease {it.id} for {it.target} expired -- policy reverts automatically",
                              "info")
            act = [i for i in self.intents.values() if i.status == "active"]
            if act:
                g = self.plan_grants(act, rate_down_kbit * self.max_boost, rate_up_kbit * self.max_boost)
                for i in act:
                    i.granted_down_kbit, i.granted_up_kbit = g[i.id]
            if changed:
                self._save()
            return act

    def assure(self, it: Intent, measured: dict) -> None:
        """Closed-loop check of an active lease against measurements of its device."""
        est_lat = measured.get("est_latency_ms")
        got_d = measured.get("down_kbit", 0.0)
        frac = it.granted_down_kbit / it.demand_down_kbit if it.demand_down_kbit else 1.0
        old = it.assurance
        if frac < 0.5:
            it.assurance, it.assurance_detail = "infeasible", \
                f"link capacity allows only {frac * 100:.0f}% of the declared demand"
        elif it.goal == "low_latency" and est_lat is not None and est_lat > it.latency_ms:
            it.assurance, it.assurance_detail = "degraded", f"estimated latency {est_lat:.0f} ms > goal {it.latency_ms:.0f} ms"
        elif not measured.get("flows"):
            it.assurance, it.assurance_detail = "pending", "no traffic from this device yet"
        else:
            detail = f"est. latency {est_lat:.0f} ms" if est_lat is not None else "reservation in place"
            it.assurance, it.assurance_detail = "satisfied", f"{detail}; {got_d / 1000:.1f} Mbit/s down"
        if old != it.assurance:
            it.history.append([round(time.time(), 1), it.assurance])
            it.history = it.history[-20:]
            if it.assurance in ("degraded", "infeasible"):
                self.emit("assurance", f"lease {it.id} ({it.target}) {it.assurance}: {it.assurance_detail}", "warn")

    def snapshot(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self.lock:
            out = []
            for i in sorted(self.intents.values(), key=lambda x: -x.created):
                if i.status in ("active", "scheduled") or now - i.expires < 3600:
                    d = asdict(i)
                    d["remaining_s"] = max(0.0, i.expires - now)
                    out.append(d)
            return out[:50]
