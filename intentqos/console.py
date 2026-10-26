"""Console services: device registry, modes, schedules, health grade, system info, setup check.

These are the product-level features the console (web UI) needs on top of the control loop.
None of them changes how traffic is shaped except through the existing intent leases.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from . import __version__

# --------------------------------------------------------------------------- grade
# Waveform-style letter grade from the delay ADDED under load (A+ < 5 ms ... F >= 400 ms).
GRADES = [(5, "A+"), (30, "A"), (60, "B"), (200, "C"), (400, "D"), (float("inf"), "F")]
GRADE_TEXT = {
    "A+": "Excellent: nothing you do will feel laggy.",
    "A": "Smooth for calls, games and streaming.",
    "B": "Fine for most things; competitive games may notice.",
    "C": "Calls may stutter while something big downloads.",
    "D": "Noticeable lag under load.",
    "F": "Severe lag under load.",
}


def grade(added_ms: float) -> str:
    for lim, g in GRADES:
        if added_ms < lim:
            return g
    return "F"


class GradeTracker:
    """Grade from the 95th percentile of added delay over the last minute, so one spike does not
    flip the grade and a sustained problem is not hidden by an instant reading."""

    def __init__(self, window_s: float = 60.0):
        self.window = window_s
        self.samples: dict[str, list] = {"down": [], "up": []}

    def add(self, now: float, down_ms: float, up_ms: float) -> None:
        for d, v in (("down", down_ms), ("up", up_ms)):
            s = self.samples[d]
            s.append((now, max(0.0, v)))
            while s and now - s[0][0] > self.window:
                s.pop(0)

    def summary(self) -> dict:
        out = {}
        for d, s in self.samples.items():
            vals = sorted(v for _, v in s)
            p95 = vals[int(0.95 * (len(vals) - 1))] if vals else 0.0
            g = grade(p95)
            out[d] = {"p95_added_ms": round(p95, 1), "grade": g}
        worst = max(out.values(), key=lambda x: [g for _, g in GRADES].index(x["grade"]))
        out["overall"] = {"grade": worst["grade"], "text": GRADE_TEXT[worst["grade"]]}
        return out


# --------------------------------------------------------------------------- devices
DEVICE_TYPES = ("laptop", "phone", "tv", "console", "desktop", "tablet", "speaker", "camera", "nas", "other")


class DeviceRegistry:
    """Friendly names and types for LAN devices (config [devices] + edits from the console)."""

    def __init__(self, path: str, config_names: dict):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        for name, ip in config_names.items():
            self.data[ip] = {"name": name, "type": _guess_type(name)}
        try:
            with open(path) as f:
                self.data.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass

    def get(self, ip: str) -> dict:
        return self.data.get(ip, {"name": "", "type": "other"})

    def set(self, ip: str, name: str | None, typ: str | None) -> dict:
        with self.lock:
            d = dict(self.get(ip))
            if name is not None:
                d["name"] = str(name).strip()[:40]
            if typ is not None:
                if typ not in DEVICE_TYPES:
                    raise ValueError(f"type must be one of {DEVICE_TYPES}")
                d["type"] = typ
            self.data[ip] = d
            self._save()
            return d

    def update(self, ip: str, **fields) -> dict:
        with self.lock:
            d = dict(self.get(ip))
            for k, v in fields.items():
                if v is None:
                    d.pop(k, None)
                else:
                    d[k] = v
            self.data[ip] = d
            self._save()
            return d

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def names(self) -> dict:
        return {ip: d.get("name", "") for ip, d in self.data.items()}


def _guess_type(name: str) -> str:
    n = name.lower()
    for t in DEVICE_TYPES:
        if t in n:
            return t
    if n.startswith("tv"):
        return "tv"
    return "other"


ACTIVITY = {"voip": "On a voice call", "gaming": "Gaming", "video": "On a video call", "streaming": "Streaming video",
            "web": "Browsing", "bulk": "Downloading or backing up"}


def activity(labels: dict) -> str:
    """Plain-language activity from a device's flow labels (most important first)."""
    for lab in ("video", "voip", "gaming", "streaming", "bulk", "web"):
        if labels.get(lab):
            return ACTIVITY[lab]
    return "Idle"


