"""Router agent: connects this gateway's engine to the operator's controller.

Runs next to the engine (a separate process, so a problem here never affects shaping). It only ever
makes OUTBOUND HTTPS connections, which works behind NAT and CGNAT, and it talks to the engine over
its local API.

    python3 -m intentqos.agent --config /etc/intentqos.toml

What it does
  enrol      first start: serial number + one-time claim code -> device id and device secret
  sync       every telemetry interval: compact QoE telemetry (+ buffered records after an outage),
             command results; receives commands, config and model updates
  wait       long-poll so operator actions reach the home within about a second
  verify     every config profile and model is signed with the operator's release key (Ed25519);
             nothing unsigned or tampered is applied
  privacy    telemetry level follows the subscriber's consent: basic (aggregates only),
             standard (+ device activity), diagnostic (+ flows, time-limited support sessions)

Authentication: TLS (server certificate checked against the operator CA) plus an HMAC-SHA256
signature on every request with the per-device secret, a timestamp and a nonce (replay-protected).
Mapping to TR-369/USP: docs/AGENT_PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__, ed25519, privagg, release
from . import config as C

LEVELS = ("basic", "standard", "diagnostic")
# Local engine endpoints the controller may reach through the relay. Everything else is refused.
RELAY_GET = ("/api/state", "/api/history", "/api/system", "/api/setup-check", "/api/inspect", "/api/model",
             "/api/modes", "/api/schedules", "/api/devices", "/api/usage", "/api/config", "/api/healthz",
             "/api/experience", "/api/offers", "/api/usp", "/camara/qos-profiles/", "/camara/quality-on-demand/v1/sessions/")
RELAY_POST = ("/api/intents", "/api/intents/parse", "/api/flows/label", "/api/classifier/mode", "/api/safe", "/api/rollback",
              "/api/automation", "/api/modes/", "/api/schedules", "/api/devices/", "/api/offers/",
              "/camara/quality-on-demand/v1/")
RELAY_DELETE = ("/api/intents/", "/api/schedules/", "/camara/quality-on-demand/v1/sessions/")


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sign_request(secret: bytes, method: str, path: str, body: bytes, ts: str, nonce: str) -> str:
    msg = "\n".join([method, path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def default_serial(wan: str) -> str:
    for p in ("/proc/device-tree/serial-number", "/sys/class/dmi/id/product_serial"):
        try:
            with open(p) as f:
                s = f.read().strip("\x00\n ")
                if s:
                    return s
        except OSError:
            pass
    try:
        with open(f"/sys/class/net/{wan}/address") as f:
            return "MAC-" + f.read().strip().replace(":", "").upper()
    except OSError:
        return "HOST-" + socket.gethostname()


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        a = cfg.get("agent", {})
        self.url = a.get("controller_url", "").rstrip("/")
        self.serial = a.get("serial") or default_serial(cfg["interfaces"]["wan"])
        self.claim = a.get("claim_code", "")
        self.engine = a.get("engine_api", "http://127.0.0.1:8080").rstrip("/")
        from .api import router_key
        self._router_key = lambda: router_key(cfg)
        self.engine_token = self._router_key()
        self.state_path = C.resolve(cfg, a.get("state_path", "data/agent.json"))
        self.models_dir = C.resolve(cfg, a.get("models_dir", "models"))
        self.interval = float(a.get("telemetry_interval_s", 15))
        self.max_buffer = int(a.get("buffer_records", 480))         # 2 h at 15 s
        self.pubkey = bytes.fromhex(a["release_pubkey"]) if a.get("release_pubkey") else None
        # offline release keys that must sign a model's identity (TUF "targets"); empty = the online key alone
        self.release_keys = [bytes.fromhex(k) for k in (a.get("release_pubkeys") or []) if k]
        self.release_threshold = max(1, int(a.get("release_threshold", 1)))
        # separately operated helper aggregator for private fleet statistics (IETF DAP "helper"); empty = home kit
        self.helper_url = (a.get("helper_url") or "").rstrip("/")
        # the helper is run by another party: verify it against the system's public CAs, not the operator CA
        self.helper_http = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.hw_model = a.get("hw_model", platform.machine())
        ctx = ssl.create_default_context(cafile=C.resolve(cfg, a["ca_file"]) if a.get("ca_file") else None)
        if a.get("client_cert"):
            ctx.load_cert_chain(C.resolve(cfg, a["client_cert"]), C.resolve(cfg, a.get("client_key", "")) or None)
        self.ctx = ctx
        # direct connections only: a gateway must not silently follow proxy variables from its environment
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.https = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        self.st = {"device_id": None, "secret": None, "level": "standard", "level_until": None,
                   "config_version": 0, "model_version": None, "model_path": None, "prev_model_path": None}
        try:
            with open(self.state_path) as f:
                self.st.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass
        self.buffer: collections.deque = collections.deque(maxlen=self.max_buffer)
        self.results: list = []
        self.lock = threading.Lock()
        self.last_hist_t = 0.0
        self.last_event_seq = 0
        self.running = True
        self.connected = False
        self.last_ok = None
        self.dropped = 0
        self.discover = bool(a.get("discover"))       # home kit: find the Operator Console again if its address changes
        self._last_discover = 0.0
        if self.discover and self.st.get("controller_url") and self.st.get("paired_url") == self.url:
            self.url = self.st["controller_url"]       # learned after pairing (the console laptop moved)

    # ------------------------------------------------------------------ plumbing
    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.st, f, indent=1)
        os.replace(tmp, self.state_path)

    def _engine(self, method: str, path: str, body: dict | None = None, timeout: float = 5.0):
        h = {"Content-Type": "application/json"}
        if self.engine_token:
            h["Authorization"] = "Bearer " + self.engine_token
        req = urllib.request.Request(self.engine + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.http.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code == 401:                 # the engine may have created its key after we started
                self.engine_token = self._router_key()
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None

    def _ctl(self, method: str, path: str, body: dict | None = None, timeout: float = 20.0, signed: bool = True):
        data = canonical(body) if body is not None else b""
        h = {"Content-Type": "application/json", "User-Agent": f"IntentQoS-agent/{__version__}"}
        if signed:
            ts, nonce = str(int(time.time())), os.urandom(8).hex()
            h.update({"X-IQ-Device": self.st["device_id"], "X-IQ-Time": ts, "X-IQ-Nonce": nonce,
                      "X-IQ-Signature": sign_request(bytes.fromhex(self.st["secret"]), method, path, data, ts, nonce)})
        req = urllib.request.Request(self.url + path, method=method, headers=h, data=data if body is not None else None)
        with (self.https if self.url.startswith("https") else self.http).open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw

    def _send_helper(self, body: dict) -> None:
        """Post the helper share to the helper aggregator; a lost share only drops this home from that batch."""
        req = urllib.request.Request(self.helper_url + "/upload", method="POST", data=canonical(body),
                                     headers={"Content-Type": "application/json"})
        try:
            with self.helper_http.open(req, timeout=10) as r:
                r.read()
        except (OSError, urllib.error.URLError):
            pass

    @staticmethod
    def _experience_summary(st: dict) -> dict:
        """Outcome-level telemetry: what the household experienced, not just what the line did."""
        xp = st.get("experience") or {}
        gsv = (xp.get("path") or {}).get("gsv") or {}
        out = st.get("outcome") or {}
        where: dict[str, int] = {}
        for v in ((st.get("bottlenecks") or {}).get("devices") or {}).values():
            where[v.get("where", "idle")] = where.get(v.get("where", "idle"), 0) + 1
        apps = xp.get("apps") or []
        worst = min((a for a in apps if a.get("qoo") is not None), key=lambda a: a["qoo"], default=None)
        lan99 = [d["lan_rtt_ms"].get("99") or d["lan_rtt_ms"].get(99) for d in (xp.get("devices") or {}).values()
                 if d.get("lan_rtt_ms")]
        return {"g": gsv.get("g"), "v50": gsv.get("v_p50"), "v99": gsv.get("v_p99"), "loss": gsv.get("loss"),
                "objective": out.get("objective"), "worst_protected": out.get("worst_protected"),
                "worst": {"label": worst["label"], "qoo": worst["qoo"]} if worst else None,
                "gated": out.get("gated", 0), "bottlenecks": where,
                "lan_rtt_p99_max": max((x for x in lan99 if x is not None), default=None),
                "l4s": (st.get("l4s") or {}).get("flows", 0), "offers": len(st.get("offers") or []),
                "camara": st.get("camara_sessions", 0)}

    def rediscover(self) -> bool:
        """Home kit: the Operator Console laptop may have a new address (DHCP). Ask the home network where it is
        now and switch only if the new address passes TLS with our operator CA, so nobody else can redirect us."""
        if not self.discover or time.monotonic() - self._last_discover < 30:
            return False
        self._last_discover = time.monotonic()
        answers = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(1.5)
            # the home network's own broadcast address: 255.255.255.255 would leave by the WAN (the default route)
            targets = {"255.255.255.255"}
            for net in self.cfg.get("interfaces", {}).get("lan_subnets", []):
                try:
                    targets.add(str(ipaddress.ip_network(net, strict=False).broadcast_address))
                except ValueError:
                    pass
            for t in targets:
                try:
                    s.sendto(b"IQKIT-DISCOVER", (t, 47815))
                except OSError:
                    pass
            t_end = time.monotonic() + 2.0
            while time.monotonic() < t_end:
                try:
                    data, _ = s.recvfrom(2048)
                    answers.append(json.loads(data))
                except (socket.timeout, ValueError):
                    break
            s.close()
        except OSError:
            return False
        for ans in answers:
            url = str(ans.get("https", "")).rstrip("/")
            if not url.startswith("https://") or url == self.url:
                continue
            try:
                with self.https.open(url + "/healthz", timeout=5) as r:     # verified against the operator CA
                    if r.status != 200:
                        continue
            except Exception:  # noqa: BLE001
                continue
            print(f"agent: Operator Console moved: {self.url} -> {url}", flush=True)
            self.st["paired_url"] = self.st.get("paired_url") or self.cfg.get("agent", {}).get("controller_url", "").rstrip("/")
            self.url = self.st["controller_url"] = url
            self._save()
            return True
        return False

    # ------------------------------------------------------------------ enrolment
    def enroll(self) -> None:
        while self.running and not self.st.get("device_id"):
            try:
                _, sysinfo = self._engine("GET", "/api/system")
                r = self._ctl("POST", "/v1/agent/enroll", {
                    "serial": self.serial, "claim_code": self.claim, "hw_model": self.hw_model,
                    "arch": platform.machine(), "agent_version": __version__,
                    "engine": (sysinfo or {}).get("versions", {})}, signed=False)
                self.st.update(device_id=r["device_id"], secret=r["secret"])
                if r.get("release_pubkey") and not self.pubkey:
                    self.pubkey = bytes.fromhex(r["release_pubkey"])
                    self.st["release_pubkey"] = r["release_pubkey"]
                self._save()
                print(f"agent: enrolled as {r['device_id']}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"agent: enrolment failed ({e}); retrying", flush=True)
                if not isinstance(e, urllib.error.HTTPError) and self.rediscover():
                    continue
                time.sleep(10)
        if not self.pubkey and self.st.get("release_pubkey"):
            self.pubkey = bytes.fromhex(self.st["release_pubkey"])

    # ------------------------------------------------------------------ telemetry
    def level(self) -> str:
        u = self.st.get("level_until")
        if u and time.time() > u:
            self.st.update(level=self.st.get("level_base", "standard"), level_until=None)
            self._save()
        return self.st.get("level", "standard")

    def collect(self) -> dict | None:
        s1, st = self._engine("GET", "/api/state")
        if s1 != 200 or not st or st.get("starting"):
            return {"t": time.time(), "engine_down": True}
        _, rows = self._engine("GET", f"/api/history?since={self.last_hist_t}")
        rows = rows or []
        if rows:
            self.last_hist_t = rows[-1]["t"]

        def agg(key, fn):
            v = [r[key] for r in rows if r.get(key) is not None]
            return round(fn(v), 2) if v else None

        def p95(v):
            v = sorted(v)
            return v[int(0.95 * (len(v) - 1))]

        e, sf = st["estimator"], st["safety"]
        tin = {}
        for r in rows:
            for k, v in (r.get("tin_d") or {}).items():
                tin[k] = tin.get(k, 0.0) + v
        n = max(1, len(rows))
        rec = {
            "t": round(st["t"], 1), "n": len(rows),
            "grade": st["grade"]["overall"]["grade"],
            "p95_add_d": st["grade"]["down"]["p95_added_ms"], "p95_add_u": st["grade"]["up"]["p95_added_ms"],
            "add_d": agg("dd", lambda v: sum(v) / len(v)), "add_u": agg("du", lambda v: sum(v) / len(v)),
            "add_d_max": agg("dd", max), "add_u_p95": agg("du", p95),
            "jit_d": agg("jd", lambda v: sum(v) / len(v)), "jit_u": agg("ju", lambda v: sum(v) / len(v)),
            "loss_d": agg("ld", lambda v: sum(v) / len(v)), "loss_u": agg("lu", lambda v: sum(v) / len(v)),
            "rate_d": e["down"]["rate_kbit"], "rate_u": e["up"]["rate_kbit"],
            "ach_d": agg("ad", lambda v: sum(v) / len(v)), "ach_u": agg("au", lambda v: sum(v) / len(v)),
            "ach_d_max": agg("ad", max),
            "q_max": max([max((r.get("q_d") or {}).values(), default=0) for r in rows], default=0),
            "jain": st["fairness"]["jain"], "flows": len(st["flows"]),
            "devices": sum(1 for d in st["devices"] if d["flows"]),
            "leases": sum(1 for i in st["intents"] if i["status"] == "active"),
            "tin_d": {k: round(v / n) for k, v in tin.items()},
            "est": {"d": e["down"]["state"], "u": e["up"]["state"], "blind": e["blind"], "enabled": e["enabled"]},
            "plan": {"d": e["down"]["nominal_kbit"], "u": e["up"]["nominal_kbit"]},
            "safety": {"rollbacks": sf["rollbacks"], "fail": sf["apply_failures"], "shield": sf["shield_interventions"],
                       "changes": sf["changes_last_minute"]},
            "paused": st.get("paused", False),
            "cap": {"drops": st["capture"].get("kernel_drops", 0), "backend": st["capture"].get("backend")},
            "cpu": st["proc"]["cpu_pct"], "rss": st["proc"]["rss_mb"],
            "classifier": {"mode": st["config"]["classifier"], "abstain": st["classifier"]["stats"].get("abstain", 0),
                           "ml": st["classifier"]["stats"].get("ml", 0), "drift": st["classifier"]["stats"].get("drift_alarms", 0)},
            "versions": {"engine": st["version"], "agent": __version__, "config": st.get("managed", {}).get("version", 0),
                         "model": (st["classifier"].get("model_meta") or {}).get("release")
                         or (st["classifier"].get("model_meta") or {}).get("trained_at")},
            "level": self.level(),
        }
        rec["xp"] = self._experience_summary(st)
        hist = privagg.qoo_histogram((st.get("experience") or {}).get("apps"))
        if any(hist):
            leader, helper = privagg.split(hist)
            rec["agg"] = {"task": "qoo_hist_v1", "leader": leader}
            if self.helper_url:
                self._send_helper({"task": "qoo_hist_v1", "device": self.st.get("device_id"), "t": rec["t"],
                                   "share": helper})
            else:
                # no separate helper configured (home kit): both shares go to the console, which then holds both
                # roles; this demonstrates the data flow but is not private against the console. Flagged so.
                rec["agg"]["helper"] = helper
                rec["agg"]["demo_both_shares"] = True
        evs = [x for x in st["events"] if x.get("seq", 0) > self.last_event_seq]
        if evs:
            self.last_event_seq = max(x.get("seq", 0) for x in evs)
        rec["events"] = [{"t": x["t"], "kind": x["kind"], "level": x["level"], "msg": x["msg"][:240]} for x in evs
                         if x["kind"] in ("rollback", "watchdog", "estimator", "drift", "assurance", "intent", "engine",
                                          "capture", "config", "model", "device", "schedule")][-20:]
        lvl = rec["level"]
        if lvl in ("standard", "diagnostic"):
            rec["dev"] = [{"ip": d["ip"], "name": d["name"], "type": d.get("type"), "act": d.get("activity"),
                           "kd": d["kbps_dn"], "ku": d["kbps_up"], "fl": d["flows"], "today": d.get("today_bytes", 0),
                           "paused": d.get("paused", False), "lease": bool(d.get("intent"))} for d in st["devices"]]
            rec["intents"] = [{"id": i["id"], "target": i["target"], "goal": i["goal"], "label": i.get("label"),
                               "remaining_s": round(i["remaining_s"]), "assurance": i.get("assurance"),
                               "requester": i.get("requester")} for i in st["intents"] if i["status"] == "active"]
        if lvl == "diagnostic":
            rec["flows_top"] = [{k: f[k] for k in ("id", "lan_ip", "proto", "committed", "confidence", "source", "tin",
                                                   "kbps_dn", "kbps_up")} for f in st["flows"][:20]]
        return rec

    # ------------------------------------------------------------------ commands
    def _verify(self, payload: dict, sig_hex: str | None) -> bool:
        if not self.pubkey:
            return False
        try:
            return ed25519.verify(self.pubkey, canonical(payload), bytes.fromhex(sig_hex or ""))
        except ValueError:
            return False

    def _accept(self, args: dict, kind: str) -> tuple[dict | None, str | None]:
        """Check a signed release envelope (signature, gateway binding, sequence, expiry) and return its body.
        The sequence number is recorded only after the instruction was carried out."""
        env = args.get("envelope")
        why = release.check_instruction(env, kind, self.st.get("device_id"), self.pubkey,
                                        int(self.st.get("release_seq") or 0))
        if why:
            return None, why
        return env["body"], None

    def _done(self, args: dict) -> None:
        self.st["release_seq"] = max(int(self.st.get("release_seq") or 0), int(args["envelope"]["seq"]))
        self._save()

    def execute(self, cmd: dict) -> dict:
        kind, args = cmd.get("kind"), cmd.get("args") or {}
        try:
            if kind == "relay":
                return self._relay(args)
            if kind == "apply_config":
                body, why = self._accept(args, "config")
                if why:
                    return {"ok": False, "error": f"config not applied: {why}"}
                payload = {k: body[k] for k in ("version", "profile", "set")}
                if int(payload["version"]) <= int(self.st.get("config_version") or 0) and not body.get("reapply"):
                    return {"ok": False, "error": f"config not applied: version {payload['version']} is not newer "
                                                  f"than the installed {self.st.get('config_version')}"}
                code, r = self._engine("POST", "/api/config", {**payload, "by": f"controller ({args.get('by', 'operator')})"})
                if code == 200:
                    self.st["config_version"] = payload["version"]
                    self._done(args)
                return {"ok": code == 200, "engine": r}
            if kind == "install_model":
                return self._install_model(args)
            if kind == "rollback_model":
                prev = self.st.get("prev_model_path")
                if not prev:
                    return {"ok": False, "error": "no previous model on this gateway"}
                code, r = self._engine("POST", "/api/model/load", {"path": prev})
                if code == 200:
                    self.st.update(model_path=prev, prev_model_path=None, model_version=args.get("version"))
                    self._save()
                return {"ok": code == 200, "engine": r}
            if kind == "set_level":
                lvl = args.get("level")
                if lvl not in LEVELS:
                    return {"ok": False, "error": "unknown level"}
                if lvl == "diagnostic":
                    self.st["level_base"] = self.st.get("level_base") or self.st.get("level", "standard")
                    self.st["level_until"] = time.time() + float(args.get("minutes", 60)) * 60
                else:
                    self.st["level_base"] = lvl
                    self.st["level_until"] = None
                self.st["level"] = lvl
                self._save()
                return {"ok": True, "level": lvl, "until": self.st["level_until"]}
            if kind == "line_test":
                return self._line_test(args)
            if kind == "snapshot":
                out = {}
                for p in ("/api/state", "/api/system", "/api/setup-check", "/api/config"):
                    out[p] = self._engine("GET", p)[1]
                if self.level() != "diagnostic":
                    out["/api/state"]["flows"] = []            # flows only with a diagnostic consent
                return {"ok": True, "snapshot": out}
            return {"ok": False, "error": f"unknown command {kind}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _relay(self, a: dict) -> dict:
        method, path = a.get("method", "GET").upper(), a.get("path", "")
        base = path.split("?", 1)[0]
        allowed = {"GET": RELAY_GET, "POST": RELAY_POST, "DELETE": RELAY_DELETE, "PUT": ("/api/devices/",)}.get(method, ())
        if not any(base == p or (p.endswith("/") and base.startswith(p)) for p in allowed):
            return {"ok": False, "status": 403, "error": f"{method} {base} is not allowed through the relay"}
        if base == "/api/state" and self.level() != "diagnostic":
            code, body = self._engine(method, path, a.get("body"))
            if isinstance(body, dict):
                body["flows"] = []                          # per-flow detail needs a diagnostic consent
                body["flows_hidden"] = True
            return {"ok": True, "status": code, "body": body}
        code, body = self._engine(method, path, a.get("body"))
        return {"ok": True, "status": code, "body": body}

    def _install_model(self, args: dict) -> dict:
        a, why = self._accept(args, "model")
        if why:
            return {"ok": False, "error": f"model not installed: {why}"}
        payload = {k: a[k] for k in ("version", "sha256", "size")}
        keys, k = (self.release_keys, self.release_threshold) if self.release_keys else ([self.pubkey], 1)
        ok, detail = release.verify_threshold(payload, a.get("targets_sigs") or [], keys, k)
        if not ok:
            return {"ok": False, "error": f"model not installed: release signatures: {detail}"}
        blob = self._ctl("GET", f"/v1/agent/blob/{a['sha256']}", timeout=60)
        if hashlib.sha256(blob).hexdigest() != a["sha256"] or len(blob) != int(a["size"]):
            return {"ok": False, "error": "downloaded model does not match its signed hash"}
        os.makedirs(self.models_dir, exist_ok=True)
        path = os.path.join(self.models_dir, f"flowclf-{a['version']}.iqm")
        with open(path + ".tmp", "wb") as f:
            f.write(blob)
        os.replace(path + ".tmp", path)
        prev = self.st.get("model_path")
        if prev == path:                                  # re-install of the same release: keep the older fallback
            prev = self.st.get("prev_model_path")
        code, r = self._engine("POST", "/api/model/load", {"path": path})
        if code != 200:
            return {"ok": False, "error": f"engine refused the model: {r}"}
        engine_prev = (r or {}).get("previous")
        self.st.update(prev_model_path=prev or (engine_prev if engine_prev != path else None), model_path=path,
                       model_version=a["version"])
        self._done(args)
        return {"ok": True, "version": a["version"], "path": path, "previous": self.st["prev_model_path"]}

    def _line_test(self, a: dict) -> dict:
        """Download for a few seconds through the gateway (and the ISP link) while sampling the engine's
        delay estimate: throughput plus latency under load, the way a household feels it. The household's
        own traffic during the test is measured too, so a busy home is not reported as a slow line."""
        url, secs = a.get("url"), min(20.0, float(a.get("seconds", 8)))
        if not url:
            return {"ok": False, "error": "no test server configured"}
        _, st0 = self._engine("GET", "/api/state")
        idle = st0["estimator"]["down"]["delay_delta_ms"] if st0 else None
        samples, got, t0 = [], 0, time.monotonic()
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                c, s = self._engine("GET", "/api/state", timeout=2)
                if c == 200 and s:
                    samples.append((s["estimator"]["down"]["delay_delta_ms"], s["estimator"]["up"]["delay_delta_ms"],
                                    sum(d.get("kbps_dn") or 0 for d in s.get("devices", [])) / 1000))
                stop.wait(0.5)
        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        try:
            with self.http.open(f"{url}{'&' if '?' in url else '?'}seconds={secs:g}", timeout=secs + 10) as r:
                while True:
                    b = r.read(65536)
                    if not b:
                        break
                    got += len(b)
                    if time.monotonic() - t0 > secs + 2:
                        break
        finally:
            stop.set()
            th.join(2)
        dt = time.monotonic() - t0
        loaded = sorted(s[0] for s in samples)
        home = sorted(s[2] for s in samples[1:]) or [0.0]     # first sample is before the download ramps up
        mbps = got * 8 / dt / 1e6
        household = home[len(home) // 2]
        return {"ok": True, "mbps": round(mbps, 1), "seconds": round(dt, 1), "bytes": got,
                "household_mbps": round(household, 1), "line_mbps": round(mbps + household, 1),
                "idle_added_ms": round(idle, 1) if idle is not None else None,
                "loaded_added_ms_p95": round(loaded[int(0.95 * (len(loaded) - 1))], 1) if loaded else None,
                "plan_down_mbit": (st0 or {}).get("config", {}).get("nominal_down_mbit")}

    # ------------------------------------------------------------------ loops
    def _handle(self, resp: dict) -> None:
        for c in resp.get("commands", []):
            res = self.execute(c)
            with self.lock:
                self.results.append({"id": c["id"], "result": res, "t": time.time()})
        if resp.get("results_now"):
            self.flush_results()

    def flush_results(self) -> None:
        with self.lock:
            res, self.results = self.results, []
        if not res:
            return
        try:
            self._ctl("POST", "/v1/agent/result", {"results": res})
        except Exception:  # noqa: BLE001
            with self.lock:
                self.results = res + self.results

    def sync_loop(self) -> None:
        backoff = 5.0
        while self.running:
            t0 = time.monotonic()
            try:
                rec = self.collect()
            except Exception as e:  # noqa: BLE001
                rec = {"t": time.time(), "agent_error": str(e)[:200]}
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped += 1
            self.buffer.append(rec)
            with self.lock:
                res, self.results = self.results, []
            try:
                batch = list(self.buffer)[:40]
                body = {"records": batch, "results": res, "buffered": len(self.buffer), "dropped": self.dropped,
                        "state": {"config_version": self.st.get("config_version"),
                                  "model_version": self.st.get("model_version") or (batch[-1].get("versions") or {}).get("model"),
                                  "level": self.level(), "level_until": self.st.get("level_until"), "agent": __version__}}
                resp = self._ctl("POST", "/v1/agent/sync", body)
                for _ in batch:
                    self.buffer.popleft()
                self.connected, self.last_ok, backoff = True, time.time(), 5.0
                if resp.get("interval"):
                    self.interval = float(resp["interval"])
                if resp.get("level") in LEVELS and resp["level"] != self.st.get("level") and not self.st.get("level_until"):
                    self.st["level"] = self.st["level_base"] = resp["level"]
                    self._save()
                self._handle(resp)
                if self.buffer:            # catching up after an outage: send the backlog quickly
                    continue
            except urllib.error.HTTPError as e:
                with self.lock:
                    self.results = res + self.results
                if e.code == 401:
                    why = (e.read() or b"").decode(errors="replace")
                    print(f"agent: controller rejected our credentials ({why.strip()[:120]}); enrolling again", flush=True)
                    if "unknown device" in why:
                        self.st.update(device_id=None, secret=None)
                        self._save()
                        self.enroll()
                        continue
                self.connected = False
                time.sleep(min(backoff, self.interval))
                backoff = min(120.0, backoff * 2)
                continue
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.results = res + self.results
                if self.connected:
                    print(f"agent: controller unreachable ({e}); buffering telemetry", flush=True)
                self.connected = False
                if self.rediscover():
                    continue
                time.sleep(min(backoff, self.interval))
                backoff = min(120.0, backoff * 2)
                continue
            d = self.interval - (time.monotonic() - t0)
            if d > 0:
                time.sleep(d)

    def wait_loop(self) -> None:
        while self.running:
            if not self.connected:
                time.sleep(2)
                continue
            try:
                resp = self._ctl("GET", "/v1/agent/wait?timeout=25", timeout=35)
                if resp.get("commands"):
                    self._handle(resp)
                    self.flush_results()
            except Exception:  # noqa: BLE001
                time.sleep(3)

    def run(self) -> None:
        self.enroll()
        threading.Thread(target=self.wait_loop, daemon=True, name="wait").start()
        self.sync_loop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args(argv)
    cfg = C.load(a.config)
    for kv in a.set:
        k, v = kv.split("=", 1)
        C.set_dotted(cfg, k, C.parse_value(v))
    if not cfg.get("agent", {}).get("controller_url"):
        print("agent: [agent] controller_url is not set; nothing to do", file=sys.stderr)
        return 2
    ag = Agent(cfg)
    try:
        ag.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
