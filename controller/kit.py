"""Home-kit mode: lets the separate IntentQoS kit programs find this controller on a home network and pair
with it, so the router, the operator laptop, the test laptop and the phone work as one system.

Enabled by `[kit] enabled = true` (IntentQoS Operator Console writes that config). What it adds:

  discovery  UDP port 47815: a program broadcasts "IQKIT-DISCOVER"; the controller answers with where it is
             (the address on the interface facing the asker), so nobody types IP addresses.
  pairing    POST /v1/kit/pair with the 6-digit PIN shown on the Operator Console screen (new PIN on every start,
             5 wrong PINs lock pairing for 5 minutes). Router Setup gets the controller URL, the operator CA, the
             release key and a one-time claim code; the router's own agent then enrols over the normal signed
             protocol. The Test Kit gets the paired routers' addresses and keys and an upload token.
  results    POST /v1/kit/results (upload token): Test Kit reports, listed on /kit/ and kept in data/kit/results.
  /kit/      the kit's home page (this computer only): PIN, paired devices and their state, the phone QR code,
             test reports, links to the operator console and the app.

The phone app is served to the home network on plain HTTP (`ui_listen = "0.0.0.0:8088"` is accepted only in
kit mode, and LAN clients get only the app; the operator console stays on this computer). This is the same
trust model as a router's own admin page on the LAN; an ISP deployment serves the app over HTTPS instead.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time

from . import security as S

DISCOVERY_PORT = 47815
DISCOVER = b"IQKIT-DISCOVER"
SCHEMA = """
CREATE TABLE IF NOT EXISTS kit_devices (id INTEGER PRIMARY KEY, role TEXT, serial TEXT, name TEXT, lan_ip TEXT,
  router_key TEXT, token_hash TEXT, info TEXT, added REAL, last REAL, UNIQUE(role, serial));
