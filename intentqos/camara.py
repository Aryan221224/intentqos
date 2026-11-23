"""CAMARA-shaped Quality-on-Demand API for the home.

CAMARA (Linux Foundation) Quality on Demand is the API mobile operators expose through GSMA Open
Gateway: an app asks for a QoS session for a device, with a named QoS profile and a duration. IntentQoS
offers the same resource shapes inside the home, so an app written for Open Gateway can ask the home
router (and, through the operator's controller, the access network) in the same words:

    POST   /camara/quality-on-demand/v1/sessions            create a session
    GET    /camara/quality-on-demand/v1/sessions/{id}       read it
    POST   /camara/quality-on-demand/v1/sessions/{id}/extend   {"requestedAdditionalDuration": s}
    DELETE /camara/quality-on-demand/v1/sessions/{id}       end it
    POST   /camara/quality-on-demand/v1/retrieve-sessions   {"device": {...}}
    GET    /camara/qos-profiles/v1/qos-profiles             the profiles this home offers

A session is carried out as an IntentQoS lease (intents.py): the same budget, admission control and
bargaining apply, and qosStatus follows the lease's assurance (AVAILABLE when satisfied). Field names
follow the CAMARA specifications (QoD v1, QoS Profiles v1); only the subset a home needs is
implemented, and the device is identified by its private IPv4/IPv6 address inside the home.
"""
from __future__ import annotations

import time

# Profiles in CAMARA QoS Profiles shape. Names follow the common QOS_E/S/M/L convention; the numbers are
# what this home engine can promise (a lease's demand and latency goal), not operator-network values.
PROFILES = {
    "QOS_E": {"description": "Low latency for calls and games", "goal": "low_latency", "latency_ms": 50,
              "down_kbit": 3000, "up_kbit": 2000},
    "QOS_S": {"description": "Small guaranteed throughput (5 Mbit/s down)", "goal": "throughput", "latency_ms": 150,
              "down_kbit": 5000, "up_kbit": 1000},
    "QOS_M": {"description": "Medium guaranteed throughput (15 Mbit/s down)", "goal": "throughput", "latency_ms": 150,
              "down_kbit": 15000, "up_kbit": 3000},
    "QOS_L": {"description": "Large guaranteed throughput (40 Mbit/s down)", "goal": "throughput", "latency_ms": 200,
              "down_kbit": 40000, "up_kbit": 5000},
}


def profile_doc(name: str) -> dict:
    p = PROFILES[name]
    return {"name": name, "description": p["description"], "status": "ACTIVE",
            "targetMinDownstreamRate": {"value": p["down_kbit"], "unit": "kbps"},
            "targetMinUpstreamRate": {"value": p["up_kbit"], "unit": "kbps"},
            "maxLatency": {"value": p["latency_ms"], "unit": "Milliseconds"},
            "maxDuration": {"value": 8, "unit": "Hours"}, "minDuration": {"value": 60, "unit": "Seconds"}}


class CamaraError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"status": self.status, "code": self.code, "message": self.message}


def device_ip(device: dict) -> str:
    """The home-side address of the device: ipv4Address.privateAddress, or ipv6Address."""
    if not isinstance(device, dict):
        raise CamaraError(400, "INVALID_ARGUMENT", "device is required")
    v4 = device.get("ipv4Address")
    if isinstance(v4, dict) and v4.get("privateAddress"):
        return str(v4["privateAddress"])
    if isinstance(v4, str):
        return v4
    if device.get("ipv6Address"):
        return str(device["ipv6Address"]).split("/")[0]
    raise CamaraError(422, "UNIDENTIFIABLE_DEVICE", "identify the device by ipv4Address.privateAddress or ipv6Address")


