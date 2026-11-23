"""Quality of Outcome (QoO) scoring, after IETF draft-ietf-ippm-qoo.

An application's network requirement is a short list of thresholds: for some latency percentiles
(and packet loss, and optionally throughput) a value at which the app works perfectly (ROP,
"requirement for optimal performance") and a value at which it becomes unusable (CPUP, "critical
point of unusable performance"). The score of each requirement interpolates linearly between them:

    score_i = 100 * (CPUP_i - measured_i) / (CPUP_i - ROP_i), clamped to 0..100

and the app's QoO is the minimum over its requirements (the worst one decides what the user feels).
For throughput the scale is reversed (more is better).

The requirement values below are IntentQoS defaults for round-trip latency as the router sees it
(LAN + our queue + internet path). They are configurable ([qoo] in the config) and documented in
docs/INNOVATIONS.md with where each number comes from.
"""
from __future__ import annotations

# percentile -> (ROP ms, CPUP ms); loss (ROP %, CPUP %); throughput (ROP kbit/s, CPUP kbit/s), down
DEFAULT_REQUIREMENTS: dict[str, dict] = {
    "voip":      {"latency": {50: (60, 300), 99: (150, 500)}, "loss": (0.5, 5.0), "down_kbit": (100, 30)},
    "video":     {"latency": {50: (80, 350), 99: (200, 600)}, "loss": (1.0, 8.0), "down_kbit": (1500, 300)},
    "gaming":    {"latency": {50: (30, 150), 99: (60, 250)}, "loss": (0.5, 3.0)},
    "streaming": {"latency": {99: (800, 4000)}, "loss": (2.0, 10.0), "down_kbit": (5000, 1000)},
    "web":       {"latency": {50: (100, 600), 99: (300, 2000)}, "loss": (1.0, 5.0)},
    "bulk":      {"latency": {99: (1500, 8000)}, "down_kbit": (10000, 500)},
}


def requirements(cfg: dict | None = None) -> dict:
    """Default requirements merged with [qoo.<label>] overrides from the config."""
    out = {k: {kk: (dict(vv) if isinstance(vv, dict) else vv) for kk, vv in v.items()}
           for k, v in DEFAULT_REQUIREMENTS.items()}
    for label, over in ((cfg or {}).get("qoo") or {}).items():
        if not isinstance(over, dict):
            continue
        r = out.setdefault(label, {})
        for k, v in over.items():
            if k == "latency" and isinstance(v, dict):
                r["latency"] = {int(p): tuple(x) for p, x in v.items()}
            elif isinstance(v, (list, tuple)) and len(v) == 2:
                r[k] = tuple(v)
    return out


def interp(measured: float, rop: float, cpup: float) -> float:
    """100 at or better than ROP, 0 at or worse than CPUP, linear in between. Works for either direction."""
    if rop == cpup:
        return 100.0 if (measured <= rop if cpup >= rop else measured >= rop) else 0.0
    s = 100.0 * (cpup - measured) / (cpup - rop)
    return max(0.0, min(100.0, s))


def score(req: dict, latency_pct: dict[int, float] | None, loss_pct: float | None = None,
          down_kbit: float | None = None) -> dict:
    """QoO of one app: {"qoo": 0..100 or None, "parts": {...}, "worst": name}. Missing measurements are skipped."""
    parts: dict[str, float] = {}
    for p, (rop, cpup) in (req.get("latency") or {}).items():
        if latency_pct and latency_pct.get(p) is not None:
            parts[f"p{p}"] = interp(latency_pct[p], rop, cpup)
    if loss_pct is not None and req.get("loss"):
        parts["loss"] = interp(loss_pct, *req["loss"])
    if down_kbit is not None and req.get("down_kbit"):
        rop, cpup = req["down_kbit"]
        parts["throughput"] = interp(down_kbit, rop, cpup)
    if not parts:
        return {"qoo": None, "parts": {}, "worst": None}
    worst = min(parts, key=parts.get)
    return {"qoo": round(parts[worst], 1), "parts": {k: round(v, 1) for k, v in parts.items()}, "worst": worst}


def grade(q: float | None) -> str:
    if q is None:
        return "unknown"
    return "great" if q >= 80 else "good" if q >= 60 else "fair" if q >= 40 else "poor" if q > 0 else "unusable"
