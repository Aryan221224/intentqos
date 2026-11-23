"""CAMARA Quality on Demand at the operator: one API call that reaches into the home AND the access network.

The router's own CAMARA endpoint (intentqos/camara.py) can only protect a device inside the home. The ISP's
controller sits where an Open Gateway API normally lives, so an application (a telehealth or exam provider,
say) can call it with the CAMARA QoD v1 shapes and the operator carries the session across two segments:

  home     -> the subscriber's gateway, through the agent relay, as an IntentQoS lease (budget, admission
              control, shield all apply there)
  access   -> the operator's network adapter, which reserves headroom for the subscriber on the PON/OLT

The device is named the CAMARA way: `phoneNumber` (the subscriber's registered number) to find the home,
plus `ipv4Address.privateAddress` for the device inside it.

What is simulated, and labelled so in every response: the access-network adapter. `SimulatedAccessNetwork`
keeps a reservation ledger per OLT against a configurable PON capacity and refuses when reservations would
exceed the share an operator would allow for priority traffic. In deployment this class is replaced by one
that calls the real policy point, for example a RADIUS CoA to the BNG that switches the subscriber to a
higher service profile for the session, or the OLT's QoS configuration through NETCONF/YANG (BBF TR-385).
"""
from __future__ import annotations

import os
import time

from intentqos.camara import PROFILES, CamaraError

QOD = "/camara/quality-on-demand/v1"


class SimulatedAccessNetwork:
    """Reservation ledger per OLT (simulated). capacity_kbit is the PON's shared downstream capacity."""

    simulated = True

    def __init__(self, capacity_kbit: float = 2_400_000, priority_share: float = 0.2):
        self.capacity = capacity_kbit
        self.share = priority_share
        self.res: dict[str, dict] = {}            # reservation id -> {olt, down, up, expires}

    def _used(self, olt: str, t: float) -> float:
        return sum(r["down"] for r in self.res.values() if r["olt"] == olt and r["expires"] > t)

    def reserve(self, olt: str, down_kbit: float, up_kbit: float, duration_s: float) -> dict:
        t = time.time()
        for k in [k for k, r in self.res.items() if r["expires"] <= t]:
            self.res.pop(k)
        limit = self.capacity * self.share
        if self._used(olt, t) + down_kbit > limit:
            return {"ok": False, "reason": f"priority reservations on {olt} would exceed {round(limit / 1000)} Mbit/s"}
        rid = "acc-" + os.urandom(4).hex()
        self.res[rid] = {"olt": olt, "down": down_kbit, "up": up_kbit, "expires": t + duration_s}
        return {"ok": True, "id": rid, "olt": olt, "used_kbit": self._used(olt, t), "limit_kbit": limit}

    def extend(self, rid: str, add_s: float) -> bool:
        r = self.res.get(rid)
        if not r:
            return False
        r["expires"] += add_s
        return True

    def release(self, rid: str) -> None:
        self.res.pop(rid, None)

    def status(self, rid: str) -> str:
        r = self.res.get(rid)
        return "RESERVED" if r and r["expires"] > time.time() else "RELEASED"


