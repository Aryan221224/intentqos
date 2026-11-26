"""IntentQoS controller: HTTPS server for the router agents, the operator console, the consumer app and
northbound integrations.

    python3 -m controller.server --config controller/controller.toml

Routes
  /v1/agent/*     router agents (HMAC-signed requests from enrolled gateways)
  /v1/auth/*      staff sign-in (cookie session, RBAC: viewer, support, noc, admin)
  /v1/fleet, /v1/homes, /v1/alerts, /v1/incidents, /v1/profiles, /v1/models, /v1/rollouts, /v1/reports,
  /v1/admin/*     operator console API
  /v1/homes/<id>/relay/api/...   live per-home console, relayed to the router through its agent
  /v1/app/*       consumer app (phone + one-time code; the subscriber's own home only)
  /v1/nb/*        northbound API for OSS/BSS/CRM (API key)
  /ops/  /app/    the two web apps
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import security as S
from .core import CONSENT_DEFAULT, Controller, jdump, jload, mask_phone, now
from .kit import Kit, local_ip_for

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = {"/ops/": os.path.join(HERE, "web", "ops.html"), "/app/": os.path.join(HERE, "web", "app.html"),
       "/kit/": os.path.join(HERE, "web", "kit.html")}
PWA = {"/app/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
       "/app/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
       "/app/icon-192.png": ("icon-192.png", "image/png"), "/app/icon-512.png": ("icon-512.png", "image/png"),
       "/app/icon-maskable-512.png": ("icon-maskable-512.png", "image/png")}
# what phones and other home devices may open on the kit's plain-HTTP listener (everything else: this computer only)
LAN_OK = ("/app", "/app/", "/v1/app/", "/fonts/", "/healthz", "/v1/kit/where")
CONSOLE = os.path.join(ROOT, "intentqos", "web", "console.html")
SIM_CONTROL = "http://127.0.0.1:8095/scenario"
FONT_DIRS = (os.path.join(HERE, "web", "fonts"), os.path.join(ROOT, "intentqos", "web", "fonts"))
SUPPORT_RELAY_WRITE = ("/api/intents", "/api/devices/", "/api/modes/", "/api/schedules", "/api/flows/label")
DEFAULTS = {"listen": "0.0.0.0:8443", "tls_cert": "", "tls_key": "", "data_dir": "controller/data",
            "telemetry_interval_s": 15, "demo_otp": False, "lab_quick_login": False, "ui_listen": "", "auto_notify_outages": True, "test_server": "",
            "kit": {"enabled": False},
            "brand": {"name": "SampleNet Fibre", "short": "SampleNet", "support_phone": "1800-000-000",
                      "grievance_email": "grievance@samplenet.example", "accent": "#3987e5"},
            "retention": {"raw_hours": 48, "rollup_days": 90, "events_days": 90}}


class Lockout:
    """Brute-force guard for staff sign-in: 5 failures within 15 minutes, per user name or per client
    address, block further attempts for 15 minutes."""

    def __init__(self, limit: int = 5, window: float = 900.0):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str, t: float) -> list[float]:
        xs = [x for x in self.fails.get(key, []) if t - x < self.window]
        self.fails[key] = xs
        return xs

    def blocked(self, user: str, ip: str) -> int:
        t = time.time()
        with self.lock:
            worst = 0
            for k in (f"u:{user}", f"ip:{ip}"):
                xs = self._recent(k, t)
                if len(xs) >= self.limit:
                    worst = max(worst, int(self.window - (t - xs[-self.limit])))
            return worst

    def fail(self, user: str, ip: str) -> None:
        t = time.time()
        with self.lock:
            for k in (f"u:{user}", f"ip:{ip}"):
                self._recent(k, t).append(t)

    def clear(self, user: str, ip: str) -> None:
        with self.lock:
            self.fails.pop(f"u:{user}", None)
            self.fails.pop(f"ip:{ip}", None)


LOCKOUT = Lockout()


class HTTPError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def make_handler(ctl: Controller):
    db = ctl.db

    class H(BaseHTTPRequestHandler):
        server_version = "IntentQoS-controller"
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        # ------------------------------------------------------------ plumbing
        def _send(self, code: int, body, ctype="application/json", headers=None):
            if not isinstance(body, (bytes, str)):
                body = jdump(body)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (headers or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _raw(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4 << 20:
                raise HTTPError(413, "request too large")
            return self.rfile.read(n) if n > 0 else b""

        def _cookie(self, name: str) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return None

        def _ip(self) -> str:
            return self.client_address[0]

        def _secure(self) -> str:
            """Cookies carry Secure on the HTTPS listener; the loopback HTTP listener cannot set it."""
            return "" if getattr(self.server, "plain_http", False) else " Secure;"

        def _loopback(self) -> bool:
            ip = self._ip()
            return ip.startswith("127.") or ip in ("::1", "::ffff:127.0.0.1")

        def _quick_ok(self) -> bool:
            """Lab quick sign-in: only when the config enables it AND the browser is on this machine."""
            return bool(ctl.cfg.get("lab_quick_login")) and self._loopback()

        def _staff(self, perm: str = "read") -> dict:
            tok = self._cookie("iqops")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                tok = auth[7:]
            s = db.one("SELECT * FROM sessions WHERE token=? AND kind='staff' AND expires > ?", (S.h(tok or ""), now()))
            if not s:
                raise HTTPError(401, "sign in required")
            u = db.one("SELECT id, username, name, role, disabled FROM users WHERE id=?", (s["principal"],))
            if not u or u["disabled"]:
                raise HTTPError(401, "account disabled")
            if perm not in S.ROLES.get(u["role"], set()):
                raise HTTPError(403, f"your role ({u['role']}) cannot do this")
            return u

        def _app(self) -> dict:
            tok = self._cookie("iqapp")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                tok = auth[7:]
            s = db.one("SELECT * FROM sessions WHERE token=? AND kind='app' AND expires > ?", (S.h(tok or ""), now()))
            if not s:
                raise HTTPError(401, "please sign in")
            sub = db.one("SELECT * FROM subscribers WHERE id=?", (s["principal"],))
            if not sub:
                raise HTTPError(401, "account not found")
            return sub

        def _nb(self) -> dict:
            auth = self.headers.get("Authorization", "")
            key = auth[7:] if auth.startswith("ApiKey ") else ""
            k = db.one("SELECT * FROM apikeys WHERE key_hash=?", (S.h(key),))
            if not k:
                raise HTTPError(401, "API key required")
            return k

        def _csrf(self) -> None:
            if self.command in ("POST", "PUT", "DELETE") and not self.headers.get("Authorization"):
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    raise HTTPError(415, "JSON requests only")

        # ------------------------------------------------------------ dispatch
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method: str):
            u = urlparse(self.path)
            p = u.path
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                raw = self._raw() if method in ("POST", "PUT", "DELETE") else b""
                if getattr(self.server, "plain_http", False) and not self._loopback() \
                        and not any(p == x or (x.endswith("/") and p.startswith(x)) for x in LAN_OK):
                    raise HTTPError(403, "this page is only available on the Operator Console computer; "
                                         "phones use /app/")
                if p.startswith("/v1/agent/"):
                    return self._agent(method, p, qs, raw)
                body = json.loads(raw or b"{}") if raw else {}
                if p in PWA and method == "GET":
                    fn, ctype = PWA[p]
                    with open(os.path.join(HERE, "web", "pwa", fn), "rb") as f:
                        return self._send(200, f.read(), ctype, {"Cache-Control": "no-cache"})
                if p == "/kit":
                    return self._send(302, b"", headers={"Location": "/kit/"})
                if p == "/kit/" and not self._loopback():
                    raise HTTPError(403, "the kit page is only available on the Operator Console computer")
                if p == "/kit/qrcode.js" and method == "GET":
                    with open(os.path.join(HERE, "web", "vendor", "qrcode.js"), "rb") as f:
                        return self._send(200, f.read(), "text/javascript; charset=utf-8", {"Cache-Control": "max-age=86400"})
                if p.startswith("/v1/kit/"):
                    return self._kitapi(method, p, qs, body)
                if p.startswith("/v1/"):
                    self._csrf()
                if p in ("/", "/ops"):
                    return self._send(302, b"", headers={"Location": "/ops/"})
                if p == "/app":
                    return self._send(302, b"", headers={"Location": "/app/"})
                if p in WEB and method == "GET":
                    with open(WEB[p], "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8",
                                          {"Content-Security-Policy": "frame-ancestors 'self'"})
                if p == "/ops/home-console.html" and method == "GET":
                    self._staff("relay_read")
                    gid = int(qs.get("gw", "0"))
                    with open(CONSOLE, encoding="utf-8") as f:
                        html = f.read()
                    inject = (f'<script>window.IQ_BASE="/v1/homes/{gid}/relay";window.IQ_REMOTE='
                              f'{jdump({"gw": gid, "home": (ctl.home(gid) or {}).get("subscriber", {}).get("account")})};</script>\n')
                    html = html.replace("<script>\n\"use strict\";", inject + "<script>\n\"use strict\";", 1)
                    return self._send(200, html, "text/html; charset=utf-8")
                if p.startswith("/fonts/") and method == "GET":
                    name = os.path.basename(p)
                    for d in FONT_DIRS:
                        fp = os.path.join(d, name)
                        if name.endswith(".woff") and os.path.isfile(fp):
                            with open(fp, "rb") as f:
                                return self._send(200, f.read(), "font/woff",
                                                  {"Cache-Control": "public, max-age=604800, immutable"})
                    raise HTTPError(404, "not found")
                if p == "/healthz":
                    return self._send(200, {"ok": True, "t": now()})
                if p.startswith("/v1/auth/"):
                    return self._auth(method, p, body)
                if p.startswith("/v1/app/"):
                    return self._appapi(method, p, qs, body)
                if p.startswith("/v1/nb/"):
                    return self._nbapi(method, p, qs, body)
                if p.startswith("/camara/"):
                    return self._camara_nb(method, p, body)
                if p.startswith("/v1/"):
                    return self._ops(method, p, qs, body)
                return self._send(404, {"error": "not found"})
            except HTTPError as e:
                return self._send(e.code, {"error": str(e)})
            except PermissionError as e:
                return self._send(401, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})
            except BrokenPipeError:
                return None

        # ------------------------------------------------------------ agents
        def _agent(self, method, p, qs, raw):
            if p == "/v1/agent/enroll" and method == "POST":
                return self._send(200, ctl.enroll(json.loads(raw), self._ip()))
            g = ctl.auth_agent(self.headers, method, self.path, raw)
            if p == "/v1/agent/sync" and method == "POST":
                return self._send(200, ctl.sync(g, json.loads(raw)))
            if p == "/v1/agent/wait" and method == "GET":
                return self._send(200, ctl.wait(g, float(qs.get("timeout", 25))))
            if p == "/v1/agent/result" and method == "POST":
                ctl.store_results(g, json.loads(raw).get("results", []))
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/v1/agent/blob/([0-9a-f]{64})", p)
            if m and method == "GET":
                mod = db.one("SELECT path FROM models WHERE sha256=?", (m.group(1),))
                if not mod:
                    raise HTTPError(404, "no such blob")
                with open(mod["path"], "rb") as f:
                    return self._send(200, f.read(), "application/octet-stream")
            raise HTTPError(404, "not found")

        # ------------------------------------------------------------ staff auth
        def _auth(self, method, p, body):
            if p == "/v1/auth/login" and method == "POST":
                uname = str(body.get("username", ""))[:64]
                wait = LOCKOUT.blocked(uname, self._ip())
                if wait:
                    raise HTTPError(429, f"too many failed sign-ins; try again in {wait // 60 + 1} min")
                u = db.one("SELECT * FROM users WHERE username=?", (uname,))
                if not u or u["disabled"] or not S.check_pw(str(body.get("password", "")), u["pw"]):
                    time.sleep(0.4)
                    LOCKOUT.fail(uname, self._ip())
                    ctl.audit(uname[:40], "staff", "login_failed", "", {}, self._ip())
                    raise HTTPError(401, "wrong username or password")
                LOCKOUT.clear(uname, self._ip())
                tok = S.token()
                db.x("INSERT INTO sessions (token, kind, principal, expires, created) VALUES (?,?,?,?,?)",
                     (S.h(tok), "staff", u["id"], now() + 12 * 3600, now()))
                ctl.audit(u["username"], "staff", "login", "", {}, self._ip())
                return self._send(200, {"ok": True, "user": {k: u[k] for k in ("username", "name", "role")}},
                                  headers={"Set-Cookie": f"iqops={tok}; Path=/; HttpOnly;{self._secure()} SameSite=Strict; Max-Age=43200"})
            # lab only: pick a role instead of typing a password (set lab_quick_login = false in production)
            if p == "/v1/auth/quick-roles":
                rows = db.q("SELECT username, name, role FROM users WHERE disabled=0 ORDER BY id") if self._quick_ok() else []
                return self._send(200, {"enabled": bool(rows), "users": rows})
            if p == "/v1/auth/quick" and method == "POST":
                u = db.one("SELECT * FROM users WHERE username=? AND disabled=0", (str(body.get("username", "")),)) if self._quick_ok() else None
                if not u:
                    raise HTTPError(403, "quick sign-in is not enabled")
                tok = S.token()
                db.x("INSERT INTO sessions (token, kind, principal, expires, created) VALUES (?,?,?,?,?)",
                     (S.h(tok), "staff", u["id"], now() + 12 * 3600, now()))
                ctl.audit(u["username"], "staff", "login_quick", "", {}, self._ip())
                return self._send(200, {"ok": True, "user": {k: u[k] for k in ("username", "name", "role")}},
                                  headers={"Set-Cookie": f"iqops={tok}; Path=/; HttpOnly;{self._secure()} SameSite=Strict; Max-Age=43200"})
            if p == "/v1/auth/logout" and method == "POST":
                tok = self._cookie("iqops")
                if tok:
                    db.x("DELETE FROM sessions WHERE token=?", (S.h(tok),))
                return self._send(200, {"ok": True}, headers={"Set-Cookie": "iqops=; Path=/; Max-Age=0"})
            if p == "/v1/auth/me":
                u = self._staff()
                return self._send(200, {"user": u, "perms": sorted(S.ROLES[u["role"]]), "brand": ctl.cfg["brand"],
                                        "lab": bool(ctl.cfg.get("demo_otp") or ctl.cfg.get("lab_quick_login"))})
            raise HTTPError(404, "not found")

        # ------------------------------------------------------------ operator console API
        def _ops(self, method, p, qs, body):
            # lab only: drive the simulated fleet (an OLT congestion or outage) from the console
            if p == "/v1/lab/scenario":
                if not (ctl.cfg.get("demo_otp") or ctl.cfg.get("lab_quick_login")):
                    raise HTTPError(404, "not found")
                u = self._staff("read" if method == "GET" else "incidents")
                op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    if method == "GET":
                        with op.open(SIM_CONTROL, timeout=3) as r:
                            return self._send(200, json.loads(r.read()))
                    kind = str(body.get("kind", ""))
                    if kind not in ("olt_congestion", "olt_outage", "clear"):
                        raise HTTPError(400, "kind must be olt_congestion, olt_outage or clear")
                    req = {"kind": kind, "olt": str(body.get("olt", ""))[:20], "minutes": min(30.0, float(body.get("minutes", 8))),
                           "severity": 0.35}
                    with op.open(urllib.request.Request(SIM_CONTROL, data=json.dumps(req).encode(), method="POST",
                                                        headers={"Content-Type": "application/json"}), timeout=3) as r:
                        out = json.loads(r.read())
                    ctl.audit(u["username"], "staff", "lab_scenario", req["olt"] or "fleet", req, self._ip())
                    return self._send(200, out)
                except OSError:
                    raise HTTPError(503, "the simulated fleet is not running")
            # live per-home console relayed through the agent
            m = re.fullmatch(r"/v1/homes/(\d+)/relay(/api/.*)", p)
            if m:
                gid, path = int(m.group(1)), m.group(2)
                if method == "GET":
                    u = self._staff("relay_read")
                else:
                    u = self._staff("home_actions")
                    if "relay_write" not in S.ROLES[u["role"]] and not path.startswith(SUPPORT_RELAY_WRITE):
                        raise HTTPError(403, "support agents can change devices and priorities only; ask the NOC")
                full = path + (("?" + urlparse(self.path).query) if urlparse(self.path).query else "")
                r = ctl.relay(gid, method, full, body if method != "GET" else None, u["username"])
                if method != "GET":
                    ctl.audit(u["username"], "staff", "relay", f"gw:{gid}", {"method": method, "path": path, "body": body}, self._ip())
                elif path == "/api/state" and ctl.seen.get((u["id"], gid), 0) < now() - 3600:
                    ctl.seen[(u["id"], gid)] = now()           # one audit entry per staff member, home and hour
                    ctl.audit(u["username"], "staff", "viewed_live_console", f"gw:{gid}", {}, self._ip())
                if r.get("offline"):
                    raise HTTPError(503, "gateway is offline")
                if not r.get("ok", True) and "status" not in r:
                    raise HTTPError(504, r.get("error", "relay failed"))
                return self._send(r.get("status", 200), r.get("body"))
            u = self._staff("read")
            if p == "/v1/fleet/summary":
                return self._send(200, ctl.fleet_summary())
            if p == "/v1/fleet/trend":
                return self._send(200, ctl.fleet_trend(float(qs.get("hours", 6))))
            if p == "/v1/fleet/qoo-private":
                return self._send(200, ctl.fleet_private_qoo(float(qs.get("hours", 1))))
            if p == "/v1/camara/sessions":
                return self._send(200, {"sessions": ctl.camara.list(),
                                        "access_network": "simulated" if getattr(ctl.camara.net, "simulated", False) else "live"})
            if p == "/v1/fleet/olts":
                rows = db.q("SELECT s.region, s.pop, s.olt, COUNT(*) homes, SUM(g.online) online FROM gateways g JOIN subscribers s "
                            "ON s.id=g.subscriber_id WHERE g.device_id IS NOT NULL GROUP BY s.olt ORDER BY s.region, s.olt")
                al = {}
                for a in db.q("SELECT s.olt, COUNT(DISTINCT a.gw) n FROM alerts a JOIN gateways g ON g.id=a.gw JOIN subscribers s "
                              "ON s.id=g.subscriber_id WHERE a.status IN ('open','ack') AND a.rule IN ('capacity_shortfall','qoe_poor','offline') "
                              "GROUP BY s.olt"):
                    al[a["olt"]] = a["n"]
                for r in rows:
                    r["affected"] = al.get(r["olt"], 0)
                return self._send(200, rows)
            if p == "/v1/homes" and method == "GET":
                return self._send(200, ctl.homes(qs.get("q", ""), qs.get("filter", ""), int(qs.get("limit", 200))))
            m = re.fullmatch(r"/v1/homes/(\d+)(/[a-z_-]+)?", p)
            if m:
                gid, sub = int(m.group(1)), m.group(2) or ""
                g = ctl.gw(gid)
                if not g:
                    raise HTTPError(404, "no such home")
                if sub == "" and method == "GET":
                    ctl.audit(u["username"], "staff", "viewed_home", f"gw:{gid}", {}, self._ip())
                    return self._send(200, ctl.home(gid))
                if sub == "/timeline":
                    return self._send(200, ctl.timeline(gid, float(qs.get("hours", 2))))
                if sub == "/diagnose":
                    return self._send(200, ctl.diagnose(gid))
                if sub == "/notes" and method == "POST":
                    self._staff("notes")
                    sid = g["subscriber_id"]
                    db.x("INSERT INTO notes (subscriber_id, author, t, text) VALUES (?,?,?,?)", (sid, u["username"], now(),
                                                                                                 str(body.get("text", ""))[:2000]))
                    return self._send(200, {"ok": True})
                if sub == "/command" and method == "POST":
                    return self._send(200, self._home_command(u, g, body))
                if sub == "/ticket" and method == "POST":
                    self._staff("tickets")
                    tid = ctl.create_ticket(g["subscriber_id"], "support", body.get("category", "quality"),
                                            str(body.get("summary", ""))[:1000], bool(body.get("diagnostics", True)), u["username"])
                    return self._send(200, {"ok": True, "ticket": tid})
            if p == "/v1/alerts":
                st = qs.get("status", "open")
                rows = db.q("SELECT a.*, s.account, s.name, s.olt, g.serial FROM alerts a JOIN gateways g ON g.id=a.gw JOIN subscribers s "
                            "ON s.id=g.subscriber_id WHERE " + ("a.status IN ('open','ack')" if st == "open" else "1=1")
                            + " ORDER BY CASE a.severity WHEN 'major' THEN 0 WHEN 'minor' THEN 1 ELSE 2 END, a.opened DESC LIMIT 300")
                return self._send(200, rows)
            m = re.fullmatch(r"/v1/alerts/(\d+)/ack", p)
            if m and method == "POST":
                self._staff("home_actions")
                db.x("UPDATE alerts SET status='ack', ack_by=? WHERE id=?", (u["username"], int(m.group(1))))
                ctl.audit(u["username"], "staff", "alert_ack", m.group(1), {}, self._ip())
                return self._send(200, {"ok": True})
            if p == "/v1/incidents":
                rows = db.q("SELECT * FROM incidents ORDER BY CASE WHEN status='resolved' THEN 1 ELSE 0 END, opened DESC LIMIT 100")
                return self._send(200, rows)
            m = re.fullmatch(r"/v1/incidents/(\d+)/(notify|status|note)", p)
            if m and method == "POST":
                iid, act = int(m.group(1)), m.group(2)
                if act == "notify":
                    self._staff("notify")
                    return self._send(200, {"ok": True, "notified": ctl.notify_incident(iid, u["username"])})
                self._staff("incidents")
                inc = db.one("SELECT * FROM incidents WHERE id=?", (iid,))
                if act == "status":
                    stt = body.get("status")
                    if stt not in ("open", "investigating", "identified", "resolved"):
                        raise HTTPError(400, "bad status")
                    db.x("UPDATE incidents SET status=?, owner=?, updated=?, closed=? WHERE id=?",
                         (stt, u["username"], now(), now() if stt == "resolved" else None, iid))
                else:
                    notes = jload(inc["notes"], [])
                    notes.append({"t": now(), "by": u["username"], "text": str(body.get("text", ""))[:1000]})
                    db.x("UPDATE incidents SET notes=?, updated=? WHERE id=?", (jdump(notes), now(), iid))
                ctl.audit(u["username"], "staff", f"incident_{act}", str(iid), body, self._ip())
                return self._send(200, {"ok": True})
            if p == "/v1/profiles":
                if method == "POST":
                    self._staff("profiles")
                    from intentqos.manage import validate
                    st = validate(body.get("settings", {}))
                    name = re.sub(r"[^a-z0-9-]", "", str(body.get("name", "")).lower())[:40]
                    if not name:
                        raise HTTPError(400, "profile name required (a-z, 0-9, -)")
                    ver = (db.val("SELECT MAX(version) FROM profiles WHERE name=?", (name,)) or 0) + 1
                    pid = db.x("INSERT INTO profiles (name, version, settings, description, created_by, created) VALUES (?,?,?,?,?,?)",
                               (name, ver, jdump(st), str(body.get("description", ""))[:300], u["username"], now()))
                    ctl.audit(u["username"], "staff", "profile_created", f"{name} v{ver}", st, self._ip())
                    return self._send(200, {"ok": True, "id": pid, "version": ver})
                rows = db.q("SELECT p.*, (SELECT COUNT(*) FROM gateways g WHERE g.profile_id=p.id) homes FROM profiles p "
                            "ORDER BY name, version DESC")
                from intentqos.manage import MANAGED_KEYS
                return self._send(200, {"profiles": [{**r, "settings": jload(r["settings"], {})} for r in rows],
                                        "keys": {k: {"kind": v[0], "lo": v[1] if not isinstance(v[1], tuple) else list(v[1]),
                                                     "hi": v[2], "live": v[3]} for k, v in MANAGED_KEYS.items()}})
            if p == "/v1/models":
                return self._send(200, [{**r, "metrics": jload(r["metrics"], {}), "path": None, "signature": r["signature"][:16] + "…",
                                         "homes": db.val("SELECT COUNT(*) FROM gateways WHERE model_version=?", (r["version"],))}
                                        for r in db.q("SELECT * FROM models ORDER BY created DESC")])
            if p == "/v1/rollouts":
                if method == "POST":
                    self._staff("rollouts")
                    stages = body.get("stages") or [{"pct": 5, "min_minutes": 10}, {"pct": 25, "min_minutes": 10}, {"pct": 100, "min_minutes": 10}]
                    rid = ctl.create_rollout(body["kind"], str(body["target"]), str(body.get("name", ""))[:80] or f"{body['kind']} {body['target']}",
                                             stages, u["username"], body.get("guardrails"))
                    return self._send(200, {"ok": True, "id": rid})
                rows = db.q("SELECT * FROM rollouts ORDER BY id DESC LIMIT 30")
                out = []
                for r in rows:
                    r = {**r, "stages": jload(r["stages"], []), "guardrails": jload(r["guardrails"], {}),
                         "decisions": jload(r["decisions"], []), "previous": None,
                         "members": db.q("SELECT stage, status, COUNT(*) n FROM rollout_members WHERE rollout_id=? GROUP BY stage, status",
                                         (r["id"],))}
                    if r["status"] in ("running", "paused", "halted"):
                        r["eval"] = ctl.rollout_eval(r["id"])
                    out.append(r)
                return self._send(200, out)
            m = re.fullmatch(r"/v1/rollouts/(\d+)/(halt|resume|rollback|advance)", p)
            if m and method == "POST":
                self._staff("rollouts")
                rid, act = int(m.group(1)), m.group(2)
                r = db.one("SELECT * FROM rollouts WHERE id=?", (rid,))
                dec = jload(r["decisions"], [])
                if act == "halt":
                    db.x("UPDATE rollouts SET status='paused' WHERE id=?", (rid,))
                elif act == "resume":
                    db.x("UPDATE rollouts SET status='running', stage_started=? WHERE id=?", (now(), rid))
                elif act == "advance":
                    st = jload(r["stages"], [])
                    if r["stage"] + 1 < len(st):
                        dec.append({"t": now(), "stage": r["stage"], "action": "advanced", "by": u["username"]})
                        db.x("UPDATE rollouts SET decisions=? WHERE id=?", (jdump(dec), rid))
                        ctl._assign(rid, r["stage"] + 1)
                else:
                    ctl.rollback_rollout(rid, u["username"])
                ctl.audit(u["username"], "staff", f"rollout_{act}", r["name"], {}, self._ip())
                return self._send(200, {"ok": True})
            if p == "/v1/reports/qoe":
                grp = qs.get("by", "region")
                col = {"region": "s.region", "olt": "s.olt", "plan": "p.code", "pop": "s.pop"}.get(grp, "s.region")
                hrs = float(qs.get("hours", 24))
                rows = db.q(f"SELECT {col} grp, COUNT(DISTINCT r.gw) homes, COUNT(*) n, AVG(r.p95_add_d) p95_mean, "
                            "SUM(CASE WHEN r.p95_add_d < 50 THEN 1 ELSE 0 END) lat_ok, SUM(CASE WHEN r.jit_d < 40 OR r.jit_d IS NULL THEN 1 ELSE 0 END) jit_ok, "
                            "SUM(CASE WHEN r.grade IN ('C','D','F') THEN 1 ELSE 0 END) poor, AVG(r.ach_d / NULLIF(r.plan_d,0)) util "
                            f"FROM rollup5 r JOIN gateways g ON g.id=r.gw JOIN subscribers s ON s.id=g.subscriber_id LEFT JOIN plans p "
                            f"ON p.id=s.plan_id WHERE r.t >= ? GROUP BY {col} ORDER BY grp", (now() - hrs * 3600,))
                for r in rows:
                    n = max(1, r["n"])
                    r.update(lat_ok_pct=round(100 * r.pop("lat_ok") / n, 1), jit_ok_pct=round(100 * r.pop("jit_ok") / n, 1),
                             poor_pct=round(100 * r.pop("poor") / n, 1), p95_mean=round(r["p95_mean"] or 0, 1),
                             util=round(100 * (r["util"] or 0), 1))
                if qs.get("format") == "csv":
                    hdr = ["grp", "homes", "n", "p95_mean", "lat_ok_pct", "jit_ok_pct", "poor_pct", "util"]
                    csv = ",".join(hdr) + "\n" + "\n".join(",".join(str(r.get(h, "")) for h in hdr) for r in rows)
                    return self._send(200, csv, "text/csv", {"Content-Disposition": f"attachment; filename=qoe-by-{grp}.csv"})
                return self._send(200, rows)
            if p == "/v1/tickets":
                rows = db.q("SELECT t.id, t.category, t.status, t.created, t.updated, t.summary, t.source, t.gw, s.account, s.name "
                            "FROM tickets t JOIN subscribers s ON s.id=t.subscriber_id ORDER BY t.id DESC LIMIT 200")
                return self._send(200, rows)
            m = re.fullmatch(r"/v1/tickets/(\d+)", p)
            if m:
                t = db.one("SELECT * FROM tickets WHERE id=?", (int(m.group(1)),))
                if method == "POST":
                    self._staff("tickets")
                    th = jload(t["thread"], [])
                    if body.get("reply"):
                        th.append({"t": now(), "from": u["name"], "text": str(body["reply"])[:2000]})
                        ctl.notify(t["subscriber_id"], "ticket", f"Update on your report #{t['id']}", str(body["reply"])[:300], f"ticket:{t['id']}:{len(th)}")
                    db.x("UPDATE tickets SET status=COALESCE(?, status), thread=?, updated=?, assignee=? WHERE id=?",
                         (body.get("status"), jdump(th), now(), u["username"], t["id"]))
                    ctl.audit(u["username"], "staff", "ticket_updated", str(t["id"]), {"status": body.get("status")}, self._ip())
                    return self._send(200, {"ok": True})
                return self._send(200, {**t, "diagnosis": jload(t["diagnosis"]), "snapshot": jload(t["snapshot"]),
                                        "thread": jload(t["thread"], [])})
            if p == "/v1/audit":
                return self._send(200, db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(qs.get("limit", 200)),)))
            if p == "/v1/privacy/requests":
                if method == "GET":
                    return self._send(200, db.q("SELECT r.*, s.account, s.name FROM privacy_requests r JOIN subscribers s ON s.id=r.subscriber_id "
                                                "ORDER BY r.id DESC"))
            m = re.fullmatch(r"/v1/privacy/requests/(\d+)/close", p)
            if m and method == "POST":
                self._staff("privacy")
                db.x("UPDATE privacy_requests SET status='closed', closed=? WHERE id=?", (now(), int(m.group(1))))
                ctl.audit(u["username"], "staff", "privacy_request_closed", m.group(1), {}, self._ip())
                return self._send(200, {"ok": True})
            if p == "/v1/admin/users":
                self._staff("users")
                if method == "POST":
                    if body.get("role") not in S.ROLES:
                        raise HTTPError(400, "bad role")
                    db.x("INSERT INTO users (username, name, role, pw, created) VALUES (?,?,?,?,?)",
                         (body["username"], body.get("name", body["username"]), body["role"], S.hash_pw(body["password"]), now()))
                    ctl.audit(u["username"], "staff", "user_created", body["username"], {"role": body["role"]}, self._ip())
                    return self._send(200, {"ok": True})
                return self._send(200, db.q("SELECT id, username, name, role, created, disabled FROM users"))
            if p == "/v1/admin/webhooks":
                self._staff("integrations")
                if method == "POST":
                    db.x("INSERT INTO webhooks (url, events, secret, created) VALUES (?,?,?,?)",
                         (body["url"], body.get("events", "*"), S.token(16), now()))
                    ctl.audit(u["username"], "staff", "webhook_added", body["url"], {}, self._ip())
                    return self._send(200, {"ok": True})
                return self._send(200, [{**w, "secret": "••••"} for w in db.q("SELECT * FROM webhooks")])
            if p == "/v1/admin/system":
                return self._send(200, {"release_pubkey": ctl.key.pub_hex, "uptime_s": round(now() - ctl.started),
                                        "telemetry_rows": db.val("SELECT COUNT(*) FROM telemetry"),
                                        "db_bytes": os.path.getsize(db.path), "interval_s": ctl.interval(),
                                        "retention": ctl.cfg["retention"], "brand": ctl.cfg["brand"]})
            raise HTTPError(404, "not found")

        def _home_command(self, u, g, body):
            kind = body.get("kind")
            gid = g["id"]
            if kind == "line_test":
                self._staff("line_test")
                if not ctl.cfg.get("test_server"):
                    raise HTTPError(400, "no test server configured")
                ctl.audit(u["username"], "staff", "line_test", f"gw:{gid}", {}, self._ip())
                return ctl.call(gid, "line_test", {"url": ctl.cfg["test_server"], "seconds": 8}, u["username"], timeout=30)
            if kind in ("safe", "rollback", "pause_automation", "resume_automation"):
                self._staff("relay_write")
                path, b = {"safe": ("/api/safe", {}), "rollback": ("/api/rollback", {}),
                           "pause_automation": ("/api/automation", {"paused": True}),
                           "resume_automation": ("/api/automation", {"paused": False})}[kind]
                ctl.audit(u["username"], "staff", kind, f"gw:{gid}", {}, self._ip())
                return ctl.relay(gid, "POST", path, b, u["username"])
            if kind == "push_config":
                self._staff("home_actions")
                ctl.audit(u["username"], "staff", "push_config", f"gw:{gid}", {}, self._ip())
                return {"ok": True, "command": ctl.push_config(gid, u["username"])}
            if kind == "request_diagnostics":
                self._staff("home_actions")
                sub = ctl.sub_of(g)
                consent = {**CONSENT_DEFAULT, **jload(sub.get("consent"), {})}
                if not consent.get("support_diagnostics"):
                    raise HTTPError(409, "the subscriber has turned off support diagnostics in their app")
                ctl.notify(sub["id"], "consent", "Support asks for 60 minutes of detailed diagnostics",
                           f"{u['name']} from support would like to see which apps each device is using for the next 60 minutes "
                           "to fix your problem. Nothing is shared unless you allow it.", f"diag:{now():.0f}")
                ctl.audit(u["username"], "staff", "requested_diagnostics", f"gw:{gid}", {}, self._ip())
                return {"ok": True, "asked": True}
            if kind == "snapshot":
                self._staff("home_actions")
                ctl.audit(u["username"], "staff", "snapshot", f"gw:{gid}", {}, self._ip())
                return ctl.call(gid, "snapshot", {}, u["username"])
            raise HTTPError(400, "unknown command")

        # ------------------------------------------------------------ consumer app
        def _appapi(self, method, p, qs, body):
            if p == "/v1/app/brand":
                return self._send(200, ctl.cfg["brand"])
            if p == "/v1/app/otp" and method == "POST":
                phone = re.sub(r"\D", "", str(body.get("phone", "")))[-10:]
                sub = db.one("SELECT id FROM subscribers WHERE phone LIKE ?", (f"%{phone}",)) if len(phone) == 10 else None
                if not sub:
                    time.sleep(0.4)
                    raise HTTPError(404, "this number is not registered with a broadband account")
                o = db.one("SELECT * FROM otp WHERE phone=?", (phone,))
                if o and now() - o["sent"] < 30:
                    raise HTTPError(429, "please wait 30 seconds before asking for another code")
                code = f"{int.from_bytes(os.urandom(3), 'big') % 1000000:06d}"
                db.x("INSERT OR REPLACE INTO otp (phone, code_hash, expires, attempts, sent) VALUES (?,?,?,0,?)",
                     (phone, S.h(phone + code), now() + 300, now()))
                out = {"ok": True, "sent_to": mask_phone(phone)}
                if ctl.cfg.get("demo_otp"):
                    out["demo_code"] = code          # no SMS gateway in the demo; never enable in production
                return self._send(200, out)
            if p == "/v1/app/login" and method == "POST":
                phone = re.sub(r"\D", "", str(body.get("phone", "")))[-10:]
                o = db.one("SELECT * FROM otp WHERE phone=?", (phone,))
                if not o or o["expires"] < now() or o["attempts"] >= 5:
                    raise HTTPError(401, "the code has expired; ask for a new one")
                if S.h(phone + str(body.get("code", ""))) != o["code_hash"]:
                    db.x("UPDATE otp SET attempts=attempts+1 WHERE phone=?", (phone,))
                    raise HTTPError(401, "that code is not right")
                db.x("DELETE FROM otp WHERE phone=?", (phone,))
                sub = db.one("SELECT * FROM subscribers WHERE phone LIKE ?", (f"%{phone}",))
                tok = S.token()
                db.x("INSERT INTO sessions (token, kind, principal, expires, created) VALUES (?,?,?,?,?)",
                     (S.h(tok), "app", sub["id"], now() + 30 * 86400, now()))
                ctl.audit(sub["account"], "subscriber", "app_login", str(sub["id"]), {}, self._ip())
                return self._send(200, {"ok": True}, headers={"Set-Cookie": f"iqapp={tok}; Path=/; HttpOnly;{self._secure()} SameSite=Strict; Max-Age=2592000"})
            if p == "/v1/app/kitlink" and method == "POST":    # home kit: the console's QR code signs the phone in
                if not ctl.kit.enabled:
                    raise HTTPError(404, "kit mode is off")
                try:
                    sub = ctl.kit.redeem_phone_link(str(body.get("token", "")))
                except PermissionError as e:
                    raise HTTPError(401, str(e))
                tok = S.token()
                db.x("INSERT INTO sessions (token, kind, principal, expires, created) VALUES (?,?,?,?,?)",
                     (S.h(tok), "app", sub["id"], now() + 30 * 86400, now()))
                ctl.audit(sub["account"], "subscriber", "app_login_kitlink", str(sub["id"]), {}, self._ip())
                return self._send(200, {"ok": True}, headers={"Set-Cookie": f"iqapp={tok}; Path=/; HttpOnly;{self._secure()} SameSite=Strict; Max-Age=2592000"})
            if p == "/v1/app/quick" and method == "POST":      # lab only, like demo_otp
                sub = db.one("SELECT * FROM subscribers WHERE phone LIKE ?", ("%9800000001",)) if self._quick_ok() else None
                if not sub:
                    raise HTTPError(403, "quick sign-in is not enabled")
                tok = S.token()
                db.x("INSERT INTO sessions (token, kind, principal, expires, created) VALUES (?,?,?,?,?)",
                     (S.h(tok), "app", sub["id"], now() + 30 * 86400, now()))
                ctl.audit(sub["account"], "subscriber", "app_login_quick", str(sub["id"]), {}, self._ip())
                return self._send(200, {"ok": True}, headers={"Set-Cookie": f"iqapp={tok}; Path=/; HttpOnly;{self._secure()} SameSite=Strict; Max-Age=2592000"})
            sub = self._app()
            sid = sub["id"]
            g = ctl.app_gateway(sid)
            if p == "/v1/app/logout" and method == "POST":
                db.x("DELETE FROM sessions WHERE token=?", (S.h(self._cookie("iqapp") or ""),))
                return self._send(200, {"ok": True}, headers={"Set-Cookie": "iqapp=; Path=/; Max-Age=0"})
            if p == "/v1/app/home":
                return self._send(200, ctl.app_home(sid))
            if p == "/v1/app/notifications":
                if method == "POST":
                    db.x("UPDATE notifications SET read=1 WHERE subscriber_id=?", (sid,))
                    return self._send(200, {"ok": True})
                return self._send(200, db.q("SELECT * FROM notifications WHERE subscriber_id=? ORDER BY t DESC LIMIT 50", (sid,)))
            if p == "/v1/app/privacy":
                if method == "POST":
                    return self._send(200, ctl.set_consent(sid, body, sub["account"]))
                return self._send(200, {"consent": {**CONSENT_DEFAULT, **jload(sub["consent"], {})},
                                        "requests": db.q("SELECT * FROM privacy_requests WHERE subscriber_id=? ORDER BY id DESC", (sid,)),
                                        "grievance": ctl.cfg["brand"].get("grievance_email")})
            if p == "/v1/app/privacy/export":
                ctl.audit(sub["account"], "subscriber", "data_export", str(sid), {}, self._ip())
                return self._send(200, ctl.export_data(sid))
            if p == "/v1/app/privacy/request" and method == "POST":
                kind = body.get("kind")
                if kind not in ("erasure", "correction", "grievance"):
                    raise HTTPError(400, "unknown request")
                rid = db.x("INSERT INTO privacy_requests (subscriber_id, kind, status, created, due, detail) VALUES (?,?,?,?,?,?)",
                           (sid, kind, "open", now(), now() + 30 * 86400, str(body.get("detail", ""))[:1000]))
                ctl.audit(sub["account"], "subscriber", f"privacy_{kind}", str(sid), {}, self._ip())
                ctl.notify(sid, "privacy", "We received your request", f"Your {kind} request #{rid} will be answered within 30 days.", f"priv:{rid}")
                return self._send(200, {"ok": True, "id": rid})
            if p == "/v1/app/diagnostics/consent" and method == "POST":
                if not g:
                    raise HTTPError(404, "no router on this account")
                if body.get("allow"):
                    r = ctl.call(g["id"], "set_level", {"level": "diagnostic", "minutes": 60}, f"subscriber {sub['account']}")
                    ctl.audit(sub["account"], "subscriber", "allowed_diagnostics_60min", str(sid), {}, self._ip())
                    return self._send(200, r)
                ctl.audit(sub["account"], "subscriber", "declined_diagnostics", str(sid), {}, self._ip())
                return self._send(200, {"ok": True})
            if p == "/v1/app/plans":
                cur = db.val("SELECT plan_id FROM subscribers WHERE id=?", (sid,))
                rows = db.q("SELECT id, code, name, down_mbit, up_mbit, price_inr FROM plans ORDER BY down_mbit")
                if method == "POST":
                    pl = next((r for r in rows if r["code"] == str(body.get("code", ""))), None)
                    if not pl or pl["id"] == cur:
                        raise HTTPError(400, "choose a different plan")
                    tid = ctl.create_ticket(sid, "app", "plan_change",
                                            f"Asked to move to {pl['name']} ({pl['down_mbit']}/{pl['up_mbit']} Mbit/s, "
                                            f"Rs {pl['price_inr']}/month)", False, sub["account"])
                    return self._send(200, {"ok": True, "id": tid})
                return self._send(200, [{**r, "current": r["id"] == cur} for r in rows])
            if p == "/v1/app/tickets":
                if method == "POST":
                    tid = ctl.create_ticket(sid, "app", str(body.get("category", "slow"))[:40], str(body.get("summary", ""))[:1000],
                                            bool(body.get("diagnostics", True)), sub["account"])
                    t = db.one("SELECT id, status, diagnosis FROM tickets WHERE id=?", (tid,))
                    return self._send(200, {"ok": True, "id": tid, "diagnosis": jload(t["diagnosis"])})
                rows = db.q("SELECT id, category, status, created, updated, summary, thread FROM tickets WHERE subscriber_id=? ORDER BY id DESC", (sid,))
                return self._send(200, [{**r, "thread": jload(r["thread"], [])} for r in rows])
            if not g:
                raise HTTPError(404, "no router linked to this account yet")
            if p == "/v1/app/speedtest" and method == "POST":
                if not ctl.cfg.get("test_server"):
                    raise HTTPError(400, "speed test is not available")
                r = ctl.call(g["id"], "line_test", {"url": ctl.cfg["test_server"], "seconds": 8}, f"subscriber {sub['account']}", timeout=30)
                return self._send(200, r)
            # live views and actions on the subscriber's own router (pass-through: not stored by the controller)
            live = {"/v1/app/live/state": ("GET", "/api/state"), "/v1/app/live/usage": ("GET", "/api/usage?days=30"),
                    "/v1/app/live/modes": ("GET", "/api/modes"), "/v1/app/live/schedules": ("GET", "/api/schedules")}
            if p in live:
                mth, path = live[p]
                r = ctl.relay(g["id"], mth, path, None, f"subscriber {sub['account']}")
                if r.get("offline"):
                    raise HTTPError(503, "your router is offline")
                body_out = r.get("body")
                if path == "/api/state" and isinstance(body_out, dict):
                    body_out = {k: body_out.get(k) for k in ("t", "grade", "devices", "intents", "estimator", "config",
                                                            "budget", "schedules", "paused", "loss_pct", "offers")}
                    xp = (r.get("body") or {}).get("experience") or {}
                    body_out["experience"] = {"apps": [{k: a.get(k) for k in ("ip", "device", "label", "qoo")}
                                                       for a in (xp.get("apps") or [])]}
                return self._send(r.get("status", 200), body_out)
            m = re.fullmatch(r"/v1/app/live(/api/(?:intents|devices|modes|schedules|offers)(?:/.*)?)", p)
            if m and method in ("POST", "PUT", "DELETE"):
                path = m.group(1)
                if path.startswith("/api/intents") and method == "POST":
                    body = {**body, "requester": sub["name"].split()[0].lower()}
                if path.startswith("/api/devices/") and method == "POST":
                    body = {**body, "by": sub["name"].split()[0]}
                q = urlparse(self.path).query
                r = ctl.relay(g["id"], method, path + (f"?{q}" if q else ""), body, f"subscriber {sub['account']}")
                ctl.audit(sub["account"], "subscriber", "app_action", f"gw:{g['id']}", {"method": method, "path": path}, self._ip())
                if r.get("offline"):
                    raise HTTPError(503, "your router is offline")
                return self._send(r.get("status", 200), r.get("body"))
            raise HTTPError(404, "not found")

        # ------------------------------------------------------------ home kit
        def _kitapi(self, method, p, qs, body):
            kit = ctl.kit
            if p == "/v1/kit/where":
                return self._send(200, kit.where(local_ip_for(self._ip())) if kit.enabled else {"service": "intentqos-console",
                                                                                                "kit": False})
            if not kit.enabled:
                raise HTTPError(404, "kit mode is off")
            auth = self.headers.get("Authorization", "")
            if p == "/v1/kit/pair" and method == "POST":
                return self._send(200, kit.pair(body, self._ip()))
            if p == "/v1/kit/status" and method == "GET":
                if not self._loopback():
                    kit.token_ok(auth)
                return self._send(200, kit.status(self._loopback()))
            if p == "/v1/kit/phone" and method == "GET":
                if not self._loopback():
                    raise HTTPError(403, "this computer only")
                sid = db.val("SELECT id FROM subscribers WHERE account=?", (kit.household()["account"],))
                n = db.val("SELECT count(*) FROM sessions WHERE kind='app' AND principal=? AND expires > ?", (sid, now())) if sid else 0
                return self._send(200, {"signed_in": bool(n)})
            if p == "/v1/kit/results" and method == "POST":
                return self._send(200, kit.save_result(kit.token_ok(auth), body))
            m = re.fullmatch(r"/v1/kit/results/([0-9a-f-]+)", p)
            if m and method == "GET":
                if not self._loopback():
                    kit.token_ok(auth)
                return self._send(200, kit.result(m.group(1)))
            if p == "/v1/kit/household" and method == "POST":
                if not self._loopback():
                    raise HTTPError(403, "change the household on the Operator Console computer")
                name, phone = str(body.get("name", "")).strip()[:60], re.sub(r"\D", "", str(body.get("phone", "")))
                if len(phone) != 10:
                    raise HTTPError(400, "the mobile number needs 10 digits")
                db.set_meta("kit_household", {"name": name or "Household", "phone": phone})
                acct = kit.household()["account"]
                db.x("UPDATE subscribers SET name=?, phone=? WHERE account=?", (name or "Household", phone, acct))
                return self._send(200, {"ok": True, "household": kit.household()})
            raise HTTPError(404, "not found")

        # ------------------------------------------------------------ northbound
        def _nbapi(self, method, p, qs, body):
            k = self._nb()
            if p == "/v1/nb/subscribers" and method == "POST":
                plan = db.one("SELECT * FROM plans WHERE code=?", (body["plan"],))
                if not plan:
                    raise HTTPError(400, "unknown plan code")
                cur = db.one("SELECT * FROM subscribers WHERE account=?", (body["account"],))
                if cur:
                    db.x("UPDATE subscribers SET name=COALESCE(?, name), plan_id=?, region=COALESCE(?, region), olt=COALESCE(?, olt) "
                         "WHERE id=?", (body.get("name"), plan["id"], body.get("region"), body.get("olt"), cur["id"]))
                    changed = cur["plan_id"] != plan["id"]
                    if changed:
                        for g in db.q("SELECT id FROM gateways WHERE subscriber_id=?", (cur["id"],)):
                            ctl.push_config(g["id"], f"plan change to {plan['code']} (BSS)")
                        ctl.notify(cur["id"], "plan", f"Your plan is now {plan['name']}",
                                   f"Your router has been updated for {plan['down_mbit']:.0f}/{plan['up_mbit']:.0f} Mbit/s.", f"plan:{plan['code']}")
                    ctl.audit(k["name"], "integration", "subscriber_updated", body["account"], {"plan": plan["code"], "plan_changed": changed})
                    return self._send(200, {"ok": True, "updated": True, "plan_changed": changed})
                sid = db.x("INSERT INTO subscribers (account, name, phone, email, plan_id, region, pop, olt, pon, area, created) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (body["account"], body.get("name"), body.get("phone"), body.get("email"),
                                                              plan["id"], body.get("region"), body.get("pop"), body.get("olt"),
                                                              body.get("pon"), body.get("area"), now()))
                ctl.audit(k["name"], "integration", "subscriber_created", body["account"], {})
                return self._send(200, {"ok": True, "id": sid})
            if p == "/v1/nb/gateways" and method == "POST":
                sub = db.one("SELECT id FROM subscribers WHERE account=?", (body["account"],))
                db.x("INSERT INTO gateways (serial, claim_hash, subscriber_id) VALUES (?,?,?) ON CONFLICT(serial) DO UPDATE SET "
                     "claim_hash=excluded.claim_hash, subscriber_id=excluded.subscriber_id",
                     (body["serial"], S.h(body["claim_code"]), sub["id"] if sub else None))
                ctl.audit(k["name"], "integration", "gateway_provisioned", body["serial"], {"account": body["account"]})
                return self._send(200, {"ok": True})
            if p == "/v1/nb/incidents":
                return self._send(200, db.q("SELECT id, scope, kind, status, opened, closed, affected, summary FROM incidents "
                                            "ORDER BY id DESC LIMIT 100"))
            if p == "/v1/nb/qoe":
                return self._send(200, ctl.fleet_summary())
            if p == "/v1/nb/qoo-private":
                return self._send(200, ctl.fleet_private_qoo(float(qs.get("hours", 1))))
            raise HTTPError(404, "not found")

        def _camara_nb(self, method, p, body):
            """CAMARA QoD v1 for applications, API-key authenticated; home + (simulated) access network."""
            from intentqos.camara import PROFILES, CamaraError, profile_doc
            k = self._nb()
            nb = ctl.camara
            try:
                if p == "/camara/qos-profiles/v1/qos-profiles" and method == "GET":
                    return self._send(200, [profile_doc(n) for n in PROFILES])
                if p == "/camara/quality-on-demand/v1/sessions" and method == "POST":
                    return self._send(201, nb.create(body, k["name"]))
                m = re.fullmatch(r"/camara/quality-on-demand/v1/sessions/(op-[0-9a-f]+)(/extend)?", p)
                if m and method == "GET" and not m.group(2):
                    return self._send(200, nb.get(m.group(1), k["name"]))
                if m and method == "POST" and m.group(2):
                    return self._send(200, nb.extend(m.group(1), body, k["name"]))
                if m and method == "DELETE" and not m.group(2):
                    nb.delete(m.group(1), k["name"])
                    return self._send(204, b"", "application/json")
                raise CamaraError(404, "NOT_FOUND", "no such CAMARA resource")
            except CamaraError as e:
                return self._send(e.status, e.body())

    return H


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for k in ("data_dir", "tls_cert", "tls_key"):           # relative paths are relative to the repository root
        if cfg.get(k) and not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(ROOT, cfg[k])
    return cfg


def serve(cfg: dict) -> None:
    ctl = Controller(cfg)
    ctl.kit = Kit(ctl, cfg)
    host, port = cfg["listen"].rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), make_handler(ctl))
    srv.daemon_threads = True
    if cfg.get("tls_cert"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cfg["tls_cert"], cfg["tls_key"])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)

        def reload_cert(path=cfg["tls_cert"], key=cfg["tls_key"]):
            """Pick up a re-issued server certificate (the console re-issues it when the laptop gets a new
            address); new connections use it, the CA routers trust is unchanged."""
            mt = os.path.getmtime(path)
            while True:
                time.sleep(5)
                try:
                    m = os.path.getmtime(path)
                    if m != mt:
                        time.sleep(0.5)               # let the key and certificate both be written
                        ctx.load_cert_chain(path, key)
                        mt = m
                        print("controller: server certificate reloaded", flush=True)
                except (OSError, ssl.SSLError) as e:
                    print(f"controller: certificate reload failed: {e}", flush=True)
        threading.Thread(target=reload_cert, daemon=True).start()

    ui = cfg.get("ui_listen") or ""
    if ui:
        uhost, uport = ui.rsplit(":", 1)
        if uhost not in ("127.0.0.1", "localhost", "::1") and not ctl.kit.enabled:
            raise SystemExit("ui_listen serves plain HTTP and must be a loopback address (127.0.0.1:PORT) "
                             "unless [kit] enabled = true (then home devices get only the app)")
        usrv = ThreadingHTTPServer((uhost, int(uport)), make_handler(ctl))
        usrv.daemon_threads = True
        usrv.plain_http = True
        threading.Thread(target=usrv.serve_forever, daemon=True).start()
        print(f"IntentQoS controller consoles on http://{uhost}:{uport}/ops/ and /app/ "
              f"({'phones on the home network: /app/ only' if ctl.kit.enabled else 'this machine only'})", flush=True)

    def house():
        while True:
            try:
                ctl.housekeeping()
            except Exception as e:  # noqa: BLE001
                print(f"housekeeping error: {e}", file=sys.stderr, flush=True)
            time.sleep(15)
    threading.Thread(target=house, daemon=True).start()
    ctl.kit.start()
    print(f"IntentQoS controller on {'https' if cfg.get('tls_cert') else 'http'}://{host}:{port}/ "
          f"(release key {ctl.key.pub_hex[:16]}…)", flush=True)
    srv.serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    serve(load_config(a.config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
