"""Weak labels from published service endpoint lists.

Some services publish the addresses their real-time traffic uses. Microsoft's "Microsoft 365 IP Address
and URL web service" tags each endpoint set with a category; "Optimize" sets are the latency-sensitive
ones (Teams media among them) and the list is versioned and updated monthly. A flow to such an address is
very likely a call: that is a *weak label*, not ground truth (shared CDNs exist), so it is used in two
cautious ways:

* as a prior when the model abstains (before falling back to the rule baseline), and
* as feedback for adaptive conformal prediction (classifier.AdaptiveConformal): a prediction set that
  leaves out every call class for such a flow counts as a miss.

The list is kept in a local JSON file; `refresh_ms365()` downloads and converts the official list when the
gateway is allowed to ([weak_labels] fetch_ms365 = true). Nothing is sent but the list request itself.
"""
from __future__ import annotations

import ipaddress
import json
import os
import time
import urllib.request
import uuid

MS365_URL = "https://endpoints.office.com/endpoints/worldwide?clientrequestid={rid}"
CALL_SET = ("voip", "video")


def from_ms365(endpoints: list) -> list[dict]:
    """Official MS 365 endpoint JSON -> [{"net", "labels", "source", "udp"}] for Optimize sets with UDP ports."""
    out = []
    for e in endpoints or []:
        if not isinstance(e, dict) or e.get("category") != "Optimize":
            continue
        udp = str(e.get("udpPorts") or "")
        for net in e.get("ips") or []:
            out.append({"net": net, "labels": list(CALL_SET), "udp": bool(udp),
                        "source": f"Microsoft 365 {e.get('serviceArea', '')} Optimize (id {e.get('id')})"})
    return out


class WeakLabels:
    def __init__(self, cfg: dict, resolve=lambda p: p):
        c = cfg.get("weak_labels", {}) if isinstance(cfg.get("weak_labels"), dict) else {}
        self.path = resolve(c.get("path", "data/weak_labels.json"))
        self.fetch = bool(c.get("fetch_ms365", False))
        self.max_age_s = float(c.get("max_age_s", 7 * 86400))
        self.nets: list[tuple] = []
        self.meta: dict = {}
        self.cache: dict[str, dict | None] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        nets = []
        for r in doc.get("entries", []):
            try:
                nets.append((ipaddress.ip_network(r["net"], strict=False), r))
            except (KeyError, ValueError):
                continue
        self.nets = nets
        self.meta = {k: doc.get(k) for k in ("fetched", "source", "version")}
        self.cache.clear()

    def refresh_ms365(self, opener=None) -> dict:
        url = MS365_URL.format(rid=uuid.uuid4())
        op = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(url, timeout=20) as r:
            data = json.loads(r.read())
        entries = from_ms365(data)
        doc = {"fetched": time.time(), "source": "Microsoft 365 IP Address and URL web service", "entries": entries}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, self.path)
        self.load()
        return {"ok": True, "entries": len(entries)}

    def maybe_refresh(self) -> None:
        if not self.fetch:
            return
        if self.meta.get("fetched") and time.time() - float(self.meta["fetched"]) < self.max_age_s:
            return
        try:
            self.refresh_ms365()
        except Exception:  # noqa: BLE001 -- best effort; keep the old list
            self.meta["fetched"] = time.time() - self.max_age_s + 3600      # retry in an hour

    def lookup(self, ip: str, proto: int | None = None) -> dict | None:
        if not self.nets:
            return None
        key = f"{ip}/{proto}"
        if key in self.cache:
            return self.cache[key]
        hit = None
        try:
            a = ipaddress.ip_address(ip)
            for n, r in self.nets:
                if a.version == n.version and a in n and (proto != 17 or r.get("udp", True)):
                    hit = r
                    break
        except ValueError:
            pass
        if len(self.cache) > 4096:
            self.cache.clear()
        self.cache[key] = hit
        return hit
