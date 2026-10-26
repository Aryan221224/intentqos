"""Remote management on the router: the managed-configuration overlay, model swaps and usage metering.

The controller never edits the gateway's base configuration. It sends a *managed overlay*: a small
set of allow-listed keys (plan rates, floors, budgets, classifier mode...) that is validated here,
written to data/managed.json and applied on top of the base config at every start. Most keys apply
live; the rest say "restart required". Anything not on the allow-list is refused, so a compromised or
buggy controller cannot, for example, change interfaces or open the API to the internet.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time

# dotted key -> (kind, lo, hi, live)
MANAGED_KEYS: dict[str, tuple] = {
    "link.down_mbit": ("float", 0.5, 10000.0, True),
    "link.up_mbit": ("float", 0.2, 10000.0, True),
    "classifier.mode": ("enum", ("ml", "rules"), None, True),
    "policy.floor_fraction.voice": ("float", 0.0, 0.3, True),
    "policy.floor_fraction.video": ("float", 0.0, 0.3, True),
    "policy.floor_fraction.besteffort": ("float", 0.02, 0.5, True),
    "policy.floor_fraction.bulk": ("float", 0.01, 0.3, True),
    "policy.max_realtime_fraction": ("float", 0.2, 0.8, True),
    "policy.remainder_bulk_share": ("float", 0.1, 0.9, True),
    "policy.max_changes_per_minute": ("int", 10, 600, True),
    "policy.safe_fraction": ("float", 0.3, 1.0, True),
    "intents.daily_budget_min": ("float", 0.0, 1440.0, True),
    "intents.max_duration_s": ("float", 600.0, 86400.0, True),
    "intents.max_boost_fraction": ("float", 0.0, 0.8, True),
    "estimator.bloat_threshold_ms": ("float", 5.0, 100.0, True),
    "estimator.enabled": ("bool", None, None, False),
    "estimator.reflectors": ("iplist", 1, 8, False),
    "watchdog.timeout_s": ("float", 1.0, 30.0, False),
}


def validate(changes: dict) -> dict:
    """Return normalised values or raise ValueError listing every problem."""
    out, errs = {}, []
    for k, v in changes.items():
        spec = MANAGED_KEYS.get(k)
        if spec is None:
            errs.append(f"{k}: not a managed setting")
            continue
        kind, lo, hi, _live = spec
        try:
            if kind == "float":
                x = float(v)
                if not lo <= x <= hi:
                    raise ValueError(f"must be between {lo} and {hi}")
            elif kind == "int":
                x = int(v)
                if not lo <= x <= hi:
                    raise ValueError(f"must be between {lo} and {hi}")
            elif kind == "bool":
                if not isinstance(v, bool):
                    raise ValueError("must be true or false")
                x = v
            elif kind == "enum":
                if v not in lo:
                    raise ValueError(f"must be one of {', '.join(lo)}")
                x = v
            elif kind == "iplist":
                if not isinstance(v, list) or not lo <= len(v) <= hi:
                    raise ValueError(f"must be a list of {lo} to {hi} addresses")
                x = [str(ipaddress.ip_address(a)) for a in v]
            else:
                raise ValueError("unknown kind")
        except (TypeError, ValueError) as e:
            errs.append(f"{k}: {e}")
            continue
        out[k] = x
    if errs:
        raise ValueError("; ".join(errs))
    return out


def _set(cfg: dict, dotted: str, value) -> None:
    d = cfg
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


class Managed:
    """data/managed.json: {"version": n, "profile": "...", "set": {...}, "applied_at": t, "by": "..."}"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.doc = {"version": 0, "profile": None, "set": {}, "applied_at": None, "by": None}
        try:
            with open(path) as f:
                self.doc.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass

    def overlay(self, cfg: dict) -> list[str]:
        """Apply the stored overlay to a freshly loaded config (at start). Bad entries are skipped."""
        skipped = []
        for k, v in self.doc.get("set", {}).items():
            try:
                _set(cfg, k, validate({k: v})[k])
            except ValueError:
                skipped.append(k)
        floors = cfg["policy"]["floor_fraction"]
        if sum(floors.values()) > 0.6:
            skipped.append("policy.floor_fraction (sum > 0.6)")
        return skipped

    def save(self, changes: dict, version: int | None, profile: str | None, by: str) -> None:
        with self.lock:
            merged = dict(self.doc.get("set", {}))
            merged.update(changes)
            self.doc.update({"set": merged, "version": version if version is not None else self.doc.get("version", 0) + 1,
                             "profile": profile or self.doc.get("profile"), "applied_at": time.time(), "by": by})
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.doc, f, indent=1)
            os.replace(tmp, self.path)