"""
HOUSEHOLD = {"account": "SN-100001", "name": "Asha Verma", "phone": "9800000001"}   # fictional, as in the lab


def local_ip_for(peer: str) -> str:
    """The address of this machine on the interface that faces `peer` (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((peer if peer and not peer.startswith("127.") else "192.0.2.1", 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def lan_addresses() -> list[str]:
    """IPv4 addresses of this machine that other home devices could reach (best effort, no extra deps)."""
    out = []
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.append(sa[0])
    except OSError:
        pass
    out.append(local_ip_for("192.0.2.1"))
    good = []
    for a in out:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and a not in good and not a.startswith("172.17."):
            good.append(a)
    return good or ["127.0.0.1"]


class Kit:
    def __init__(self, ctl, cfg: dict):
        self.ctl, self.cfg, self.db = ctl, cfg, ctl.db
        self.kcfg = cfg.get("kit", {})
        self.enabled = bool(self.kcfg.get("enabled"))
        self.pin = f"{secrets.randbelow(10 ** 6):06d}"
        self.fails: list[float] = []
        self.lock = threading.Lock()
        self.dir = os.path.join(cfg["data_dir"], "kit", "results")
        os.makedirs(self.dir, exist_ok=True)
        with self.db.wlock:
            c = self.db.conn()
            c.executescript(SCHEMA)
            c.commit()
        hp, ap = cfg["listen"].rsplit(":", 1)[1], (cfg.get("ui_listen") or ":8088").rsplit(":", 1)[1]
        self.https_port, self.app_port = int(hp), int(ap)

    # ------------------------------------------------------------ discovery
    def start(self) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._discovery, daemon=True, name="kit-discovery").start()
        print(f"IntentQoS kit: pairing PIN {self.pin}; discovery on UDP {DISCOVERY_PORT}", flush=True)

    def _discovery(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", int(self.kcfg.get("discovery_port", DISCOVERY_PORT))))
        except OSError as e:
            print(f"kit discovery not available: {e}", flush=True)
            return
        while True:
            try:
                data, addr = s.recvfrom(512)
            except OSError:
                time.sleep(1)
                continue
            if not data.startswith(DISCOVER):
                continue
            me = local_ip_for(addr[0])
            s.sendto(json.dumps(self.where(me)).encode(), addr)

    def where(self, ip: str) -> dict:
        out = {"service": "intentqos-console", "name": self.cfg.get("brand", {}).get("name", "IntentQoS"),
               "ip": ip, "https": f"https://{ip}:{self.https_port}", "app": f"http://{ip}:{self.app_port}/app/",
               "pair": f"https://{ip}:{self.https_port}/v1/kit/pair", "version": 1}
        lab = os.environ.get("IQ_LAB_FINGERPRINT", "")          # in-home RPM + capacity endpoints (kit/lab.go)
        if lab:
            out["lab"] = {"fingerprint": lab, "https_port": int(os.environ.get("IQ_LAB_HTTPS_PORT", "8446")),
                          "udp_port": int(os.environ.get("IQ_LAB_UDP_PORT", "8447"))}
        return out

    # ------------------------------------------------------------ pairing
    def _check_pin(self, pin: str) -> None:
        t = time.time()
        with self.lock:
            self.fails = [x for x in self.fails if t - x < 300]
            if len(self.fails) >= 5:
                raise PermissionError(f"too many wrong PINs: pairing is locked for {int(300 - (t - self.fails[0]))} s")
            if not secrets.compare_digest(str(pin).strip(), self.pin):
                self.fails.append(t)
                raise PermissionError("wrong PIN: use the 6 digits shown on the Operator Console")
            self.fails.clear()

    def household(self) -> dict:
        h = dict(HOUSEHOLD)
        h.update({k: v for k, v in self.kcfg.get("household", {}).items() if v})
        h.update({k: v for k, v in (self.db.meta("kit_household") or {}).items() if v})
        return h

    def _plan_for(self, down: float, up: float) -> int:
        row = self.db.one("SELECT id FROM plans WHERE abs(down_mbit-?)<0.5 AND abs(up_mbit-?)<0.5", (down, up))
        if row:
            return row["id"]
        code = f"KIT-{down:g}-{up:g}"
        return self.db.x("INSERT INTO plans (code, name, down_mbit, up_mbit, boost_minutes, price_inr) VALUES (?,?,?,?,?,?)",
                         (code, f"Home kit {down:g}/{up:g}", down, up, 480, 0))

    def _subscriber(self, plan_id: int) -> int:
        h = self.household()
        cur = self.db.one("SELECT id FROM subscribers WHERE account=?", (h["account"],))
        if cur:
            self.db.x("UPDATE subscribers SET plan_id=?, name=?, phone=? WHERE id=?", (plan_id, h["name"], h["phone"], cur["id"]))
            return cur["id"]
        return self.db.x("INSERT INTO subscribers (account, name, phone, plan_id, region, area, created) VALUES (?,?,?,?,?,?,?)",
                         (h["account"], h["name"], h["phone"], plan_id, "Home kit", "Home kit", time.time()))

    def pair(self, body: dict, peer: str) -> dict:
        self._check_pin(body.get("pin", ""))
        role = body.get("role")
        me = local_ip_for(peer)
        if role == "router":
            serial = str(body.get("serial", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9._:-]{4,64}", serial):
                raise ValueError("serial must be 4-64 letters, digits or . _ : -")
            down, up = float(body.get("down_mbit") or 100), float(body.get("up_mbit") or 100)
            sid = self._subscriber(self._plan_for(down, up))
            claim = "KIT-" + secrets.token_hex(8)
            prof = self.db.val("SELECT id FROM profiles WHERE name='default'")
            # a re-paired router enrols again with the new claim code
            self.db.x("INSERT INTO gateways (serial, claim_hash, subscriber_id, profile_id) VALUES (?,?,?,?) ON CONFLICT(serial) "
                      "DO UPDATE SET claim_hash=excluded.claim_hash, subscriber_id=excluded.subscriber_id, device_id=NULL, secret=NULL",
                      (serial, S.h(claim), sid, prof))
            self._remember("router", serial, body.get("name") or body.get("hw_model") or "Router", body.get("lan_ip"),
                           body.get("router_key"), None, {k: body.get(k) for k in ("hw_model", "firmware", "version", "down_mbit", "up_mbit")})
            with open(os.path.join(self.cfg["data_dir"], "pki", "ca.pem")) as f:
                ca = f.read()
            self.ctl.audit("kit", "kit", "router_paired", serial, {"lan_ip": body.get("lan_ip")})
            return {"ok": True, "controller_url": f"https://{me}:{self.https_port}", "ca_pem": ca,
                    "release_pubkey": self.ctl.key.pub_hex, "claim_code": claim, "serial": serial,
                    "account": self.household()["account"]}
        if role == "testkit":
            token = "kt_" + secrets.token_hex(16)
            name = str(body.get("name") or "Test laptop")[:60]
            self._remember("testkit", body.get("machine") or name, name, peer, None, S.h(token), {})
            with open(os.path.join(self.cfg["data_dir"], "pki", "ca.pem")) as f:
                ca = f.read()
            return {"ok": True, "token": token, "routers": self.routers(with_keys=True), "ca_pem": ca,
                    "app": f"http://{me}:{self.app_port}/app/", "console": f"https://{me}:{self.https_port}"}
        raise ValueError("role must be 'router' or 'testkit'")

    def _remember(self, role, serial, name, lan_ip, key, token_hash, info) -> None:
        t = time.time()
        self.db.x("INSERT INTO kit_devices (role, serial, name, lan_ip, router_key, token_hash, info, added, last) "
                  "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(role, serial) DO UPDATE SET name=excluded.name, "
                  "lan_ip=COALESCE(excluded.lan_ip, lan_ip), router_key=COALESCE(excluded.router_key, router_key), "
                  "token_hash=COALESCE(excluded.token_hash, token_hash), info=excluded.info, last=excluded.last",
                  (role, str(serial)[:64], str(name)[:60], lan_ip, key, token_hash, json.dumps(info), t, t))

    def routers(self, with_keys: bool = False) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM kit_devices WHERE role='router' ORDER BY added"):
            g = self.db.one("SELECT id, last_seen, enrolled, engine_version FROM gateways WHERE serial=?", (r["serial"],))
            d = {"serial": r["serial"], "name": r["name"], "lan_ip": r["lan_ip"],
                 "console": f"http://{r['lan_ip']}:8080/" if r["lan_ip"] else None, "info": json.loads(r["info"] or "{}"),
                 "gateway_id": g["id"] if g else None, "enrolled": bool(g and g["enrolled"]),
                 "online": bool(g and g["last_seen"] and time.time() - g["last_seen"] < 60),
                 "last_seen": g["last_seen"] if g else None, "engine_version": g["engine_version"] if g else None}
            if with_keys:
                d["key"] = r["router_key"]
            out.append(d)
        return out

    def token_ok(self, auth: str) -> dict:
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        row = self.db.one("SELECT * FROM kit_devices WHERE role='testkit' AND token_hash=?", (S.h(tok),)) if tok else None
        if not row:
            raise PermissionError("pair this program with the Operator Console first")
        return row

    # ------------------------------------------------------------ test reports
    def save_result(self, who: dict, body: dict) -> dict:
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        body = dict(body)
        body["uploaded_by"] = who["name"]
        body["uploaded"] = time.time()
        raw = json.dumps(body).encode()
        if len(raw) > 2 << 20:
            raise ValueError("report too large")
        with open(os.path.join(self.dir, f"{rid}.json"), "wb") as f:
            f.write(raw)
        self.db.x("UPDATE kit_devices SET last=? WHERE id=?", (time.time(), who["id"]))
        return {"ok": True, "id": rid}

    def results(self) -> list[dict]:
        out = []
        for fn in sorted(os.listdir(self.dir), reverse=True)[:50]:
            try:
                with open(os.path.join(self.dir, fn)) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            out.append({"id": fn[:-5], "title": d.get("title"), "uploaded": d.get("uploaded"), "by": d.get("uploaded_by"),
                        "summary": d.get("summary"), "verdict": d.get("verdict")})
        return out

    def result(self, rid: str) -> dict:
        if not re.fullmatch(r"[0-9a-f-]{15,40}", rid):
            raise ValueError("bad id")
        with open(os.path.join(self.dir, f"{rid}.json")) as f:
            return json.load(f)

    # ------------------------------------------------------------ status for /kit/
    def status(self, local: bool) -> dict:
        ips = lan_addresses()
        d = {"enabled": self.enabled, "addresses": ips, "app_urls": [f"http://{ip}:{self.app_port}/app/" for ip in ips],
             "routers": self.routers(), "household": self.household(), "results": self.results(),
             "testkits": [{"name": r["name"], "last": r["last"]} for r in self.db.q("SELECT name, last FROM kit_devices WHERE role='testkit'")],
             "demo_otp": bool(self.cfg.get("demo_otp"))}
        if local:
            d["pin"] = self.pin
        return d