class CamaraNorthbound:
    def __init__(self, ctl, network=None):
        self.ctl = ctl
        self.net = network or SimulatedAccessNetwork(**ctl.cfg.get("access_network", {}))
        self.sessions: dict[str, dict] = {}       # operator session id -> {gw, router_sid, access, olt, by}

    def _home(self, device: dict) -> tuple[dict, dict, str]:
        if not isinstance(device, dict) or not device.get("phoneNumber"):
            raise CamaraError(400, "INVALID_ARGUMENT", "device.phoneNumber identifies the subscriber's home")
        priv = (device.get("ipv4Address") or {}).get("privateAddress") or device.get("ipv6Address")
        if not priv:
            raise CamaraError(422, "MISSING_IDENTIFIER", "device.ipv4Address.privateAddress names the device in the home")
        phone = "".join(ch for ch in str(device["phoneNumber"]) if ch.isdigit())
        sub = None
        for s in self.ctl.db.q("SELECT * FROM subscribers WHERE phone IS NOT NULL"):
            if "".join(ch for ch in s["phone"] if ch.isdigit()).endswith(phone[-10:]):
                sub = s
                break
        if not sub:
            raise CamaraError(404, "IDENTIFIER_NOT_FOUND", "no subscriber with that phone number")
        g = self.ctl.db.one("SELECT * FROM gateways WHERE subscriber_id=? AND device_id IS NOT NULL", (sub["id"],))
        if not g:
            raise CamaraError(404, "IDENTIFIER_NOT_FOUND", "the subscriber has no managed gateway")
        return sub, g, priv

    def _relay(self, gw_id: int, method: str, path: str, body=None, by: str = "") -> dict:
        r = self.ctl.relay(gw_id, method, path, body, by=f"camara:{by}")
        if r.get("offline"):
            raise CamaraError(503, "UNAVAILABLE", "the home gateway is offline")
        if not r.get("ok"):
            raise CamaraError(502, "UNAVAILABLE", r.get("error", "the gateway did not answer"))
        status, out = r.get("status", 500), r.get("body")
        if status >= 400:
            out = out if isinstance(out, dict) else {}
            raise CamaraError(status, out.get("code", "CONFLICT"), out.get("message", "the home refused the session"))
        return out

    def _doc(self, sid: str, router_doc: dict) -> dict:
        s = self.sessions[sid]
        d = dict(router_doc or {})
        d["sessionId"] = sid
        d["device"] = s["device"]
        d["x-intentqos"] = {**(d.get("x-intentqos") or {}),
                            "segments": {"home": d.get("qosStatus"),
                                         "access": self.net.status(s["access"]) if s.get("access") else "NOT_REQUESTED"},
                            "access_network": {"olt": s["olt"], "simulated": getattr(self.net, "simulated", False)}}
        return d

    def create(self, body: dict, by: str) -> dict:
        prof = body.get("qosProfile")
        if prof not in PROFILES:
            raise CamaraError(400, "INVALID_ARGUMENT", f"qosProfile must be one of {', '.join(PROFILES)}")
        dur = int(body.get("duration", 3600))
        sub, g, priv = self._home(body.get("device"))
        p = PROFILES[prof]
        acc = self.net.reserve(sub.get("olt") or "unassigned", p["down_kbit"], p["up_kbit"], dur)
        if not acc["ok"]:
            raise CamaraError(409, "QUALITY_ON_DEMAND.UNAVAILABLE", acc["reason"])
        home_body = {k: v for k, v in body.items() if k != "device"}
        home_body["device"] = {"ipv4Address": {"privateAddress": priv}} if "." in priv else {"ipv6Address": priv}
        home_body["x-intentqos"] = {"network_extension": {"olt": acc["olt"], "reservation": acc["id"],
                                                          "simulated": getattr(self.net, "simulated", False)}}
        try:
            doc = self._relay(g["id"], "POST", QOD + "/sessions", home_body, by)
        except CamaraError:
            self.net.release(acc["id"])
            raise
        sid = "op-" + os.urandom(6).hex()
        self.sessions[sid] = {"gw": g["id"], "router_sid": doc["sessionId"], "access": acc["id"],
                              "olt": acc["olt"], "by": by, "device": body["device"], "created": time.time()}
        self.ctl.audit(by, "integration", "camara_session_created", sid,
                       {"gw": g["id"], "profile": prof, "duration": dur, "access": acc["id"]})
        return self._doc(sid, doc)

    def _get(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            raise CamaraError(404, "NOT_FOUND", "the session does not exist")
        return s

    def get(self, sid: str, by: str) -> dict:
        s = self._get(sid)
        return self._doc(sid, self._relay(s["gw"], "GET", f"{QOD}/sessions/{s['router_sid']}", None, by))

    def extend(self, sid: str, body: dict, by: str) -> dict:
        s = self._get(sid)
        doc = self._relay(s["gw"], "POST", f"{QOD}/sessions/{s['router_sid']}/extend", body, by)
        self.net.extend(s["access"], float(body.get("requestedAdditionalDuration", 0)))
        return self._doc(sid, doc)

    def delete(self, sid: str, by: str) -> None:
        s = self._get(sid)
        try:
            self._relay(s["gw"], "DELETE", f"{QOD}/sessions/{s['router_sid']}", None, by)
        finally:
            self.net.release(s["access"])
            self.sessions.pop(sid, None)
            self.ctl.audit(by, "integration", "camara_session_deleted", sid, {})

    def list(self) -> list[dict]:
        return [{"sessionId": k, "gw": v["gw"], "olt": v["olt"], "access": self.net.status(v["access"]),
                 "created": v["created"], "by": v["by"]} for k, v in self.sessions.items()]