def apply_live(eng, changes: dict, version: int | None = None, profile: str | None = None, by: str = "controller") -> dict:
    """Validate, apply what can be applied live, persist everything. Returns what happened."""
    vals = validate(changes)
    floors = dict(eng.cfg["policy"]["floor_fraction"])
    for k, v in vals.items():
        if k.startswith("policy.floor_fraction."):
            floors[k.rsplit(".", 1)[1]] = v
    if sum(floors.values()) > 0.6:
        raise ValueError("policy.floor_fraction: floors may not add up to more than 60% of the link")
    applied, restart = [], []
    for k, v in vals.items():
        live = MANAGED_KEYS[k][3]
        _set(eng.cfg, k, v)
        if not live:
            restart.append(k)
            continue
        if k in ("link.down_mbit", "link.up_mbit"):
            d = eng.estimator.down if k.endswith("down_mbit") else eng.estimator.up
            _retarget(eng, d, v * 1000.0)
        elif k == "classifier.mode":
            eng.classifier.set_mode(v)
        elif k == "intents.daily_budget_min":
            eng.intents.budget_min = v
        elif k == "intents.max_duration_s":
            eng.intents.max_duration = v
        elif k == "intents.max_boost_fraction":
            eng.intents.max_boost = v
        applied.append(k)
    eng.managed.save(vals, version, profile, by)
    eng.emit("config", f"managed config v{eng.managed.doc['version']} from {by}: "
                       f"{', '.join(f'{k}={vals[k]}' for k in vals)}"
                       + (f" (restart needed for {', '.join(restart)})" if restart else ""), "info")
    return {"ok": True, "version": eng.managed.doc["version"], "applied": applied, "restart_required": restart}


def _retarget(eng, d, nominal_kbit: float) -> None:
    """New plan rate: move the estimator's nominal, bounds and base, and clamp the current rate."""
    L = eng.cfg["link"]
    d.nominal = nominal_kbit
    d.min = max(L.get("min_kbit", 0), L["min_fraction"] * nominal_kbit)
    d.max = L.get(f"max_fraction_{d.name}", L["max_fraction"]) * nominal_kbit
    d.base = L["start_fraction"] * nominal_kbit
    d.rate = min(max(d.rate, d.min), d.max)
    d.bloat_rate = None


class UsageMeter:
    """Bytes per device per day (local time), from the closed flow windows. Kept 35 days."""

    def __init__(self, path: str, keep_days: int = 35):
        self.path, self.keep = path, keep_days
        self.lock = threading.Lock()
        self.days: dict[str, dict[str, list]] = {}
        self._saved = time.monotonic()
        try:
            with open(path) as f:
                self.days = json.load(f)
        except (FileNotFoundError, ValueError):
            pass

    def add_window(self, flows, wall: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(wall))
        with self.lock:
            d = self.days.setdefault(day, {})
            for f in flows:
                if not f.hist:
                    continue
                w = f.hist[-1]
                if w.by_up or w.by_dn:
                    a = d.setdefault(f.lan_ip, [0, 0])
                    a[0] += w.by_up
                    a[1] += w.by_dn
            if len(self.days) > self.keep:
                for k in sorted(self.days)[:-self.keep]:
                    del self.days[k]
        if time.monotonic() - self._saved > 60:
            self.save()

    def save(self) -> None:
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self.days, f)
                os.replace(tmp, self.path)
            except OSError:
                pass
            self._saved = time.monotonic()

    def summary(self, days: int = 30) -> dict:
        with self.lock:
            ks = sorted(self.days)[-days:]
            per_dev: dict[str, list] = {}
            for k in ks:
                for ip, (u, d) in self.days[k].items():
                    a = per_dev.setdefault(ip, [0, 0])
                    a[0] += u
                    a[1] += d
            return {"days": {k: {ip: v for ip, v in self.days[k].items()} for k in ks},
                    "devices": {ip: {"up_bytes": v[0], "down_bytes": v[1]} for ip, v in per_dev.items()},
                    "total": {"up_bytes": sum(v[0] for v in per_dev.values()), "down_bytes": sum(v[1] for v in per_dev.values())}}
