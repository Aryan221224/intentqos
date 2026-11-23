"""REST API + dashboard server (stdlib only).

GET  /                         operator console (intentqos/web/console.html)
GET  /classic                  the original single-page engineering dashboard
GET  /api/system               engine CPU/memory, versions, kernel capabilities, watchdog
GET  /api/setup-check          the setup guide's verification steps, run live
GET  /api/inspect              live `tc -s qdisc/class` and `nft list table` output
GET  /api/model                classifier training report (models/*.report.json)
GET  /api/modes | /api/schedules
PUT  /api/devices/<ip>         {"name":"Asha's laptop","type":"laptop"}
POST /api/modes/<id>/activate  {"target":"192.168.1.20","requester":"asha"[, "dry_run":true]}
POST /api/schedules            {"target":..,"days":[0,1,2,3,4],"start":"09:00","end":"17:30","goal":"low_latency"}
DELETE /api/schedules/<id>
POST /api/automation           {"paused":true|false}   hold the safe baseline / resume adaptation
POST /api/rollback             roll back to the last-known-good policy
GET  /api/state                full engine snapshot (JSON)
GET  /api/history?since=<t>    time series for charts
GET  /metrics                  Prometheus exposition
GET  /api/healthz              liveness
POST /api/intents[?dry_run=1]  {"target":"192.168.1.20","duration_s":1800,"goal":"low_latency","priority":2,
                                "requester":"asha","label":"work call"}   -> lease or reasoned rejection
DELETE /api/intents/<id>       cancel a lease
POST /api/flows/label          {"flow":"udp:192.168.1.20:5004-...","label":"video","scope":"app|flow"}
POST /api/classifier/mode      {"mode":"ml"|"rules"}
POST /api/safe                 operator kill-switch: go to the known-safe baseline now

Access control. With [api].require_auth on (the router package turns it on), every /api call
except /api/healthz and /api/session needs either `Authorization: Bearer <router key>` (the
agent, scripts) or a session cookie from POST /api/session {"key": "<router key>"} (the console in
a browser). The router key lives in [api].key_file (0600, created on first start, like the admin
password printed on a router's label). Without require_auth, only mutating calls are checked, and
only when the environment variable named in [api].token_env is set (the lab). Never commit a real
key; the docs use the obviously fake value "example-token-change-me".

GET  /api/session              {"auth_required": bool, "signed_in": bool}
POST /api/session              {"key": "..."} -> session cookie (12 h)
DELETE /api/session            sign out
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config as C
from . import console as K
from .metrics import prometheus

_HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(_HERE, "web", "console.html")
CLASSIC = os.path.join(_HERE, "web", "dashboard.html")
_KCAPS: dict | None = None


def kernel_caps() -> dict:
    """Which shaping building blocks this kernel offers. Probed once, on a throwaway veth pair, so the
    live shaper is never touched."""
    global _KCAPS
    if _KCAPS is None:
        caps = {}
        made = K._cmd(["ip", "link", "add", "iqprobe0", "type", "veth", "peer", "name", "iqprobe1"]) == ""
        for q in ("cake", "fq_codel", "netem", "htb", "fq"):
            if not made:
                caps[q] = None
                continue
            r = K._cmd(["tc", "qdisc", "replace", "dev", "iqprobe0", "root", q])
            caps[q] = r.strip() == ""
            K._cmd(["tc", "qdisc", "del", "dev", "iqprobe0", "root"])
        if made:
            K._cmd(["ip", "link", "del", "iqprobe0"])
        caps["ipv6"] = os.path.exists("/proc/net/if_inet6")
        caps["nftables"] = K._cmd(["nft", "-v"]).startswith("nftables")
        caps["ecn"] = _read("/proc/sys/net/ipv4/tcp_ecn")
        caps["congestion_control"] = _read("/proc/sys/net/ipv4/tcp_congestion_control")
        _KCAPS = caps
    return _KCAPS


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "?"


def resolve_target(eng, target: str) -> str:
    """Accept a device's friendly name as well as its IP."""
    t = (target or "").strip()
    for ip, name in eng.devices.items():
        if name and t.lower() == name.lower():
            return ip
    return t


