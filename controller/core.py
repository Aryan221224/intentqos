"""Controller logic: fleet state, agent protocol, alerts, incident correlation, rollouts, support
diagnosis, consumer-app services, privacy and reporting. Transport-agnostic (server.py does HTTP)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import urllib.request

from intentqos import release
from . import security as S
from .seqtest import SequentialGuard
from .privagg import Aggregators
from .camara_nb import CamaraNorthbound
from .db import DB

GRADE_ORDER = ["A+", "A", "B", "C", "D", "F"]
POOR = {"C", "D", "F"}
ALERT_TEXT = {
    "qoe_poor": "Delay under load is high (grade {grade}, added delay p95 {p95} ms)",
    "capacity_shortfall": "Link carries only {pct}% of the plan rate while busy ({rate} of {plan} Mbit/s)",
    "blind": "Delay reflectors not answering: the gateway holds its rates (blind mode)",
    "engine_down": "QoS engine not running on the gateway (watchdog holds the safe state)",
    "watchdog_trip": "Watchdog restored the safe state",
    "apply_failures": "Shaper changes failed and were rolled back",
    "drift": "Traffic mix changed: the classifier is unsure more often (drift)",
    "cpu_high": "Engine CPU high on the gateway ({cpu}%)",
    "capture_drops": "Gateway dropped packets before classifying them",
    "lease_degraded": "A prioritised device is not getting what it asked for",
    "offline": "Gateway has not reported for {mins} minutes",
}
SEVERITY = {"qoe_poor": "minor", "capacity_shortfall": "major", "blind": "minor", "engine_down": "major",
            "watchdog_trip": "major", "apply_failures": "minor", "drift": "info", "cpu_high": "minor",
            "capture_drops": "minor", "lease_degraded": "minor", "offline": "major"}
OPEN_AFTER = {"qoe_poor": 4, "capacity_shortfall": 4, "blind": 2, "cpu_high": 4, "lease_degraded": 4}
CLOSE_AFTER = 4
NETWORK_RULES = ("capacity_shortfall", "qoe_poor", "offline")
CONSUMER_VERDICT = {
    "network": "We found a problem in our network in your area. Engineers are working on it; you don't need to restart your router.",
    "home-line": "Your line is not delivering your plan's speed. We'll check it remotely and call you if a technician needs to visit.",
    "gateway-software": "Your router's smart sharing had a problem and has been restarted safely. We're watching it.",
    "upstream-or-usage": "Your connection gets congested at busy times. We're looking into it and will update you here.",
    "in-home-wifi-or-device": "Your internet connection is working well right now. If one device or room is slow, it may be Wi-Fi "
                              "coverage; our team will call you to help.",
}
CONSENT_DEFAULT = {"service": True, "quality_improvement": True, "support_diagnostics": True, "marketing": False}


def now() -> float:
    return time.time()


def jdump(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=str)


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def median(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def mann_whitney_p(a: list[float], b: list[float]) -> float:
    """Two-sided Mann-Whitney U, normal approximation with tie correction (n >= 5 per group)."""
    a, b = [x for x in a if x is not None], [x for x in b if x is not None]
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return 1.0
    allv = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks, i = [0.0] * len(allv), 0
    ties = 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    n = n1 + n2
    sd = math.sqrt(n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))) if n > 1 else 0
    if sd == 0:
        return 1.0
    z = (abs(u - mu) - 0.5) / sd
    return max(0.0, min(1.0, math.erfc(z / math.sqrt(2))))


def model_sigs(m: dict) -> list[dict]:
    """The release signatures stored for a model (a JSON list of {keyid, sig}; older rows hold one hex string)."""
    v = jload(m.get("signature"), None)
    return v if isinstance(v, list) else []


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.db = DB(os.path.join(cfg["data_dir"], "controller.db"))
        self.key = S.ReleaseKey(os.path.join(cfg["data_dir"], "release.key"))
        for m in self.db.q("SELECT id, signature FROM models"):     # older databases: one bare hex signature
            if m["signature"] and not str(m["signature"]).startswith("["):
                self.db.x("UPDATE models SET signature=? WHERE id=?",
                          (jdump([{"keyid": release.keyid(self.key.pub), "sig": m["signature"]}]), m["id"]))
        self.nonces = S.NonceCache()
        self.cv = threading.Condition()
        self.cmd_events: dict[int, threading.Event] = {}
        self.seen: dict = {}
        self.streaks: dict[tuple, int] = {}
        self.prev: dict[int, dict] = {}
        self.ingest_count = 0
        self.started = now()
        self.lock = threading.Lock()
        self.seq = SequentialGuard()
        self.agg = Aggregators(self.db, cfg.get("agg_helper_url", ""))
        self.camara = CamaraNorthbound(self)

    # ================================================================== audit / helpers
    def audit(self, actor: str, actor_type: str, action: str, target: str = "", detail=None, ip: str = "") -> None:
        self.db.x("INSERT INTO audit (t, actor, actor_type, action, target, detail, ip) VALUES (?,?,?,?,?,?,?)",
                  (now(), actor, actor_type, action, target, jdump(detail or {}), ip))

    def gw(self, gw_id: int) -> dict | None:
        return self.db.one("SELECT * FROM gateways WHERE id=?", (gw_id,))

    def sub_of(self, gw: dict) -> dict | None:
        return self.db.one("SELECT s.*, p.code plan_code, p.name plan_name, p.down_mbit, p.up_mbit, p.boost_minutes "
                           "FROM subscribers s LEFT JOIN plans p ON p.id=s.plan_id WHERE s.id=?", (gw["subscriber_id"],))

    def interval(self) -> float:
        return float(self.cfg.get("telemetry_interval_s", 15))

    # ================================================================== enrolment & agent protocol
    def enroll(self, body: dict, ip: str) -> dict:
        serial, claim = str(body.get("serial", "")), str(body.get("claim_code", ""))
        g = self.db.one("SELECT * FROM gateways WHERE serial=?", (serial,))
        if not g or not g["claim_hash"] or S.h(claim) != g["claim_hash"]:
            self.audit(serial or "?", "agent", "enroll_rejected", serial, {"reason": "unknown serial or bad claim code"}, ip)
            raise PermissionError("unknown serial or claim code")
        device_id = g["device_id"] or ("gw-" + S.token(6))
        secret = S.token(32)
        self.db.x("UPDATE gateways SET device_id=?, secret=?, hw_model=?, arch=?, agent_version=?, engine_version=?, "
                  "enrolled=?, last_seen=?, online=1 WHERE id=?",
                  (device_id, secret, body.get("hw_model"), body.get("arch"), body.get("agent_version"),
                   (body.get("engine") or {}).get("intentqos"), now(), now(), g["id"]))
        self.audit(serial, "agent", "enrolled", device_id, {"arch": body.get("arch")}, ip)
        self.event(g["id"], "enrol", "info", f"gateway {serial} enrolled as {device_id}")
        self.push_config(g["id"], "zero-touch provisioning")
        return {"device_id": device_id, "secret": secret, "release_pubkey": self.key.pub_hex, "interval": self.interval()}

    def auth_agent(self, headers, method: str, path: str, body: bytes) -> dict:
        dev = headers.get("X-IQ-Device", "")
        g = self.db.one("SELECT * FROM gateways WHERE device_id=?", (dev,))
        if not g or not g["secret"]:
            raise PermissionError("unknown device")
        why = S.verify_agent(g["secret"], method, path, body, headers.get("X-IQ-Time"), headers.get("X-IQ-Nonce", ""),
                             headers.get("X-IQ-Signature", ""), self.nonces, dev)
        if why:
            raise PermissionError(why)
        return g

    def event(self, gw_id, kind: str, level: str, msg: str, t: float | None = None) -> None:
        self.db.x("INSERT INTO events (gw, t, kind, level, msg) VALUES (?,?,?,?,?)", (gw_id, t or now(), kind, level, msg))

    def sync(self, g: dict, body: dict) -> dict:
        t = now()
        recs = [r for r in body.get("records", []) if isinstance(r, dict) and r.get("t")]
        if recs:
            scope = (self.sub_of(g) or {}).get("olt") or "unassigned"
            for r in recs:                    # secret shares never sit next to the home's telemetry
                self.agg.take(g, scope, r)
            self.db.many("INSERT INTO telemetry (gw, t, rec) VALUES (?,?,?)", [(g["id"], r["t"], jdump(r)) for r in recs])
            last = recs[-1]
            self.db.x("INSERT INTO latest (gw, t, rec) VALUES (?,?,?) ON CONFLICT(gw) DO UPDATE SET t=excluded.t, rec=excluded.rec",
                      (g["id"], last["t"], jdump(last)))
            for r in recs:
                for e in r.get("events", []):
                    self.event(g["id"], e["kind"], e["level"], e["msg"], e["t"])
                self.evaluate(g, r)
            with self.lock:
                self.ingest_count += len(recs)
        st = body.get("state", {})
        v = (recs[-1].get("versions") if recs else None) or {}
        self.db.x("UPDATE gateways SET last_seen=?, online=1, buffered=?, config_version=COALESCE(?, config_version), "
                  "model_version=COALESCE(?, model_version), level=COALESCE(?, level), level_until=?, agent_version=COALESCE(?, agent_version), "
                  "engine_version=COALESCE(?, engine_version) WHERE id=?",
                  (t, int(body.get("buffered", 0)), st.get("config_version"), st.get("model_version"), st.get("level"),
                   st.get("level_until"), st.get("agent"), v.get("engine"), g["id"]))
        # a long-poll (/wait) may already have marked the gateway online, so close by alert, not by flag
        if self.close_alert(g["id"], "offline") or g["online"] == 0:
            self.event(g["id"], "online", "info", "gateway back online" + (f" ({len(recs)} buffered records)" if len(recs) > 1 else ""))
        self.store_results(g, body.get("results", []))
        cmds = self.take_commands(g["id"])
        sub = self.sub_of(g) or {}
        consent = {**CONSENT_DEFAULT, **jload(sub.get("consent"), {})}
        level = "standard" if consent.get("quality_improvement") else "basic"
        return {"commands": cmds, "interval": self.interval(), "level": level}

    SIGNED_KINDS = {"apply_config": "config", "install_model": "model"}

    def next_release_seq(self) -> int:
        with self.lock:
            n = int(self.db.meta("release_seq") or 0) + 1
            self.db.set_meta("release_seq", n)
            return n

    def take_commands(self, gw_id: int) -> list[dict]:
        rows = self.db.q("SELECT id, kind, args FROM commands WHERE gw=? AND status='pending' ORDER BY id", (gw_id,))
        if not rows:
            return []
        self.db.many("UPDATE commands SET status='sent', sent=? WHERE id=?", [(now(), r["id"]) for r in rows])
        dev = self.db.val("SELECT device_id FROM gateways WHERE id=?", (gw_id,)) or ""
        out = []
        for r in rows:
            args = jload(r["args"], {})
            kind = self.SIGNED_KINDS.get(r["kind"])
            if kind and "body" in args:
                # TUF-style instruction: bound to this gateway, monotonic sequence, short expiry
                args = {"envelope": self.key.instruction(kind, dev, self.next_release_seq(), args["body"]),
                        "by": args.get("by")}
            out.append({"id": r["id"], "kind": r["kind"], "args": args})
        return out

    def wait(self, g: dict, timeout: float) -> dict:
        end = now() + min(30.0, timeout)
        self.db.x("UPDATE gateways SET last_seen=?, online=1 WHERE id=?", (now(), g["id"]))
        with self.cv:
            while True:
                n = self.db.val("SELECT COUNT(*) FROM commands WHERE gw=? AND status='pending'", (g["id"],))
                if n or now() >= end:
                    break
                self.cv.wait(timeout=min(1.0, end - now()))
        return {"commands": self.take_commands(g["id"])}

    def store_results(self, g: dict, results: list) -> None:
        for r in results:
            c = self.db.one("SELECT * FROM commands WHERE id=? AND gw=?", (r.get("id"), g["id"]))
            if not c:
                continue
            res = r.get("result", {})
            self.db.x("UPDATE commands SET status=?, done=?, result=? WHERE id=?",
                      ("done" if res.get("ok") else "failed", now(), jdump(res), c["id"]))
            if c["kind"] in ("apply_config", "install_model", "rollback_model", "set_level"):
                self.event(g["id"], "command", "info" if res.get("ok") else "warn",
                           f"{c['kind']} {'applied' if res.get('ok') else 'FAILED'}"
                           + (f": {res.get('error')}" if res.get("error") else ""))
            if c["rollout_id"]:
                self.db.x("UPDATE rollout_members SET status=? WHERE rollout_id=? AND gw=?",
                          ("ok" if res.get("ok") else "failed", c["rollout_id"], g["id"]))
            if c["kind"] == "line_test" and res.get("ok"):
                self.event(g["id"], "line_test", "info", f"line test: {res.get('line_mbps', res.get('mbps'))} Mbit/s"
                                                         f"{' (household ' + str(res['household_mbps']) + ')' if res.get('household_mbps') else ''}, delay added under load "
                                                         f"{res.get('loaded_added_ms_p95')} ms")
            ev = self.cmd_events.pop(c["id"], None)
            if ev:
                ev.set()

    # ================================================================== commands
    def enqueue(self, gw_id: int, kind: str, args: dict, by: str, rollout_id: int | None = None) -> int:
        cid = self.db.x("INSERT INTO commands (gw, kind, args, status, requested_by, created, rollout_id) "
                        "VALUES (?,?,?,?,?,?,?)", (gw_id, kind, jdump(args), "pending", by, now(), rollout_id))
        with self.cv:
            self.cv.notify_all()
        return cid

    def call(self, gw_id: int, kind: str, args: dict, by: str, timeout: float = 12.0) -> dict:
        g = self.gw(gw_id)
        if not g or not g["online"]:
            return {"ok": False, "error": "gateway is offline", "offline": True}
        ev = threading.Event()
        cid = self.enqueue(gw_id, kind, args, by)
        self.cmd_events[cid] = ev
        if not ev.wait(timeout):
            self.cmd_events.pop(cid, None)
            self.db.x("UPDATE commands SET status='timeout' WHERE id=? AND status IN ('pending','sent')", (cid,))
            return {"ok": False, "error": "the gateway did not answer in time", "command": cid}
        c = self.db.one("SELECT result FROM commands WHERE id=?", (cid,))
        r = jload(c["result"], {}) if c else {}
        r["command"] = cid
        if kind == "relay" and (args.get("method") or "GET") == "GET":
            self.db.x("DELETE FROM commands WHERE id=?", (cid,))      # live views are pass-through, not history
        return r

    def relay(self, gw_id: int, method: str, path: str, body=None, by: str = "", timeout: float = 12.0) -> dict:
        return self.call(gw_id, "relay", {"method": method, "path": path, "body": body}, by, timeout)

    # ================================================================== configuration
    def profile(self, pid: int | None) -> dict | None:
        if pid:
            return self.db.one("SELECT * FROM profiles WHERE id=?", (pid,))
        return self.db.one("SELECT * FROM profiles WHERE name='default' ORDER BY version DESC LIMIT 1")

    def desired_settings(self, g: dict) -> tuple[dict, str]:
        p = self.profile(g["profile_id"])
        st = jload(p["settings"], {}) if p else {}
        sub = self.sub_of(g)
        if sub and sub.get("down_mbit"):
            st["link.down_mbit"] = sub["down_mbit"]
            st["link.up_mbit"] = sub["up_mbit"]
            st["intents.daily_budget_min"] = sub.get("boost_minutes") or st.get("intents.daily_budget_min", 480)
        name = f"{p['name']} v{p['version']}" if p else "default"
        return st, name

    def push_config(self, gw_id: int, by: str, rollout_id: int | None = None) -> int | None:
        g = self.gw(gw_id)
        if not g:
            return None
        st, name = self.desired_settings(g)
        ver = int(g["desired_config_version"] or 0) + 1
        payload = {"version": ver, "profile": name, "set": st}
        self.db.x("UPDATE gateways SET desired_config_version=? WHERE id=?", (ver, gw_id))
        # signed at delivery (take_commands): the envelope then carries a fresh sequence number and expiry
        return self.enqueue(gw_id, "apply_config", {"body": payload, "by": by}, by, rollout_id)

    # ================================================================== alerts & incidents
    def _streak(self, gw_id: int, rule: str, active: bool) -> int:
        """Consecutive records with the condition (positive) or without it (negative)."""
        k = (gw_id, rule)
        cur = self.streaks.get(k, 0)
        cur = (cur + 1 if cur > 0 else 1) if active else (cur - 1 if cur < 0 else -1)
        self.streaks[k] = cur
        return cur

    def open_alert(self, gw_id: int, rule: str, detail: str, severity: str | None = None) -> None:
        a = self.db.one("SELECT id FROM alerts WHERE gw=? AND rule=? AND status IN ('open','ack')", (gw_id, rule))
        if a:
            self.db.x("UPDATE alerts SET updated=?, detail=? WHERE id=?", (now(), detail, a["id"]))
            return
        self.db.x("INSERT INTO alerts (gw, rule, severity, opened, updated, status, detail) VALUES (?,?,?,?,?,?,?)",
                  (gw_id, rule, severity or SEVERITY[rule], now(), now(), "open", detail))
        self.event(gw_id, "alert", "warn", f"alert opened: {detail}")
        self.webhook("alert.opened", {"gateway": gw_id, "rule": rule, "detail": detail})

    def close_alert(self, gw_id: int, rule: str) -> bool:
        a = self.db.one("SELECT id FROM alerts WHERE gw=? AND rule=? AND status IN ('open','ack')", (gw_id, rule))
        if a:
            self.db.x("UPDATE alerts SET status='closed', closed=? WHERE id=?", (now(), a["id"]))
            self.event(gw_id, "alert", "info", f"alert cleared: {rule.replace('_', ' ')}")
        return bool(a)

    def evaluate(self, g: dict, r: dict) -> None:
        gid = g["id"]
        prev = self.prev.get(gid, {})
        self.prev[gid] = r
        if r.get("engine_down"):
            self.open_alert(gid, "engine_down", ALERT_TEXT["engine_down"])
            return
        self.close_alert(gid, "engine_down")
        checks = {
            "qoe_poor": (r.get("grade") in POOR, ALERT_TEXT["qoe_poor"].format(grade=r.get("grade"), p95=r.get("p95_add_d"))),
        }
        plan = (r.get("plan") or {}).get("d") or 0
        rate = r.get("rate_d") or 0
        busy = (r.get("est") or {}).get("d") in ("bloat", "high")
        checks["capacity_shortfall"] = (plan > 0 and busy and rate < 0.6 * plan,
                                        ALERT_TEXT["capacity_shortfall"].format(pct=round(100 * rate / plan) if plan else 0,
                                                                                rate=round(rate / 1000, 1), plan=round(plan / 1000)))
        checks["blind"] = (bool((r.get("est") or {}).get("blind")), ALERT_TEXT["blind"])
        checks["cpu_high"] = ((r.get("cpu") or 0) > 70, ALERT_TEXT["cpu_high"].format(cpu=r.get("cpu")))
        checks["lease_degraded"] = (any(i.get("assurance") in ("degraded", "infeasible") for i in r.get("intents", [])),
                                    ALERT_TEXT["lease_degraded"])
        for rule, (active, text) in checks.items():
            s = self._streak(gid, rule, active)
            if active and s >= OPEN_AFTER.get(rule, 1):
                sev = "major" if rule == "qoe_poor" and r.get("grade") in ("D", "F") else None
                self.open_alert(gid, rule, text, sev)
            elif not active and s <= -CLOSE_AFTER:
                self.close_alert(gid, rule)
        sf, ps = r.get("safety") or {}, prev.get("safety") or {}
        if ps and sf.get("fail", 0) > ps.get("fail", 0):
            self.open_alert(gid, "apply_failures", ALERT_TEXT["apply_failures"])
        cd, pd = (r.get("cap") or {}).get("drops", 0), (prev.get("cap") or {}).get("drops", 0)
        if prev and cd > pd + 100:
            self.open_alert(gid, "capture_drops", ALERT_TEXT["capture_drops"] + f" (+{cd - pd})")
        for e in r.get("events", []):
            if e["kind"] == "watchdog":
                self.open_alert(gid, "watchdog_trip", ALERT_TEXT["watchdog_trip"] + ": " + e["msg"][:120])
            if e["kind"] == "drift":
                self.open_alert(gid, "drift", ALERT_TEXT["drift"])

    def housekeeping(self) -> None:
        """Every 15 s: offline detection, incident correlation, rollouts, rollups, expiry, retention."""
        t = now()
        lim = t - max(60.0, 4 * self.interval())
        if t - self.started < max(60.0, 4 * self.interval()):
            lim = 0.0        # just (re)started: give every gateway one full interval to report before calling it offline
        for g in self.db.q("SELECT id, last_seen FROM gateways WHERE online=1 AND last_seen < ?", (lim,)):
            self.db.x("UPDATE gateways SET online=0 WHERE id=?", (g["id"],))
            self.open_alert(g["id"], "offline", ALERT_TEXT["offline"].format(mins=round((t - (g["last_seen"] or t)) / 60, 1)))
        for a in self.db.q("SELECT id FROM alerts WHERE status IN ('open','ack') AND rule IN ('watchdog_trip','apply_failures',"
                           "'capture_drops') AND updated < ?", (t - 900,)):
            self.db.x("UPDATE alerts SET status='closed', closed=? WHERE id=?", (t, a["id"]))
        self.db.x("UPDATE alerts SET status='closed', closed=? WHERE status IN ('open','ack') AND rule='drift' AND updated < ?",
                  (t, t - 3600))
        self.correlate()
        self.rollout_tick()
        self.rollup()
        if int(t) % 3600 < 16:
            self.retention()

    def correlate(self) -> None:
        """Many homes on one OLT/PoP degrading together is a network problem, not N home problems."""
        t = now()
        homes = self.db.q("SELECT g.id, g.online, s.olt, s.pop, s.region FROM gateways g JOIN subscribers s ON s.id=g.subscriber_id "
                          "WHERE g.device_id IS NOT NULL")
        open_alerts = self.db.q("SELECT gw, rule, opened FROM alerts WHERE status IN ('open','ack') AND rule IN (?,?,?)",
                                NETWORK_RULES)
        by_gw: dict[int, set] = {}
        for a in open_alerts:
            by_gw.setdefault(a["gw"], set()).add(a["rule"])
        groups: dict[str, list] = {}
        for h in homes:
            groups.setdefault(h["olt"], []).append(h)
        for olt, hs in groups.items():
            if not olt or len(hs) < 5:
                continue
            aff = [h for h in hs if by_gw.get(h["id"])]
            off = [h for h in aff if "offline" in by_gw.get(h["id"], ())]
            frac = len(aff) / len(hs)
            inc = self.db.one("SELECT * FROM incidents WHERE scope_type='olt' AND scope=? AND status IN ('open','investigating',"
                              "'identified') ORDER BY id DESC LIMIT 1", (olt,))
            if len(aff) >= 3 and frac >= 0.25:
                kind = "outage" if len(off) >= 0.6 * len(aff) else "degradation"
                summ = (f"{len(aff)} of {len(hs)} homes on {olt} ({hs[0]['pop']}, {hs[0]['region']}) "
                        + ("lost their connection" if kind == "outage" else "lost capacity or show high delay under load")
                        + " at the same time: upstream of the homes")
                if not inc:
                    iid = self.db.x("INSERT INTO incidents (scope_type, scope, kind, opened, updated, status, summary, affected, total) "
                                    "VALUES ('olt',?,?,?,?,'open',?,?,?)", (olt, kind, t, t, summ, len(aff), len(hs)))
                    self.audit("correlator", "system", "incident_opened", olt, {"id": iid, "kind": kind, "affected": len(aff)})
                    self.webhook("incident.opened", {"id": iid, "scope": olt, "kind": kind, "affected": len(aff)})
                    if kind == "outage" and self.cfg.get("auto_notify_outages", True):
                        self.notify_incident(iid, "system")
                else:
                    iid = inc["id"]
                    self.db.x("UPDATE incidents SET updated=?, affected=MAX(affected, ?), summary=?, kind=? WHERE id=?",
                              (t, len(aff), summ, kind, iid))
                self.db.many("UPDATE alerts SET incident_id=? WHERE gw=? AND status IN ('open','ack') AND rule IN ('capacity_shortfall',"
                             "'qoe_poor','offline')", [(iid, h["id"]) for h in aff])
            elif inc and frac < 0.1:
                self.db.x("UPDATE incidents SET status='resolved', closed=?, updated=? WHERE id=?", (t, t, inc["id"]))
                self.audit("correlator", "system", "incident_resolved", olt, {"id": inc["id"]})
                self.webhook("incident.resolved", {"id": inc["id"], "scope": olt})
                if inc["notified"]:
                    for s in self.db.q("SELECT s.id FROM subscribers s JOIN gateways g ON g.subscriber_id=s.id WHERE s.olt=?", (olt,)):
                        self.notify(s["id"], "incident", "Service restored in your area",
                                    "The network problem in your area has been fixed. Thank you for your patience.", f"incident:{inc['id']}")

    def notify_incident(self, iid: int, by: str) -> int:
        inc = self.db.one("SELECT * FROM incidents WHERE id=?", (iid,))
        if not inc:
            return 0
        subs = self.db.q("SELECT id FROM subscribers WHERE olt=?", (inc["scope"],))
        title = "Network issue in your area" if inc["kind"] == "degradation" else "Outage in your area"
        body = ("We have detected a problem in our network near you that may make your internet slower or laggy. "
                "Our engineers are working on it; you don't need to restart your router.") if inc["kind"] == "degradation" else (
                "Some homes near you have lost their connection. Our engineers are working on it. If the outage lasts more "
                "than 4 hours you are eligible for a rebate under TRAI rules; it is applied automatically.")
        for s in subs:
            self.notify(s["id"], "incident", title, body, f"incident:{iid}")
        self.db.x("UPDATE incidents SET notified=?, status=CASE WHEN status='open' THEN 'identified' ELSE status END WHERE id=?",
                  (now(), iid))
        self.audit(by, "staff" if by != "system" else "system", "incident_notified", inc["scope"], {"id": iid, "subscribers": len(subs)})
        return len(subs)

    # ================================================================== rollouts
    def _bucket(self, serial: str, rid: int) -> float:
        return int(hashlib.sha256(f"{rid}:{serial}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 100

    def create_rollout(self, kind: str, target: str, name: str, stages: list[dict], by: str,
                       guardrails: dict | None = None) -> int:
        if kind == "profile" and not self.db.one("SELECT id FROM profiles WHERE id=?", (int(target),)):
            raise ValueError("no such profile")
        if kind == "model" and not self.db.one("SELECT id FROM models WHERE version=?", (target,)):
            raise ValueError("no such model")
        if self.db.one("SELECT id FROM rollouts WHERE status IN ('running','paused')"):
            raise ValueError("another rollout is running; finish or halt it first")
        gr = {"max_p95_increase_ms": 5.0, "max_p95_increase_rel": 0.2, "p_value": 0.05, "max_failure_rate": 0.05,
              "auto_rollback": True, "test": "msprt", **(guardrails or {})}
        rid = self.db.x("INSERT INTO rollouts (kind, target, name, stages, stage, status, guardrails, created_by, created, "
                        "stage_started, previous) VALUES (?,?,?,?,0,'running',?,?,?,?,'{}')",
                        (kind, str(target), name, jdump(stages), jdump(gr), by, now(), now()))
        self.audit(by, "staff", "rollout_created", name, {"id": rid, "kind": kind, "target": target, "stages": stages})
        self._assign(rid, 0)
        return rid

    def _assign(self, rid: int, stage: int) -> None:
        r = self.db.one("SELECT * FROM rollouts WHERE id=?", (rid,))
        stages = jload(r["stages"], [])
        pct = stages[stage]["pct"]
        prev = jload(r["previous"], {})
        have = {m["gw"] for m in self.db.q("SELECT gw FROM rollout_members WHERE rollout_id=?", (rid,))}
        for g in self.db.q("SELECT id, serial, profile_id, model_version FROM gateways WHERE device_id IS NOT NULL"):
            if g["id"] in have or self._bucket(g["serial"], rid) >= pct:
                continue
            self.db.x("INSERT INTO rollout_members (rollout_id, gw, stage, assigned, status) VALUES (?,?,?,?,?)",
                      (rid, g["id"], stage, now(), "pending"))
            if r["kind"] == "profile":
                prev[str(g["id"])] = g["profile_id"]
                self.db.x("UPDATE gateways SET profile_id=? WHERE id=?", (int(r["target"]), g["id"]))
                self.push_config(g["id"], f"rollout {r['name']}", rid)
            else:
                prev[str(g["id"])] = g["model_version"]
                m = self.db.one("SELECT * FROM models WHERE version=?", (r["target"],))
                self.enqueue(g["id"], "install_model", {"body": {"version": m["version"], "sha256": m["sha256"],
                                                                 "size": m["size"], "targets_sigs": model_sigs(m)},
                                                        "by": f"rollout {r['name']}"}, f"rollout {r['name']}", rid)
        self.db.x("UPDATE rollouts SET previous=?, stage=?, stage_started=? WHERE id=?", (jdump(prev), stage, now(), rid))

    def rollout_eval(self, rid: int) -> dict:
        r = self.db.one("SELECT * FROM rollouts WHERE id=?", (rid,))
        since = r["stage_started"]
        members = {m["gw"]: m for m in self.db.q("SELECT * FROM rollout_members WHERE rollout_id=?", (rid,))}
        rows = self.db.q("SELECT gw, json_extract(rec,'$.p95_add_d') p, json_extract(rec,'$.add_d_max') mx FROM telemetry "
                         "WHERE t >= ? AND json_extract(rec,'$.p95_add_d') IS NOT NULL", (since,))
        per: dict[int, list] = {}
        for x in rows:
            per.setdefault(x["gw"], []).append(x["p"])
        canary = [median(v) for g, v in per.items() if g in members]
        control = [median(v) for g, v in per.items() if g not in members]
        mc, mk = median(canary), median(control)
        p = mann_whitney_p(canary, control)
        n_fail = sum(1 for m in members.values() if m["status"] == "failed")
        fail_rate = n_fail / max(1, len(members))
        gr = jload(r["guardrails"], {})
        # always-valid p (mSPRT): the guard looks at every tick, which a fixed-sample test does not allow
        p_seq = self.seq.update((rid, r["stage"]), canary, control, float(gr.get("max_p95_increase_ms", 5.0)))
        p_use = p_seq if gr.get("test", "msprt") == "msprt" else p
        breach = []
        if mc is not None and mk is not None:
            inc = mc - mk
            if inc > gr["max_p95_increase_ms"] and inc > gr["max_p95_increase_rel"] * max(mk, 1) and p_use < gr["p_value"]:
                breach.append(f"delay added under load got worse on updated homes: median p95 {mc:.1f} ms vs {mk:.1f} ms "
                              f"on the rest (always-valid p = {p_seq:.3g}; fixed-sample p = {p:.3g})")
        if fail_rate > gr["max_failure_rate"]:
            breach.append(f"{n_fail} of {len(members)} homes failed to apply the update")
        return {"canary_n": len(canary), "control_n": len(control), "canary_p95": mc, "control_p95": mk, "p": p,
                "p_seq": p_seq, "looks": self.seq.looks.get((rid, r["stage"]), 0), "test": gr.get("test", "msprt"),
                "fail": n_fail, "members": len(members), "breach": breach,
                "acked": sum(1 for m in members.values() if m["status"] in ("ok", "failed"))}

    def rollout_tick(self) -> None:
        for r in self.db.q("SELECT * FROM rollouts WHERE status='running'"):
            stages = jload(r["stages"], [])
            st = stages[r["stage"]]
            ev = self.rollout_eval(r["id"])
            dec = jload(r["decisions"], [])
            if ev["breach"]:
                dec.append({"t": now(), "stage": r["stage"], "action": "halted", "why": ev["breach"], "eval": ev})
                self.db.x("UPDATE rollouts SET status='halted', decisions=? WHERE id=?", (jdump(dec), r["id"]))
                self.audit("rollout-guard", "system", "rollout_halted", r["name"], {"why": ev["breach"]})
                self.webhook("rollout.halted", {"id": r["id"], "name": r["name"], "why": ev["breach"]})
                if jload(r["guardrails"], {}).get("auto_rollback"):
                    self.rollback_rollout(r["id"], "rollout-guard")
                continue
            if now() - r["stage_started"] >= st.get("min_minutes", 10) * 60 and ev["acked"] >= 0.8 * max(1, ev["members"]):
                if r["stage"] + 1 < len(stages):
                    dec.append({"t": now(), "stage": r["stage"], "action": "advanced", "eval": ev})
                    self.db.x("UPDATE rollouts SET decisions=? WHERE id=?", (jdump(dec), r["id"]))
                    self._assign(r["id"], r["stage"] + 1)
                    self.audit("rollout-guard", "system", "rollout_advanced", r["name"], {"to_stage": r["stage"] + 1, "eval": ev})
                else:
                    dec.append({"t": now(), "stage": r["stage"], "action": "completed", "eval": ev})
                    self.db.x("UPDATE rollouts SET status='completed', decisions=? WHERE id=?", (jdump(dec), r["id"]))
                    self.audit("rollout-guard", "system", "rollout_completed", r["name"], {"eval": ev})

    def rollback_rollout(self, rid: int, by: str) -> int:
        r = self.db.one("SELECT * FROM rollouts WHERE id=?", (rid,))
        prev = jload(r["previous"], {})
        n = 0
        for m in self.db.q("SELECT gw FROM rollout_members WHERE rollout_id=?", (rid,)):
            gid = m["gw"]
            if r["kind"] == "profile":
                self.db.x("UPDATE gateways SET profile_id=? WHERE id=?", (prev.get(str(gid)), gid))
                self.push_config(gid, f"rollback of {r['name']}", rid)
            else:
                self.enqueue(gid, "rollback_model", {"version": prev.get(str(gid))}, f"rollback of {r['name']}", rid)
            n += 1
        dec = jload(r["decisions"], [])
        dec.append({"t": now(), "action": "rolled_back", "by": by, "homes": n})
        self.db.x("UPDATE rollouts SET status='rolled_back', decisions=? WHERE id=?", (jdump(dec), rid))
        self.audit(by, "staff" if by != "rollout-guard" else "system", "rollout_rolled_back", r["name"], {"homes": n})
        return n

    # ================================================================== rollups, retention, KPIs
    def rollup(self) -> None:
        t = now()
        b = math.floor((t - 300) / 300) * 300
        last = self.db.meta("rollup_done", 0)
        if b <= last:
            return
        start = max(last + 300, b - 3600 * 6) if last else b
        while start <= b:
            rows = self.db.q("SELECT gw, rec FROM telemetry WHERE t >= ? AND t < ?", (start, start + 300))
            per: dict[int, list] = {}
            for x in rows:
                per.setdefault(x["gw"], []).append(jload(x["rec"], {}))
            out = []
            for gw, rs in per.items():
                rs = [r for r in rs if not r.get("engine_down")]
                if not rs:
                    continue
                worst = max(rs, key=lambda r: GRADE_ORDER.index(r.get("grade", "A+")) if r.get("grade") in GRADE_ORDER else 0)
                out.append((gw, start, worst.get("grade"), median([r.get("p95_add_d") for r in rs]),
                            median([r.get("p95_add_u") for r in rs]), median([r.get("add_d") for r in rs]),
                            median([r.get("jit_d") for r in rs]), median([r.get("loss_d") for r in rs]),
                            median([r.get("rate_d") for r in rs]), median([r.get("ach_d") for r in rs]),
                            median([(r.get("plan") or {}).get("d") for r in rs]), median([r.get("cpu") for r in rs])))
            if out:
                self.db.many("INSERT OR REPLACE INTO rollup5 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
            start += 300
        self.db.set_meta("rollup_done", b)

    def retention(self) -> None:
        r = self.cfg.get("retention", {})
        t = now()
        self.db.x("DELETE FROM telemetry WHERE t < ?", (t - r.get("raw_hours", 48) * 3600,))
        self.db.x("DELETE FROM rollup5 WHERE t < ?", (t - r.get("rollup_days", 90) * 86400,))
        self.db.x("DELETE FROM events WHERE t < ?", (t - r.get("events_days", 90) * 86400,))
        self.db.x("DELETE FROM sessions WHERE expires < ?", (t,))
        self.agg.prune(r.get("agg_days", 7) * 86400)

    def fleet_summary(self) -> dict:
        t = now()
        gws = self.db.q("SELECT g.id, g.online, g.simulated, g.last_seen, l.rec, s.region, s.olt, p.code plan "
                        "FROM gateways g LEFT JOIN latest l ON l.gw=g.id LEFT JOIN subscribers s ON s.id=g.subscriber_id "
                        "LEFT JOIN plans p ON p.id=s.plan_id WHERE g.device_id IS NOT NULL")
        grades = {g: 0 for g in GRADE_ORDER}
        p95s, issues, regions = [], 0, {}
        for g in gws:
            r = jload(g["rec"], {}) or {}
            reg = regions.setdefault(g["region"] or "?", {"homes": 0, "online": 0, "issues": 0, "p95": []})
            reg["homes"] += 1
            if not g["online"]:
                continue
            reg["online"] += 1
            gr = r.get("grade")
            if gr in grades:
                grades[gr] += 1
            if gr in POOR or r.get("engine_down"):
                issues += 1
                reg["issues"] += 1
            if r.get("p95_add_d") is not None:
                p95s.append(r["p95_add_d"])
                reg["p95"].append(r["p95_add_d"])
        online = sum(1 for g in gws if g["online"])
        for v in regions.values():
            m = median(v.pop("p95"))
            v["p95_median"] = round(m, 1) if m is not None else None
        alerts = self.db.q("SELECT severity, COUNT(*) n FROM alerts WHERE status IN ('open','ack') GROUP BY severity")
        with self.lock:
            rate = self.ingest_count / max(1.0, t - self.started)
        return {"t": t, "homes": len(gws), "online": online, "offline": len(gws) - online,
                "simulated": sum(1 for g in gws if g["simulated"]), "grades": grades, "issues": issues,
                "issue_pct": round(100 * issues / max(1, online), 1),
                "p95_median": round(median(p95s), 1) if p95s else None,
                "p95_p90": sorted(p95s)[int(0.9 * (len(p95s) - 1))] if p95s else None,
                "trai_ok_pct": round(100 * sum(1 for x in p95s if x < 50) / max(1, len(p95s)), 1),
                "alerts": {a["severity"]: a["n"] for a in alerts},
                "incidents": self.db.q("SELECT * FROM incidents WHERE status != 'resolved' ORDER BY opened DESC"),
                "rollout": self.db.one("SELECT id, name, kind, status, stage, stages FROM rollouts ORDER BY id DESC LIMIT 1"),
                "regions": regions, "ingest_per_s": round(rate, 1)}

    def fleet_private_qoo(self, hours: float = 1.0) -> dict:
        """QoO histograms per OLT, released only through the two-aggregator path for batches of 5+ homes."""
        olts = [r["olt"] or "unassigned" for r in self.db.q("SELECT DISTINCT s.olt FROM subscribers s JOIN gateways g ON "
                                                             "g.subscriber_id=s.id WHERE g.device_id IS NOT NULL")]
        demo = not self.agg.helper_url
        return {"batches": self.agg.fleet(sorted(set(olts)), hours), "hours": hours,
                "helper": self.agg.helper_url or "local (both roles on this console)",
                "privacy": ("Demonstration: this console holds both shares, so it is not private against the console; "
                            "set agg_helper_url to a separately operated helper for real separation") if demo else
                           "Two parties: neither aggregator alone sees a home's histogram (no Prio3 validity proofs yet)"}

    def fleet_trend(self, hours: float = 6) -> list[dict]:
        rows = self.db.q("SELECT t, COUNT(*) n, SUM(CASE WHEN grade IN ('C','D','F') THEN 1 ELSE 0 END) poor, "
                         "AVG(p95_add_d) p95 FROM rollup5 WHERE t >= ? GROUP BY t ORDER BY t", (now() - hours * 3600,))
        out = [{"t": r["t"], "homes": r["n"], "poor_pct": round(100 * r["poor"] / max(1, r["n"]), 1),
                "p95_mean": round(r["p95"] or 0, 1)} for r in rows]
        # the current 5-minute bucket from raw telemetry
        rows = self.db.q("SELECT gw, json_extract(rec,'$.grade') g, json_extract(rec,'$.p95_add_d') p FROM telemetry WHERE t >= ?",
                         (math.floor(now() / 300) * 300 - 300 if not out else out[-1]["t"] + 300,))
        if rows:
            per = {}
            for x in rows:
                per[x["gw"]] = x
            v = list(per.values())
            out.append({"t": math.floor(now() / 60) * 60, "homes": len(v),
                        "poor_pct": round(100 * sum(1 for x in v if x["g"] in POOR) / len(v), 1),
                        "p95_mean": round(sum((x["p"] or 0) for x in v) / len(v), 1), "partial": True})
        return out

    def homes(self, q: str = "", flt: str = "", limit: int = 200) -> list[dict]:
        args, where = [], ["g.device_id IS NOT NULL"]
        if q:
            where.append("(s.account LIKE ? OR s.name LIKE ? OR s.phone LIKE ? OR g.serial LIKE ? OR s.olt LIKE ? OR s.area LIKE ?)")
            args += [f"%{q}%"] * 6
        rows = self.db.q("SELECT g.id, g.serial, g.online, g.simulated, g.last_seen, g.config_version, g.desired_config_version, "
                         "g.model_version, g.level, s.id sub_id, s.account, s.name, s.phone, s.region, s.olt, s.area, p.code plan, "
                         "p.down_mbit, l.rec FROM gateways g LEFT JOIN subscribers s ON s.id=g.subscriber_id LEFT JOIN plans p "
                         "ON p.id=s.plan_id LEFT JOIN latest l ON l.gw=g.id WHERE " + " AND ".join(where) + " ORDER BY s.account LIMIT ?",
                         args + [limit * 5 if flt else limit])
        al = {}
        for a in self.db.q("SELECT gw, rule, severity FROM alerts WHERE status IN ('open','ack')"):
            al.setdefault(a["gw"], []).append(a)
        out = []
        for r in rows:
            rec = jload(r.pop("rec"), {}) or {}
            r.update(grade=rec.get("grade"), p95=rec.get("p95_add_d"), rate_d=rec.get("rate_d"), ach_d=rec.get("ach_d"),
                     devices=rec.get("devices"), alerts=al.get(r["id"], []), phone=mask_phone(r["phone"]))
            if flt == "issues" and not (r["alerts"] or r["grade"] in POOR or not r["online"]):
                continue
            if flt == "offline" and r["online"]:
                continue
            if flt == "real" and r["simulated"]:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def home(self, gw_id: int) -> dict | None:
        g = self.gw(gw_id)
        if not g:
            return None
        sub = self.sub_of(g) or {}
        rec = jload(self.db.val("SELECT rec FROM latest WHERE gw=?", (gw_id,)), {}) or {}
        prof = self.profile(g["profile_id"])
        g = {k: v for k, v in g.items() if k not in ("secret", "claim_hash")}
        return {"gateway": g, "subscriber": {**{k: v for k, v in sub.items() if k != "phone"}, "phone": mask_phone(sub.get("phone")),
                                             "consent": {**CONSENT_DEFAULT, **jload(sub.get("consent"), {})}},
                "latest": rec, "profile": {"id": prof["id"], "name": prof["name"], "version": prof["version"]} if prof else None,
                "alerts": self.db.q("SELECT * FROM alerts WHERE gw=? ORDER BY opened DESC LIMIT 30", (gw_id,)),
                "events": self.db.q("SELECT t, kind, level, msg FROM events WHERE gw=? ORDER BY t DESC LIMIT 80", (gw_id,)),
                "commands": [{**c, "args": None, "result": jload(c["result"], None)} for c in
                             self.db.q("SELECT id, kind, status, requested_by, created, done, result FROM commands WHERE gw=? "
                                       "ORDER BY id DESC LIMIT 25", (gw_id,))],
                "notes": self.db.q("SELECT * FROM notes WHERE subscriber_id=? ORDER BY t DESC", (sub.get("id"),)),
                "tickets": self.db.q("SELECT id, category, status, created, summary, source FROM tickets WHERE subscriber_id=? "
                                     "ORDER BY id DESC LIMIT 10", (sub.get("id"),)),
                "diagnosis": self.diagnose(gw_id)}

    def timeline(self, gw_id: int, hours: float = 2) -> list[dict]:
        rows = self.db.q("SELECT t, rec FROM telemetry WHERE gw=? AND t >= ? ORDER BY t", (gw_id, now() - hours * 3600))
        out = []
        for x in rows:
            r = jload(x["rec"], {})
            out.append({"t": x["t"], "grade": r.get("grade"), "add_d": r.get("add_d"), "p95_add_d": r.get("p95_add_d"),
                        "add_d_max": r.get("add_d_max"), "add_u": r.get("add_u"), "rate_d": r.get("rate_d"), "ach_d": r.get("ach_d"),
                        "ach_d_max": r.get("ach_d_max"), "plan_d": (r.get("plan") or {}).get("d"), "loss_d": r.get("loss_d"),
                        "jit_d": r.get("jit_d"), "cpu": r.get("cpu"), "down": bool(r.get("engine_down")), "tin_d": r.get("tin_d")})
        return out

    # ================================================================== support diagnosis
    def diagnose(self, gw_id: int) -> dict:
        """'Is it the home or the network?' -- the support agent's first question, answered from data."""
        g = self.gw(gw_id)
        sub = self.sub_of(g) or {}
        rec = jload(self.db.val("SELECT rec FROM latest WHERE gw=?", (gw_id,)), {}) or {}
        alerts = {a["rule"]: a for a in self.db.q("SELECT * FROM alerts WHERE gw=? AND status IN ('open','ack')", (gw_id,))}
        inc = self.db.one("SELECT * FROM incidents WHERE scope=? AND status != 'resolved' ORDER BY id DESC LIMIT 1", (sub.get("olt"),))
        ev, actions = [], []
        plan = (rec.get("plan") or {}).get("d") or 0
        if inc:
            ev.append(f"Open {inc['kind']} on {inc['scope']}: {inc['summary']}")
            verdict, where = ("Area problem: this home is one of {} affected on {}. Not a home issue.".format(inc["affected"], inc["scope"]),
                              "network")
            actions = ["Tell the customer it is an area issue and share the incident status",
                       "Do not book a home visit", "Notify affected subscribers (NOC)"]
        elif not g["online"]:
            verdict, where = "Gateway offline and no area incident: power, cable or line problem at this home.", "home-line"
            ev.append(f"Last report {round((now() - (g['last_seen'] or now())) / 60)} min ago")
            actions = ["Ask the customer to check the router's power and fibre lights", "Check the ONT/port status",
                       "Book a technician if it stays offline"]
        elif rec.get("engine_down") or "engine_down" in alerts:
            verdict, where = "The QoS engine is not running; the watchdog keeps the gateway in its safe state.", "gateway-software"
            actions = ["Restart the QoS service remotely", "Check the gateway's last events", "Escalate to tier 2 if it recurs"]
        elif "capacity_shortfall" in alerts:
            verdict, where = ("Only this home's line is short of its plan while busy: likely the access line (ONT, fibre, port).",
                              "home-line")
            ev.append(alerts["capacity_shortfall"]["detail"])
            actions = ["Run a line test", "Check the ONT optical power and port errors", "Book a technician if the line test confirms"]
        elif "qoe_poor" in alerts or rec.get("grade") in POOR:
            verdict, where = ("Delay under load is high although the line carries its plan rate: congestion beyond the access "
                              "link or very heavy use at home.", "upstream-or-usage")
            ev.append(f"Grade {rec.get('grade')}, delay added p95 {rec.get('p95_add_d')} ms")
            actions = ["Run a line test", "Check the class mix for bulk transfers", "Watch for an area incident forming"]
        else:
            verdict, where = ("The internet path to this home is healthy. If the customer still has problems, it is most likely "
                              "Wi-Fi coverage or one device.", "in-home-wifi-or-device")
            ev.append(f"Grade {rec.get('grade', '–')}, delay added under load p95 {rec.get('p95_add_d', '–')} ms, "
                      f"shaping at {round((rec.get('rate_d') or 0) / 1000)} of {round(plan / 1000)} Mbit/s plan")
            actions = ["Ask which device and which room", "Suggest prioritising the device for calls or games",
                       "The gateway cannot see Wi-Fi airtime: use the Wi-Fi tools for coverage"]
        for k in ("blind", "drift", "lease_degraded", "cpu_high", "watchdog_trip", "apply_failures"):
            if k in alerts:
                ev.append(alerts[k]["detail"])
        loc = self.localise(gw_id, rec, sub)
        if loc and where == "in-home-wifi-or-device" and loc["segment"] != "unknown":
            verdict = loc["summary"]
            where = {"home-wifi": "in-home-wifi-or-device", "access": "home-line", "olt": "network",
                     "upstream": "upstream-or-usage", "none": where}.get(loc["segment"], where)
        return {"verdict": verdict, "where": where, "evidence": ev, "actions": actions, "localisation": loc, "t": now()}

    def localise(self, gw_id: int, rec: dict, sub: dict) -> dict | None:
        """Which segment adds the delay: home Wi-Fi, access line, the OLT/aggregation, or upstream?

        ΔQ splits a path's delay into G (the floor: distance and routing) and V (what queues add above it).
        The gateway measures both passively for the internet path and the LAN round trip per device. Comparing
        this home with the other homes on its OLT separates causes: V high here only = this home's access line
        or usage; V high on most of the OLT = shared congestion at the OLT or aggregation; G raised on many homes
        = a routing or peering change upstream; a slow LAN round trip with steady packets = Wi-Fi.
        """
        xp = rec.get("xp") or {}
        if not xp:
            return None
        ev, seg = [], "none"
        peers = []
        if sub.get("olt"):
            for r in self.db.q("SELECT l.rec FROM latest l JOIN gateways g ON g.id=l.gw JOIN subscribers s ON "
                               "s.id=g.subscriber_id WHERE s.olt=? AND g.id != ? AND l.t > ?", (sub["olt"], gw_id, now() - 900)):
                x = (jload(r["rec"], {}) or {}).get("xp") or {}
                if x.get("v99") is not None or x.get("g") is not None:
                    peers.append(x)
        pv = median([p["v99"] for p in peers if p.get("v99") is not None])
        pg = median([p["g"] for p in peers if p.get("g") is not None])
        v99, g = xp.get("v99"), xp.get("g")
        wifi = (xp.get("bottlenecks") or {}).get("wifi", 0)
        lan = xp.get("lan_rtt_p99_max")
        if g is not None:
            ev.append(f"Internet path floor G {g} ms" + (f" (OLT peers median {pg} ms)" if pg is not None else ""))
        if v99 is not None:
            ev.append(f"Queueing above the floor V p99 {v99} ms" + (f" (OLT peers median {pv} ms)" if pv is not None else ""))
        if lan is not None:
            ev.append(f"Slowest in-home round trip p99 {lan} ms; {wifi} device(s) limited by Wi-Fi")
        busy_peers = sum(1 for p in peers if (p.get("v99") or 0) > 30)
        if len(peers) >= 3 and busy_peers >= 0.5 * len(peers) and (v99 or 0) > 30:
            seg = "olt"
            summ = (f"Queueing delay is high on {busy_peers + 1} of {len(peers) + 1} homes on {sub['olt']}: shared congestion "
                    "at the OLT or aggregation, not this home.")
        elif g is not None and pg is not None and len(peers) >= 3 and g > pg + 20:
            seg = "upstream"
            summ = (f"This home's path floor is {round(g - pg)} ms above its OLT peers while queueing is normal: a routing or "
                    "peering difference upstream.")
        elif len(peers) >= 3 and v99 is not None and v99 > 30 and pv is not None and pv < 15:
            seg = "access"
            summ = ("Queueing delay is high on this home only while its OLT peers are fine: this home's access line or its own "
                    "heavy use.")
        elif wifi and (lan or 0) > 15:
            seg = "home-wifi"
            summ = (f"The internet path is healthy; {wifi} device(s) are limited by Wi-Fi (steady packets, {lan} ms round trip "
                    "inside the home).")
        elif v99 is None and g is None:
            seg, summ = "unknown", "Not enough passive delay samples yet to locate the cause."
        else:
            summ = "No segment adds notable delay right now."
        return {"segment": seg, "summary": summ, "evidence": ev, "peers": len(peers),
                "method": "ΔQ G/V decomposition (passive TCP timestamps) compared with homes on the same OLT"}

    # ================================================================== consumer app services
    def notify(self, sub_id: int, kind: str, title: str, body: str, ref: str = "") -> None:
        if ref and self.db.one("SELECT id FROM notifications WHERE subscriber_id=? AND ref=? AND title=?", (sub_id, ref, title)):
            return
        self.db.x("INSERT INTO notifications (subscriber_id, t, kind, title, body, ref) VALUES (?,?,?,?,?,?)",
                  (sub_id, now(), kind, title, body, ref))

    def app_gateway(self, sub_id: int) -> dict | None:
        return self.db.one("SELECT * FROM gateways WHERE subscriber_id=? ORDER BY id LIMIT 1", (sub_id,))

    def app_home(self, sub_id: int) -> dict:
        sub = self.db.one("SELECT s.*, p.name plan_name, p.down_mbit, p.up_mbit, p.code plan_code, p.boost_minutes, "
                          "p.price_inr FROM subscribers s LEFT JOIN plans p ON p.id=s.plan_id WHERE s.id=?", (sub_id,))
        g = self.app_gateway(sub_id)
        rec = jload(self.db.val("SELECT rec FROM latest WHERE gw=?", (g["id"],)), {}) if g else {}
        inc = self.db.one("SELECT id, kind, summary, opened, status FROM incidents WHERE scope=? AND status != 'resolved' "
                          "ORDER BY id DESC LIMIT 1", (sub["olt"],))
        lt = self.db.one("SELECT result, done FROM commands WHERE gw=? AND kind='line_test' AND status='done' ORDER BY id DESC LIMIT 1",
                         (g["id"],)) if g else None
        return {"subscriber": {"name": sub["name"], "account": sub["account"], "plan": sub["plan_name"], "plan_code": sub["plan_code"],
                               "down_mbit": sub["down_mbit"], "up_mbit": sub["up_mbit"], "boost_minutes": sub["boost_minutes"],
                               "area": sub["area"], "price_inr": sub["price_inr"], "since": sub["created"],
                               "renews": next_renewal(sub["created"] or now())},
                "gateway": {"online": bool(g and g["online"]), "last_seen": g["last_seen"] if g else None,
                            "serial": g["serial"] if g else None, "simulated": bool(g and g["simulated"])},
                "status": rec, "incident": inc,
                "line_test": {**jload(lt["result"], {}), "t": lt["done"]} if lt else None,
                "unread": self.db.val("SELECT COUNT(*) FROM notifications WHERE subscriber_id=? AND read=0", (sub_id,)),
                "brand": self.cfg.get("brand", {})}

    def set_consent(self, sub_id: int, changes: dict, actor: str) -> dict:
        sub = self.db.one("SELECT consent FROM subscribers WHERE id=?", (sub_id,))
        c = {**CONSENT_DEFAULT, **jload(sub["consent"], {})}
        for k in ("quality_improvement", "support_diagnostics", "marketing"):
            if k in changes:
                c[k] = bool(changes[k])
        c["updated"] = now()
        self.db.x("UPDATE subscribers SET consent=? WHERE id=?", (jdump(c), sub_id))
        g = self.app_gateway(sub_id)
        if g:
            lvl = "standard" if c["quality_improvement"] else "basic"
            self.enqueue(g["id"], "set_level", {"level": lvl}, f"consent change by {actor}")
        self.audit(actor, "subscriber", "consent_changed", str(sub_id), {k: c[k] for k in CONSENT_DEFAULT})
        return c

    def export_data(self, sub_id: int) -> dict:
        sub = self.db.one("SELECT * FROM subscribers WHERE id=?", (sub_id,))
        g = self.app_gateway(sub_id)
        tel = self.db.q("SELECT t, grade, p95_add_d, rate_d, ach_d FROM rollup5 WHERE gw=? ORDER BY t", (g["id"],)) if g else []
        return {"generated": now(), "subscriber": {k: sub[k] for k in ("account", "name", "phone", "email", "area", "created")},
                "consent": {**CONSENT_DEFAULT, **jload(sub["consent"], {})},
                "gateway": {k: g[k] for k in ("serial", "enrolled", "level")} if g else None,
                "quality_history_5min": tel,
                "tickets": self.db.q("SELECT id, category, status, created, summary FROM tickets WHERE subscriber_id=?", (sub_id,)),
                "notifications": self.db.q("SELECT t, title, body FROM notifications WHERE subscriber_id=?", (sub_id,)),
                "who_accessed_my_data": self.db.q("SELECT t, actor, actor_type, action FROM audit WHERE target IN (?, ?) "
                                                  "ORDER BY t DESC LIMIT 200", (str(sub_id), f"gw:{g['id']}" if g else "-"))}

    def create_ticket(self, sub_id: int, source: str, category: str, summary: str, include_diag: bool, actor: str) -> int:
        g = self.app_gateway(sub_id)
        service = category in ("plan_change",)          # a request, not a fault: no diagnosis or snapshot
        include_diag = include_diag and not service
        diag = self.diagnose(g["id"]) if g and not service else None
        snap = None
        if include_diag and g and g["online"] and not g["simulated"]:
            r = self.call(g["id"], "snapshot", {}, f"ticket by {actor}", timeout=10)
            snap = r.get("snapshot") if r.get("ok") else None
        tid = self.db.x("INSERT INTO tickets (subscriber_id, gw, source, category, status, created, updated, summary, diagnosis, "
                        "snapshot, thread) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sub_id, g["id"] if g else None, source, category, "open", now(), now(), summary, jdump(diag),
                         jdump(snap) if snap else None, jdump([{"t": now(), "from": actor, "text": summary}])))
        self.audit(actor, "subscriber" if source == "app" else "staff", "ticket_created", str(sub_id), {"ticket": tid})
        self.webhook("ticket.created", {"id": tid, "subscriber": sub_id, "category": category})
        if service:
            self.notify(sub_id, "ticket", f"Plan change request #{tid}",
                        "We'll call you to confirm the new plan and price. Nothing changes until you agree.", f"ticket:{tid}")
        else:
            self.notify(sub_id, "ticket", f"We received your report #{tid}",
                        CONSUMER_VERDICT.get((diag or {}).get("where"), "Our team will look into it.") if source == "app"
                        else "Support opened a ticket for you.", f"ticket:{tid}")
        return tid

    # ================================================================== integrations
    def webhook(self, event: str, payload: dict) -> None:
        hooks = [h for h in self.db.q("SELECT * FROM webhooks") if event in (h["events"] or "") or h["events"] == "*"]
        if not hooks:
            return

        def go():
            for hk in hooks:
                body = jdump({"event": event, "t": now(), "data": payload}).encode()
                sig = __import__("hmac").new(hk["secret"].encode(), body, hashlib.sha256).hexdigest()
                req = urllib.request.Request(hk["url"], data=body, method="POST",
                                             headers={"Content-Type": "application/json", "X-IQ-Webhook-Signature": sig})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        st = str(r.status)
                except Exception as e:  # noqa: BLE001
                    st = f"error: {e}"[:120]
                self.db.x("UPDATE webhooks SET last_status=? WHERE id=?", (st, hk["id"]))
        threading.Thread(target=go, daemon=True).start()


def next_renewal(since: float, t: float | None = None) -> float:
    """Next monthly renewal on the day of the month the connection started (28th at the latest)."""
    import datetime as dt
    t = time.time() if t is None else t
    d0 = dt.datetime.fromtimestamp(since)
    day = min(d0.day, 28)
    cur = dt.datetime.fromtimestamp(t)
    y, m = cur.year, cur.month
    cand = cur.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if cand <= cur:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        cand = cand.replace(year=y, month=m)
    return cand.timestamp()


def mask_phone(p: str | None) -> str | None:
    if not p:
        return p
    return p[:3] + "•" * max(0, len(p) - 7) + p[-4:]