# --------------------------------------------------------------------------- modes
MODES = [
    {"id": "work-call", "name": "Work call", "goal": "low_latency", "priority": 3, "duration_s": 3600,
     "demand_down_kbit": 3000, "demand_up_kbit": 2000, "needs_device": True,
     "text": "Keeps one device's calls smooth, even while others download."},
    {"id": "gaming", "name": "Gaming", "goal": "low_latency", "priority": 2, "duration_s": 7200,
     "demand_down_kbit": 1500, "demand_up_kbit": 800, "needs_device": True,
     "text": "Lowest delay for a console or gaming PC."},
    {"id": "movie-night", "name": "Movie night", "goal": "throughput", "priority": 2, "duration_s": 3 * 3600,
     "demand_down_kbit": 16000, "demand_up_kbit": 300, "needs_device": True,
     "text": "Reserves enough for a 4K stream on one TV."},
    {"id": "big-upload", "name": "Finish an upload", "goal": "throughput", "priority": 1, "duration_s": 1800,
     "demand_down_kbit": 500, "demand_up_kbit": 6000, "needs_device": True,
     "text": "Gives one device's upload a guaranteed share for a while."},
]


# --------------------------------------------------------------------------- schedules
class Scheduler:
    """Recurring intents ("weekdays 09:00-17:30, work call on the laptop") that create leases."""

    def __init__(self, path: str, intents, emit):
        self.path = path
        self.intents = intents
        self.emit = emit
        self.lock = threading.Lock()
        self.items: list[dict] = []
        self.started: dict[str, str] = {}    # schedule id -> "YYYY-MM-DD" it last started
        try:
            with open(path) as f:
                d = json.load(f)
                self.items = d.get("items", [])
                self.started = d.get("started", {})
        except (FileNotFoundError, ValueError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"items": self.items, "started": self.started}, f, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        v = int(h) * 60 + int(m)
        if not 0 <= v < 24 * 60:
            raise ValueError("time must be HH:MM")
        return v

    def add(self, req: dict) -> dict:
        days = [int(x) for x in req.get("days", [0, 1, 2, 3, 4])]
        if not days or any(not 0 <= d <= 6 for d in days):
            raise ValueError("days must be 0 (Mon) .. 6 (Sun)")
        start, end = self._minutes(req.get("start", "09:00")), self._minutes(req.get("end", "17:00"))
        if end <= start:
            raise ValueError("end must be after start (same day)")
        if (end - start) * 60 > self.intents.max_duration:
            raise ValueError("a scheduled window cannot be longer than the maximum lease")
        if not req.get("target"):
            raise ValueError("target device is required")
        item = {"id": os.urandom(4).hex(), "target": req["target"], "days": days,
                "start": req.get("start", "09:00"), "end": req.get("end", "17:00"),
                "goal": req.get("goal", "low_latency"), "priority": int(req.get("priority", 2)),
                "label": str(req.get("label", ""))[:60], "requester": str(req.get("requester", "schedule"))[:40],
                "enabled": True}
        with self.lock:
            self.items.append(item)
            self._save()
        self.emit("schedule", f"schedule {item['id']} added: {item['label'] or item['goal']} for {item['target']} "
                              f"on days {days} {item['start']}-{item['end']}", "info")
        return item

    def remove(self, sid: str) -> bool:
        with self.lock:
            n = len(self.items)
            self.items = [i for i in self.items if i["id"] != sid]
            self._save()
            return len(self.items) < n

    def tick(self, now_wall: float, rate_down: float, rate_up: float) -> None:
        lt = time.localtime(now_wall)
        today = time.strftime("%Y-%m-%d", lt)
        mins = lt.tm_hour * 60 + lt.tm_min
        for it in list(self.items):
            if not it.get("enabled") or lt.tm_wday not in it["days"] or self.started.get(it["id"]) == today:
                continue
            s, e = self._minutes(it["start"]), self._minutes(it["end"])
            if s <= mins < e:
                res = self.intents.request({"target": it["target"], "goal": it["goal"], "priority": it["priority"],
                                            "duration_s": (e - mins) * 60, "label": it["label"] or "scheduled",
                                            "requester": it["requester"]}, rate_down, rate_up, now=now_wall)
                self.started[it["id"]] = today
                self._save()
                if not res.get("ok"):
                    self.emit("schedule", f"schedule {it['id']} could not start a lease: {res.get('reason')}", "warn")


# --------------------------------------------------------------------------- system
class ProcStats:
    def __init__(self):
        self.t = time.monotonic()
        self.cpu = sum(os.times()[:2])
        self.pct = 0.0

    def sample(self) -> dict:
        now, cpu = time.monotonic(), sum(os.times()[:2])
        if now - self.t >= 1.0:
            self.pct = (cpu - self.cpu) / (now - self.t) * 100.0
            self.t, self.cpu = now, cpu
        rss = 0.0
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) / 1024.0
        except OSError:
            pass
        return {"cpu_pct": round(self.pct, 1), "rss_mb": round(rss, 1), "threads": threading.active_count()}


