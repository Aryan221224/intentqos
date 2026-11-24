"""Proactive boost offers: suggest a lease at the moment it would help, and let the household decide.

The household should not have to notice a problem first. When a call or a game starts on a device while
the line is busy (other devices downloading, delay rising, or the app's Quality of Outcome slipping), the
engine offers a boost for that device in the app. Nothing changes until someone accepts it; an accepted
offer is an ordinary lease (budget, admission control and bargaining apply). One offer per device per
30 minutes, and dismissed offers are respected for that long.

Only real sessions get an offer. In the full-system test 12 of 13 offers named a laptop or phone whose only
"call" was the Test Kit's delay probes (small DNS queries, ~1-2 kbit/s per flow) or a 200 ms ping. So an app
must have been seen under the same label for MIN_AGE_S and carry at least MIN_KBIT in both directions (a voice
call sends 20-100 kbit/s each way, a game 10-100, a video call hundreds) before it is considered.
"""
from __future__ import annotations

import os
import time

OFFER_LABELS = ("voip", "video", "gaming")
OFFER_TTL_S = 300.0
QUIET_S = 1800.0
MIN_AGE_S = 20.0
MIN_KBIT = {"voip": (16.0, 16.0), "video": (100.0, 50.0), "gaming": (8.0, 8.0)}   # (down, up) kbit/s


class Offers:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.last_by_device: dict[str, float] = {}
        self.since: dict[tuple, float] = {}          # (ip, label) -> first seen as a real session

    def consider(self, apps: list, devices: list, est, intents: list, names: dict, now: float | None = None) -> list:
        """apps: experience snapshot apps; devices: engine device list; returns the offers made now."""
        now = time.time() if now is None else now
        for k in [k for k, o in self.items.items() if o["expires"] < now or o["state"] != "open"]:
            if self.items[k]["expires"] < now - 3600:
                self.items.pop(k, None)
        leased = {it.target for it in intents}
        heavy = sum(1 for d in devices if d.get("elastic"))
        busy_line = (est is not None and getattr(est, "enabled", False) and
                     (est.down.load >= 0.7 or est.down.state in ("bloat", "probe")))
        made = []
        live = set()
        for a in apps:
            ip, label = a.get("ip"), a.get("label")
            if label not in OFFER_LABELS:
                continue
            need_d, need_u = MIN_KBIT[label]
            if (a.get("kbit_down") or 0) < need_d or (a.get("kbit_up") or 0) < need_u:
                continue                              # probes and pings, not a call or a game
            live.add((ip, label))
            if now - self.since.setdefault((ip, label), now) < MIN_AGE_S or ip in leased:
                continue
            if now - self.last_by_device.get(ip, -1e9) < QUIET_S:
                continue
            slipping = a.get("qoo") is not None and a["qoo"] < 80
            if not (busy_line or heavy >= 1 or slipping):
                continue
            why = ("its call quality is slipping" if slipping else
                   "the line is busy" if busy_line else "another device is downloading")
            kind = {"voip": "call", "video": "video call", "gaming": "game"}[a["label"]]
            oid = os.urandom(4).hex()
            o = {"id": oid, "ip": ip, "device": names.get(ip) or a.get("device") or ip, "kind": kind,
                 "reason": f"A {kind} started on {names.get(ip) or ip} and {why}.",
                 "suggest": {"target": ip, "goal": "low_latency", "duration_s": 3600, "priority": 2,
                             "label": f"Offered boost: {kind}"},
                 "created": now, "expires": now + OFFER_TTL_S, "state": "open"}
            self.items[oid] = o
            self.last_by_device[ip] = now
            made.append(o)
        for k in [k for k in self.since if k not in live]:
            del self.since[k]                         # the session ended: it has to last MIN_AGE_S again
        return made

    def accept(self, oid: str, eng, requester: str = "household", minutes: float | None = None) -> dict:
        o = self.items.get(oid)
        if not o or o["state"] != "open" or o["expires"] < time.time():
            return {"ok": False, "reason": "this offer is no longer open"}
        req = dict(o["suggest"], requester=requester)
        if minutes:
            req["duration_s"] = max(60, min(8 * 3600, float(minutes) * 60))
        r = eng.intents.request(req, eng.estimator.down.rate, eng.estimator.up.rate)
        if r.get("ok"):
            o["state"] = "accepted"
            o["lease"] = r["intent"]["id"]
        return r

    def dismiss(self, oid: str) -> bool:
        o = self.items.get(oid)
        if not o or o["state"] != "open":
            return False
        o["state"] = "dismissed"
        return True

    def open(self, now: float | None = None) -> list:
        now = time.time() if now is None else now
        return sorted((o for o in self.items.values() if o["state"] == "open" and o["expires"] >= now),
                      key=lambda o: -o["created"])
