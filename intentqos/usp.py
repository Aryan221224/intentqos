"""IntentQoS as a TR-181 vendor extension: the data model a USP (TR-369) controller would see.

ISPs manage home gateways with TR-069 or its successor USP (TR-369), over the Broadband Forum's TR-181
device data model. To fit that world, every number the engine reports and every switch the operator can
set is given a TR-181-style path under a vendor prefix (`Device.X_IntentQoS_`), with the TR-181
conventions for names (CamelCase objects, numbered multi-instance tables). `data_model(eng)` returns the
live values as {path: value}; `SCHEMA` lists each parameter's type, access and meaning, which is what a
USP agent's data-model definition needs.

What is implemented: the mapping and a JSON read-out (GET /api/usp on the router, relayed to the
controller). What is not yet: registering this model with a USP agent such as the Broadband Forum's
open-source OB-USP-Agent (as a "USP Service" over its UNIX-domain socket, TR-369 1.3); the engine's own
agent (agent.py) still uses its HTTPS protocol, documented as mapping onto USP messages.
"""
from __future__ import annotations

P = "Device.X_IntentQoS_"

# path suffix -> (type, access, description)
SCHEMA: dict[str, tuple[str, str, str]] = {
    "Enable": ("boolean", "readWrite", "Adaptive QoS on (false = the safe baseline, automation paused)"),
    "Version": ("string", "readOnly", "Engine version"),
    "Backend": ("string", "readOnly", "Shaper: cake, htb"),
    "L4SMode": ("string", "readOnly", "How ECT(1) traffic is handled"),
    "Link.DownstreamRate": ("unsignedInt", "readOnly", "Current shaped downstream rate, kbit/s"),
    "Link.UpstreamRate": ("unsignedInt", "readOnly", "Current shaped upstream rate, kbit/s"),
    "Link.EstimatorState": ("string", "readOnly", "idle, low, high, bloat, probe, rebaseline, blind, static"),
    "Link.PathRTTMedian": ("decimal", "readOnly", "Internet path RTT, median, ms (passive + probes)"),
    "Outcome.Objective": ("decimal", "readOnly", "Weighted Quality of Outcome of live apps, 0-100"),
    "Outcome.WorstProtected": ("decimal", "readOnly", "Lowest QoO among calls, games and leased devices, 0-100"),
    "Outcome.GatedChanges": ("unsignedInt", "readOnly", "Plans refused by the outcome gate"),
    "Outcome.Enable": ("boolean", "readWrite", "Choose policy parameters by predicted outcome"),
    "AppNumberOfEntries": ("unsignedInt", "readOnly", "Number of App entries"),
    "App.{i}.Device": ("string", "readOnly", "Device name or address"),
    "App.{i}.Class": ("string", "readOnly", "voip, video, gaming, streaming, web, bulk"),
    "App.{i}.QoO": ("decimal", "readOnly", "Quality of Outcome, 0-100"),
    "App.{i}.LatencyP99": ("decimal", "readOnly", "Round-trip latency 99th percentile, ms"),
    "App.{i}.Bottleneck": ("string", "readOnly", "wifi, line, none, idle"),
    "Lease.ActiveNumberOfEntries": ("unsignedInt", "readOnly", "Active leases (boosts)"),
    "Lease.DailyBudgetMinutes": ("unsignedInt", "readWrite", "Boost minutes per requester per day"),
    "Safety.Rollbacks": ("unsignedInt", "readOnly", "Transactional rollbacks since start"),
    "Safety.ShieldInterventions": ("unsignedInt", "readOnly", "Shield corrections since start"),
}


def _pct(d: dict, p: int):
    """Percentile maps use int keys in the engine and string keys after a JSON round trip."""
    v = d.get(p)
    return v if v is not None else d.get(str(p))


def data_model(eng) -> dict:
    s = eng.snapshot or {}
    est = s.get("estimator") or {}
    out = {
        P + "Enable": not s.get("paused", False),
        P + "Version": s.get("version"),
        P + "Backend": (s.get("config") or {}).get("backend"),
        P + "L4SMode": (s.get("l4s") or {}).get("mode"),
        P + "Link.DownstreamRate": (est.get("down") or {}).get("rate_kbit"),
        P + "Link.UpstreamRate": (est.get("up") or {}).get("rate_kbit"),
        P + "Link.EstimatorState": (est.get("down") or {}).get("state"),
        P + "Link.PathRTTMedian": _pct((((s.get("experience") or {}).get("path") or {}).get("rtt_ms") or {}), 50),
        P + "Outcome.Objective": (s.get("outcome") or {}).get("objective"),
        P + "Outcome.WorstProtected": (s.get("outcome") or {}).get("worst_protected"),
        P + "Outcome.GatedChanges": (s.get("outcome") or {}).get("gated"),
        P + "Outcome.Enable": (s.get("outcome") or {}).get("enabled"),
        P + "Lease.ActiveNumberOfEntries": sum(1 for i in s.get("intents", []) if i.get("status") == "active"),
        P + "Lease.DailyBudgetMinutes": (s.get("budget") or {}).get("daily_min"),
        P + "Safety.Rollbacks": (s.get("safety") or {}).get("rollbacks"),
        P + "Safety.ShieldInterventions": (s.get("safety") or {}).get("shield_interventions"),
    }
    apps = (s.get("experience") or {}).get("apps") or []
    out[P + "AppNumberOfEntries"] = len(apps)
    for i, a in enumerate(apps, 1):
        out[f"{P}App.{i}.Device"] = a.get("device") or a.get("ip")
        out[f"{P}App.{i}.Class"] = a.get("label")
        out[f"{P}App.{i}.QoO"] = a.get("qoo")
        out[f"{P}App.{i}.LatencyP99"] = _pct(a.get("latency_ms") or {}, 99)
        out[f"{P}App.{i}.Bottleneck"] = (a.get("bottleneck") or {}).get("where")
    return {"values": out, "schema": {P + k: {"type": t, "access": acc, "description": d}
                                      for k, (t, acc, d) in SCHEMA.items()}}