class QoDSessions:
    """CAMARA sessions on top of the engine's lease manager."""

    def __init__(self, eng):
        self.eng = eng
        self.sessions: dict[str, dict] = {}       # sessionId -> {"lease": id, "req": request, ...}

    def _status(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            raise CamaraError(404, "NOT_FOUND", "the session does not exist")
        it = self.eng.intents.intents.get(s["lease"])
        now = time.time()
        if it is None or it.status in ("expired", "cancelled"):
            qs, info = "UNAVAILABLE", ("DURATION_EXPIRED" if it is not None and it.status == "expired"
                                       else "DELETE_REQUESTED")
        elif it.status == "scheduled":
            qs, info = "REQUESTED", None
        elif it.assurance in ("satisfied", "pending"):
            qs, info = "AVAILABLE", None
        else:
            qs, info = "UNAVAILABLE", "NETWORK_TERMINATED" if it.assurance == "infeasible" else None
        req = s["req"]
        out = {"sessionId": sid, "device": req["device"], "qosProfile": req["qosProfile"],
               "duration": round(it.expires - it.start) if it else req.get("duration"),
               "qosStatus": qs, "startedAt": _iso(it.start) if it else None, "expiresAt": _iso(it.expires) if it else None,
               "remainingSeconds": max(0, round(it.expires - now)) if it else 0,
               "x-intentqos": {"lease": s["lease"], "assurance": it.assurance if it else None,
                               "detail": it.assurance_detail if it else None,
                               "network_extension": s.get("network_extension")}}
        for k in ("applicationServer", "devicePorts", "applicationServerPorts", "sink"):
            if k in req:
                out[k] = req[k]
        if info:
            out["statusInfo"] = info
        return out

    def create(self, body: dict, requester: str = "camara-app") -> dict:
        prof = body.get("qosProfile")
        if prof not in PROFILES:
            raise CamaraError(400, "INVALID_ARGUMENT", f"qosProfile must be one of {', '.join(PROFILES)}")
        try:
            dur = int(body.get("duration", 3600))
        except (TypeError, ValueError):
            raise CamaraError(400, "INVALID_ARGUMENT", "duration must be a number of seconds") from None
        if dur < 60:
            raise CamaraError(400, "OUT_OF_RANGE", "duration must be at least 60 seconds")
        ip = device_ip(body.get("device"))
        p = PROFILES[prof]
        e = self.eng
        r = e.intents.request({"target": ip, "goal": p["goal"], "duration_s": dur, "latency_ms": p["latency_ms"],
                               "demand_down_kbit": p["down_kbit"], "demand_up_kbit": p["up_kbit"], "priority": 2,
                               "requester": requester[:64], "label": f"CAMARA {prof}"},
                              e.estimator.down.rate, e.estimator.up.rate)
        if not r.get("ok"):
            raise CamaraError(409 if "budget" in r.get("reason", "") or "capacity" in r.get("reason", "") else 400,
                              "QUALITY_ON_DEMAND.UNAVAILABLE" if "capacity" in r.get("reason", "") else "CONFLICT",
                              r.get("reason", "request refused"))
        sid = r["intent"]["id"]
        ext = (body.get("x-intentqos") or {}).get("network_extension") if isinstance(body.get("x-intentqos"), dict) else None
        self.sessions[sid] = {"lease": sid, "req": {k: v for k, v in body.items() if k != "x-intentqos"},
                              "created": time.time(), "network_extension": ext}
        return self._status(sid)

    def get(self, sid: str) -> dict:
        return self._status(sid)

    def delete(self, sid: str) -> None:
        s = self.sessions.get(sid)
        if s is None:
            raise CamaraError(404, "NOT_FOUND", "the session does not exist")
        self.eng.intents.cancel(s["lease"])

    def extend(self, sid: str, body: dict) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            raise CamaraError(404, "NOT_FOUND", "the session does not exist")
        try:
            add = int(body.get("requestedAdditionalDuration", 0))
        except (TypeError, ValueError):
            raise CamaraError(400, "INVALID_ARGUMENT", "requestedAdditionalDuration must be seconds") from None
        im = self.eng.intents
        it = im.intents.get(s["lease"])
        if it is None or it.status not in ("active", "scheduled"):
            raise CamaraError(409, "CONFLICT", "the session is no longer active")
        new_end = min(it.start + im.max_duration, it.expires + max(0, add))
        used = im.used_minutes(it.requester, time.time())
        extra_min = (new_end - it.expires) / 60.0
        if used + extra_min > im.budget_min:
            raise CamaraError(409, "CONFLICT", "the household's daily boost budget does not allow this extension")
        with im.lock:
            it.expires = new_end
            im.usage.setdefault(it.requester, []).append([im._day(time.time()), extra_min])
            im._save()
        return self._status(sid)

    def retrieve(self, body: dict) -> list[dict]:
        ip = device_ip(body.get("device"))
        return [self._status(sid) for sid, s in self.sessions.items()
                if device_ip(s["req"]["device"]) == ip and self._status(sid)["qosStatus"] != "UNAVAILABLE"]


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