def _cmd(args: list[str], limit: int = 20000) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=3)
        return (r.stdout or r.stderr)[:limit]
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"({e})"


_VERSIONS: dict | None = None


def versions() -> dict:
    global _VERSIONS
    if _VERSIONS is None:
        _VERSIONS = {"intentqos": __version__, "kernel": os.uname().release,
                     "tc": _cmd(["tc", "-V"]).strip(), "nft": _cmd(["nft", "-v"]).strip(),
                     "python": ".".join(map(str, os.sys.version_info[:3]))}
    return _VERSIONS


def inspect(eng) -> dict:
    """The live kernel state the engine manages -- shown verbatim so anyone can audit it."""
    out = {}
    for d, dev in (("down", eng.lan), ("up", eng.wan)):
        out[d] = {"dev": dev, "qdisc": _cmd(["tc", "-s", "qdisc", "show", "dev", dev]),
                  "class": _cmd(["tc", "-s", "class", "show", "dev", dev])}
    out["nft"] = _cmd(["nft", "list", "table", "inet", eng.cfg["enforce"]["nft_table"]], 40000)
    return out


def watchdog_status(path: str) -> dict:
    try:
        with open(path) as f:
            d = json.load(f)
        d["age_s"] = round(time.time() - d.get("ts", 0), 1)
        d["alive"] = d["age_s"] < 5
        return d
    except (FileNotFoundError, ValueError):
        return {"alive": False, "detail": "not running (start: python3 -m intentqos.watchdog)"}


def setup_check(eng) -> list[dict]:
    """The setup guide's verification steps, run live."""
    snap = eng.snapshot or {}
    cap = eng.capture.stats()
    checks = []

    def add(name, ok, detail, cmd=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "command": cmd})

    for d, dev in (("download", eng.lan), ("upload", eng.wan)):
        q = _cmd(["tc", "qdisc", "show", "dev", dev, "root"])
        add(f"{d} shaper installed on {dev}", ("htb" in q or "cake" in q) and "1:" in q, q.strip()[:160],
            f"tc qdisc show dev {dev} root")
    nft = _cmd(["nft", "list", "table", "inet", eng.cfg["enforce"]["nft_table"]])
    add("nftables flow-class table present", "map cls4" in nft, f"{nft.count('timeout')} marked flow entries",
        f"nft list table inet {eng.cfg['enforce']['nft_table']}")
    add("capture is receiving packets", cap["packets"] > 0, f"{cap['packets']} packets, {cap['kernel_drops']} "
        f"kernel drops, {cap['flows']} flows, {cap.get('backend', 'python')}",
        ("iq-capture (TPACKET_V3 ring) on " if "helper" in str(cap.get("backend", "")) else "AF_PACKET on ") + eng.lan)
    est = snap.get("estimator", {})
    add("delay reflectors answering", est.get("enabled") is False or (est.get("samples", 0) > 0 and not est.get("blind")),
        ", ".join(f"{r}: {v['recv']}/{v['sent']} ({v['mode']})" for r, v in est.get("reflectors", {}).items())
        or "estimator disabled", "ICMP timestamp / echo")
    cl = eng.classifier.info()
    add("classifier model loaded", cl["model_loaded"] or cl["mode"] == "rules",
        (f"release {(cl.get('model_meta') or {}).get('release') or '?'}, {cl.get('model_runtime') or ''} runtime"
         if cl["model_loaded"] else (cl["model_error"] or "rules mode")),
        os.path.relpath(cl["model_path"], eng.cfg.get("_root", "/")) if cl.get("model_path") else eng.cfg["classifier"]["model_path"])
    wd = watchdog_status(eng.wd_path)
    add("watchdog running", wd.get("alive"), f"last heartbeat {wd.get('age_s', '?')} s ago" if "age_s" in wd
        else wd.get("detail", ""), "python3 -m intentqos.watchdog")
    s = snap.get("safety", {})
    add("no failed policy applies", s.get("apply_failures", 0) == 0, f"{s.get('apply_failures', 0)} failures, "
        f"{s.get('rollbacks', 0)} rollbacks", "engine log")
    hb_age = time.time() - snap.get("t", 0) if snap else 99
    add("engine loop alive", hb_age < 3, f"last tick {hb_age:.1f} s ago", "GET /api/healthz")
    return checks