def router_key(cfg: dict, create: bool = False) -> str:
    """The key that authorises API calls: the token env var, else the key file (created if asked)."""
    a = cfg["api"]
    tok = os.environ.get(a.get("token_env") or "", "")
    if tok:
        return tok
    path = a.get("key_file") or ""
    if not path:
        return ""
    path = C.resolve(cfg, path)
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        if not create:
            return ""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no 0/O/1/I: it gets typed from a label
    key = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    return key


class Sessions:
    def __init__(self, ttl: float = 12 * 3600):
        self.ttl, self.items = ttl, {}

    def new(self) -> str:
        sid = secrets.token_hex(24)
        self.items[sid] = time.time() + self.ttl
        return sid

    def valid(self, sid: str | None) -> bool:
        exp = self.items.get(sid or "")
        if exp and exp > time.time():
            return True
        self.items.pop(sid or "", None)
        return False

    def drop(self, sid: str | None) -> None:
        self.items.pop(sid or "", None)


def make_handler(eng):
    require = bool(eng.cfg["api"].get("require_auth"))
    token = router_key(eng.cfg, create=require)
    if require and not token:
        raise SystemExit("api.require_auth is on but no router key could be created (set api.key_file)")
    sessions = Sessions()
    if require and eng.cfg["api"].get("key_file"):
        eng.emit("api", f"router key in {C.resolve(eng.cfg, eng.cfg['api']['key_file'])} (needed to sign in to the console)", "info")

    class H(BaseHTTPRequestHandler):
        server_version = "IntentQoS"

        def log_message(self, *_a):
            pass

        def _send(self, code: int, body, ctype="application/json", headers=None):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body, default=str)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (headers or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > 65536:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _cookie(self) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "iqgw":
                    return v
            return None

        def _bearer_ok(self) -> bool:
            return bool(token) and hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def _authorised(self, write: bool = True) -> bool:
            if not require and not token:
                return True
            if not require and not write:
                return True
            if self._bearer_ok():
                return True
            if require and sessions.valid(self._cookie()):
                # cookie-authenticated writes must be JSON: a cross-site form cannot send that without CORS
                return not write or (self.headers.get("Content-Type") or "").startswith("application/json")
            return False

        def _session(self, method: str):
            if method == "GET":
                return self._send(200, {"auth_required": require, "signed_in": (not require) or self._bearer_ok()
                                        or sessions.valid(self._cookie())})
            if method == "DELETE":
                sessions.drop(self._cookie())
                return self._send(200, {"ok": True}, headers={"Set-Cookie": "iqgw=; Path=/; Max-Age=0"})
            try:
                body = self._json()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            given = str(body.get("key", "")).strip().upper()
            if not require:
                return self._send(200, {"ok": True})
            time.sleep(0.3)                               # slows guessing; the key has 32^16 values anyway
            if not hmac.compare_digest(given, token.upper()):
                eng.emit("api", f"console sign-in failed from {self.client_address[0]}", "warn")
                return self._send(401, {"error": "that key does not match the one on this router"})
            sid = sessions.new()
            return self._send(200, {"ok": True}, headers={
                "Set-Cookie": f"iqgw={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(sessions.ttl)}"})

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/session":
                return self._session("GET")
            if (u.path.startswith(("/api/", "/camara/")) or u.path == "/metrics") and u.path != "/api/healthz" \
                    and not self._authorised(write=False):
                return self._send(401, {"error": "sign in with the router key", "auth_required": True})
            if u.path.startswith("/fonts/"):
                name = os.path.basename(u.path)
                fp = os.path.join(os.path.dirname(WEB), "fonts", name)
                if name.endswith(".woff") and os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), "font/woff", headers={"Cache-Control": "public, max-age=604800"})
                return self._send(404, {"error": "not found"})
            if u.path in ("/", "/index.html", "/classic"):
                try:
                    with open(CLASSIC if u.path == "/classic" else WEB, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    return self._send(404, {"error": "console missing"})
            if u.path == "/api/system":
                return self._send(200, {"proc": eng.proc.sample(), "versions": K.versions(), "kernel": kernel_caps(),
                                        "watchdog": K.watchdog_status(eng.wd_path),
                                        "backend": eng.backend.name, "leaf": getattr(eng.backend, "leaf", None),
                                        "uptime_s": round(time.time() - eng.t_start, 1),
                                        "capture": eng.capture.stats(), "paused": eng.paused})
            if u.path == "/api/setup-check":
                return self._send(200, K.setup_check(eng))
            if u.path == "/api/inspect":
                return self._send(200, K.inspect(eng))
            if u.path == "/api/model":
                mp = eng.cfg["classifier"]["model_path"]
                rp = os.path.splitext(C.resolve(eng.cfg, mp))[0] + ".report.json"
                try:
                    with open(rp) as f:
                        return self._send(200, json.load(f))
                except (FileNotFoundError, ValueError):
                    return self._send(404, {"error": f"no model report at {rp}"})
            if u.path == "/api/modes":
                return self._send(200, K.MODES)
            if u.path == "/api/schedules":
                return self._send(200, eng.scheduler.items)
            if u.path == "/api/devices":
                return self._send(200, {"types": K.DEVICE_TYPES, "devices": eng.devreg.data})
            if u.path == "/api/usage":
                days = int(parse_qs(u.query).get("days", ["30"])[0])
                return self._send(200, eng.usage.summary(max(1, min(35, days))))
            if u.path == "/api/config":
                from .manage import MANAGED_KEYS
                return self._send(200, {"managed": eng.managed.doc,
                                        "allowed": {k: {"kind": v[0], "live": v[3]} for k, v in MANAGED_KEYS.items()}})
            if u.path == "/api/state":
                return self._send(200, eng.snapshot or {"starting": True})
            if u.path == "/api/history":
                since = float(parse_qs(u.query).get("since", ["0"])[0])
                return self._send(200, eng.history.since(since))
            if u.path == "/metrics":
                return self._send(200, prometheus(eng.snapshot or {}), "text/plain; version=0.0.4")
            if u.path == "/api/healthz":
                return self._send(200, {"ok": eng.running, "t": time.time()})
            if u.path == "/api/experience":
                snap = eng.snapshot or {}
                return self._send(200, {k: snap.get(k) for k in ("experience", "bottlenecks", "outcome", "l4s",
                                                                  "identity", "cellular", "probing")})
            if u.path == "/api/offers":
                return self._send(200, eng.offers.open())
            if u.path == "/api/usp":
                from .usp import data_model
                return self._send(200, data_model(eng))
            if u.path == "/camara/qos-profiles/v1/qos-profiles":
                from .camara import PROFILES, profile_doc
                return self._send(200, [profile_doc(n) for n in PROFILES])
            if u.path.startswith("/camara/qos-profiles/v1/qos-profiles/"):
                from .camara import PROFILES, profile_doc
                n = u.path.rsplit("/", 1)[-1]
                return self._send(200, profile_doc(n)) if n in PROFILES else \
                    self._send(404, {"status": 404, "code": "NOT_FOUND", "message": "no such QoS profile"})
            if u.path.startswith("/camara/quality-on-demand/v1/sessions/"):
                return self._camara("GET", u.path, {})
            return self._send(404, {"error": "not found"})

        def _camara(self, method: str, path: str, body: dict):
            from .camara import CamaraError
            q = eng.qod
            parts = path.rstrip("/").split("/")          # ['', 'camara', 'quality-on-demand', 'v1', ...]
            try:
                if parts[4:] == ["sessions"] and method == "POST":
                    return self._send(201, q.create(body, str(self.headers.get("X-Requester", "camara-app"))[:64]))
                if parts[4:] == ["retrieve-sessions"] and method == "POST":
                    return self._send(200, q.retrieve(body))
                if len(parts) == 6 and parts[4] == "sessions":
                    if method == "GET":
                        return self._send(200, q.get(parts[5]))
                    if method == "DELETE":
                        q.delete(parts[5])
                        return self._send(204, b"")
                if len(parts) == 7 and parts[4] == "sessions" and parts[6] == "extend" and method == "POST":
                    return self._send(200, q.extend(parts[5], body))
            except CamaraError as e:
                return self._send(e.status, e.body())
            return self._send(404, {"status": 404, "code": "NOT_FOUND", "message": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/session":
                return self._session("POST")
            if not self._authorised():
                return self._send(401, {"error": "sign in with the router key", "auth_required": True})
            try:
                body = self._json()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if u.path == "/api/intents":
                dry = parse_qs(u.query).get("dry_run", ["0"])[0] in ("1", "true")
                body["target"] = resolve_target(eng, body.get("target", ""))
                res = eng.intents.request(body, eng.estimator.down.rate, eng.estimator.up.rate, dry_run=dry)
                if res.get("ok"):
                    res["impact"] = impact_preview(eng, body.get("target"))
                return self._send(200 if res.get("ok") else 409, res)
            if u.path == "/api/flows/label":
                f = eng.find_flow(body.get("flow", ""))
                if f is None:
                    return self._send(404, {"error": "flow not active"})
                try:
                    return self._send(200, eng.classifier.correct(f, body.get("label"), body.get("scope", "app")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/classifier/mode":
                try:
                    eng.classifier.set_mode(body.get("mode", ""))
                    eng.cfg["classifier"]["mode"] = body["mode"]
                    return self._send(200, {"mode": eng.classifier.mode})
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/safe":
                eng.go_safe("operator requested the safe baseline")
                eng._log_apply(time.time(), "safe", None, eng.enforcer.applied, [], "operator: safe baseline now")
                return self._send(200, {"ok": True})
            if u.path == "/api/config":
                from .manage import apply_live
                try:
                    return self._send(200, apply_live(eng, body.get("set", {}), body.get("version"), body.get("profile"),
                                                      str(body.get("by", "api"))[:60]))
                except ValueError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
            if u.path == "/api/model/load":
                try:
                    return self._send(200, eng.load_model(str(body.get("path", ""))))
                except (OSError, ValueError) as e:
                    return self._send(400, {"ok": False, "error": str(e)})
            if u.path.startswith("/api/devices/") and u.path.endswith(("/pause", "/resume")):
                ip = u.path.split("/")[3]
                if not eng.table.is_lan(ip):
                    return self._send(400, {"error": "not a device on the home network"})
                who = str(body.get("by", "household"))[:40]
                if u.path.endswith("/pause"):
                    m = body.get("minutes")
                    return self._send(200, eng.pause_device(ip, float(m) if m else None, who))
                return self._send(200, eng.resume_device(ip, who))
            if u.path == "/api/rollback":
                return self._send(200, eng.operator_rollback())
            if u.path == "/api/bypass":
                try:
                    return self._send(200, eng.set_bypass(bool(body.get("on")), float(body.get("minutes", 5)),
                                                          str(body.get("by", "operator"))[:40]))
                except (TypeError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/automation":
                eng.set_paused(bool(body.get("paused")), str(body.get("requester", "operator"))[:40])
                return self._send(200, {"paused": eng.paused})
            if u.path.startswith("/api/modes/") and u.path.endswith("/activate"):
                mid = u.path.split("/")[3]
                mode = next((m for m in K.MODES if m["id"] == mid), None)
                if mode is None:
                    return self._send(404, {"error": f"unknown mode {mid}"})
                target = resolve_target(eng, body.get("target", ""))
                req = {"target": target, "goal": mode["goal"], "priority": mode["priority"],
                       "duration_s": float(body.get("duration_s", mode["duration_s"])),
                       "demand_down_kbit": mode["demand_down_kbit"], "demand_up_kbit": mode["demand_up_kbit"],
                       "label": mode["name"], "requester": body.get("requester", "household")}
                res = eng.intents.request(req, eng.estimator.down.rate, eng.estimator.up.rate,
                                          dry_run=bool(body.get("dry_run")))
                if res.get("ok"):
                    res["impact"] = impact_preview(eng, target)
                return self._send(200 if res.get("ok") else 409, res)
            if u.path == "/api/schedules":
                try:
                    body["target"] = resolve_target(eng, body.get("target", ""))
                    return self._send(200, eng.scheduler.add(body))
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": str(e)})
            if u.path.startswith("/camara/quality-on-demand/v1/"):
                return self._camara("POST", u.path, body)
            if u.path == "/api/intents/parse":
                # plain-language request -> a drafted lease; confirm=true then asks for it like any other request
                from .nlintent import parse
                r = parse(str(body.get("text", "")), eng.devices, body.get("from_ip") or self.client_address[0])
                if not r["ok"] or not body.get("confirm"):
                    if r["ok"]:
                        r["preview"] = eng.intents.request(dict(r["request"]), eng.estimator.down.rate,
                                                           eng.estimator.up.rate, dry_run=True)
                    return self._send(200 if r["ok"] else 400, r)
                req = dict(r["request"], requester=str(body.get("requester", "household"))[:64])
                res = eng.intents.request(req, eng.estimator.down.rate, eng.estimator.up.rate)
                res["understood"] = r["understood"]
                return self._send(200 if res.get("ok") else 409, res)
            if u.path.startswith("/api/offers/") and u.path.endswith(("/accept", "/dismiss")):
                oid = u.path.split("/")[3]
                if u.path.endswith("/dismiss"):
                    ok = eng.offers.dismiss(oid)
                    return self._send(200 if ok else 404, {"ok": ok})
                res = eng.offers.accept(oid, eng, str(body.get("requester", "household"))[:64], body.get("minutes"))
                return self._send(200 if res.get("ok") else 409, res)
            return self._send(404, {"error": "not found"})

        def do_PUT(self):
            u = urlparse(self.path)
            if not self._authorised():
                return self._send(401, {"error": "sign in with the router key", "auth_required": True})
            try:
                body = self._json()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if u.path.startswith("/api/devices/"):
                ip = u.path.rsplit("/", 1)[-1]
                try:
                    d = eng.devreg.set(ip, body.get("name"), body.get("type"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                eng.emit("device", f"{ip} renamed to '{d['name']}' ({d['type']})", "info")
                return self._send(200, d)
            return self._send(404, {"error": "not found"})

        def do_DELETE(self):
            u = urlparse(self.path)
            if u.path == "/api/session":
                return self._session("DELETE")
            if not self._authorised():
                return self._send(401, {"error": "sign in with the router key", "auth_required": True})
            if u.path.startswith("/api/intents/"):
                ok = eng.intents.cancel(u.path.rsplit("/", 1)[-1])
                return self._send(200 if ok else 404, {"ok": ok})
            if u.path.startswith("/api/schedules/"):
                ok = eng.scheduler.remove(u.path.rsplit("/", 1)[-1])
                return self._send(200 if ok else 404, {"ok": ok})
            if u.path.startswith("/camara/quality-on-demand/v1/sessions/"):
                return self._camara("DELETE", u.path, {})
            return self._send(404, {"error": "not found"})

    return H


def impact_preview(eng, target: str | None) -> list[dict]:
    """Who gives up guaranteed share when this lease is active (shown before confirming)."""
    out = []
    for d in (eng.snapshot or {}).get("devices", []):
        if d["ip"] == target:
            continue
        lab = max(d["labels"], key=d["labels"].get) if d["labels"] else "?"
        if lab in ("bulk", "streaming", "web") or d["kbps_dn"] > 1000:
            out.append({"device": d["name"] or d["ip"], "ip": d["ip"], "kbps_dn": d["kbps_dn"], "class": lab,
                        "effect": "keeps its floor and borrows only leftover capacity while the lease is active"})
    return out


def serve(eng) -> None:
    host, port = eng.cfg["api"]["listen"].rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(eng))
    srv.daemon_threads = True
    eng.emit("api", f"dashboard and API on http://{host}:{port}/", "info")
    srv.serve_forever()
