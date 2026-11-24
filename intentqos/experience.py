"""Experience sensing: how each app is doing, from packet headers alone.

The classifier says what a flow is; this module says how it is going, using the per-window
experience fields the capture keeps (capture.XFIELDS):

* **Latency** of each app as a ΔQ distribution (deltaq.py):
    - TCP flows: measured, from TCP timestamps (router -> server -> router, which includes our queues)
      plus the device's LAN RTT (router -> device -> router),
    - QUIC flows: measured, from the spin bit when the endpoints spin it,
    - other UDP (calls, games): composed from the device's LAN RTT, our queue for the flow's tin in
      each direction, and the internet path delay the capacity estimator measures.
* **Loss**: TCP retransmissions (or reordering) per data packet; for UDP, drops in its tin.
* **Calls**: video frames per second in each direction from bursts of video-size packets
  (header-only, after the IP/UDP heuristic in Sharma et al., "Estimating WebRTC Video QoE Metrics
  Without Using Application Headers", IMC 2023), and freezes (windows with packets but no frames).
* **Streaming video**: bitrate and a resolution class from it, and a start-up estimate (time until
  the first pause in the download, i.e. the player's buffer filled).
* **L4S**: share of the flow's packets marked ECT(1) or CE.

Flows are grouped into **app sessions** (same device, same class, same remote network) and each session
gets a Quality of Outcome score (qoo.py). Everything here is an estimate from timing and sizes, labelled
as such in the console; nothing reads payload.
"""
from __future__ import annotations

import ipaddress
import time

from . import TIN_NAMES
from .capture import WINDOW_S, Flow
from .deltaq import Dist, compose
from .qoo import grade, requirements, score

CALL_LABELS = ("voip", "video")


def _prefix(ip: str) -> str:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    return str(ipaddress.ip_network(f"{ip}/{24 if a.version == 4 else 48}", strict=False))


class FlowXP:
    """Rolling experience state of one flow, kept in Flow.meta['xp']."""
    __slots__ = ("wan", "spin", "last_w", "retx", "pk", "ect1", "ecn_pk", "dscp", "fr_dn", "fr_up", "vid_win",
                 "startup_s", "on_s")

    def __init__(self):
        self.wan, self.spin = Dist(cap=96), Dist(cap=64)
        self.last_w = None             # the window folded in last (each closed window is folded once)
        self.retx = self.pk = self.ect1 = self.ecn_pk = 0
        self.dscp = None
        self.fr_dn: list[int] = []     # frames per window, last 8 windows
        self.fr_up: list[int] = []
        self.vid_win: list[int] = []   # packets per window (to tell "no frames" from "no packets")
        self.startup_s = None
        self.on_s = 0.0


class ExperienceTracker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.req = requirements(cfg)
        self.lan: dict[str, Dist] = {}          # device ip -> LAN RTT (Wi-Fi / cable)
        self.queues = {"down": {}, "up": {}}    # tin name -> Dist of queue delay (ms)
        self.path = Dist(cap=128)               # internet path RTT (estimator: baseline + delay rise)
        self.snapshot: dict = {"apps": [], "devices": {}, "path": {}, "queues": {}}

    # ------------------------------------------------------------------ inputs
    def observe_queues(self, tin_view: dict) -> None:
        for d in ("down", "up"):
            for name, v in (tin_view.get(d) or {}).items():
                q = self.queues[d].setdefault(name, Dist(cap=64))
                q.add(max(0.0, float(v.get("queue_ms") or 0.0)))

    def observe_path(self, est) -> None:
        """Internet path RTT from the capacity estimator: unloaded baseline plus the current rise."""
        if est is None or not getattr(est, "enabled", False):
            return
        b = est.baseline
        ups = [v for (r, k), v in b.items() if k == "up"]
        dns = [v for (r, k), v in b.items() if k == "down"]
        rtts = [v for (r, k), v in b.items() if k == "rtt"]
        if ups and dns:
            base = min(ups) + min(dns)
        elif rtts:
            base = min(rtts)
        else:
            return
        # one-way clocks can be offset; the RTT floor is what matters, so never let it go negative
        self.path.add(max(0.5, base + max(0.0, est.down.delta) + max(0.0, est.up.delta)))

    def _fold(self, f: Flow) -> FlowXP:
        xp = f.meta.get("xp")
        if xp is None:
            xp = f.meta["xp"] = FlowXP()
        # every policy step closes one window per flow (capture.roll_windows): fold in the newest, once
        w = f.hist[-1] if f.hist else None
        if w is not None and w is not xp.last_w:
            xp.last_w = w
            pk = w.pk_up + w.pk_dn
            if getattr(w, "rw_n", 0):
                xp.wan.add(w.rw_s / w.rw_n, n=min(3, w.rw_n))
            if getattr(w, "rl_n", 0):
                self.lan.setdefault(f.dev, Dist(cap=128)).add(w.rl_s / w.rl_n, n=min(3, w.rl_n))
            if getattr(w, "sp_n", 0) and w.sp_n * 4 <= max(1, w.pk_dn):     # many edges = a random (disabled) bit
                xp.spin.add(w.sp_s / w.sp_n)
            xp.retx += getattr(w, "retx_up", 0) + getattr(w, "retx_dn", 0)
            xp.pk += pk
            e1 = getattr(w, "ect1", 0) + getattr(w, "ce", 0)
            xp.ect1 += e1
            xp.ecn_pk += pk
            if getattr(w, "dscp_up", 255) != 255:
                xp.dscp = w.dscp_up
            xp.fr_dn = (xp.fr_dn + [getattr(w, "fr_dn", 0)])[-8:]
            xp.fr_up = (xp.fr_up + [getattr(w, "fr_up", 0)])[-8:]
            xp.vid_win = (xp.vid_win + [w.pk_dn])[-8:]
            if w.by_dn > 0:
                xp.on_s += WINDOW_S
            elif xp.startup_s is None and xp.on_s >= 2.0:
                xp.startup_s = round(time.monotonic() - f.first - WINDOW_S, 1)   # first pause after playback began
        return xp

    # ------------------------------------------------------------------ per policy step
    def update(self, flows: list, decisions: dict, flow_tin: dict, est=None, tin_view: dict | None = None,
               plan: dict | None = None, rt_loss: dict | None = None, names: dict | None = None,
               locate=None) -> dict:
        if tin_view:
            self.observe_queues(tin_view)
        self.observe_path(est)
        sessions: dict[tuple, dict] = {}
        for f in flows:
            xp = self._fold(f)
            dec = decisions.get(f.id())
            label = (dec.committed or dec.label) if dec else None
            if not label:
                continue
            tin = flow_tin.get(f.key)
            k = (f.dev, label, _prefix(f.remote_ip))
            s = sessions.setdefault(k, {"flows": [], "label": label, "ip": f.dev, "tin": tin})
            s["flows"].append((f, xp))
        apps = []
        for (ip, label, _pfx), s in sessions.items():
            apps.append(self._app(ip, label, s, plan, rt_loss or {}, names or {}, locate))
        apps.sort(key=lambda a: (a["qoo"] if a["qoo"] is not None else 101, -a["kbit_down"]))
        self.snapshot = {
            "apps": apps[:40],
            "devices": {ip: {"lan_rtt_ms": {k: round(v, 1) for k, v in d.percentiles((50, 99)).items()},
                             "samples": len(d)} for ip, d in self.lan.items() if len(d)},
            "path": {"rtt_ms": {k: round(v, 1) for k, v in self.path.percentiles((50, 99)).items()},
                     "gsv": self.path.gsv()},
            "queues": {d: {n: {k: round(v, 1) for k, v in q.percentiles((50, 99)).items()} for n, q in qs.items()}
                       for d, qs in self.queues.items()},
            "requirements_source": "IETF draft-ietf-ippm-qoo method; IntentQoS default thresholds",
        }
        return self.snapshot

    def latency(self, ip: str, flows: list, tin: int | None) -> tuple[Dist, str]:
        """ΔQ of an app: measured where the flow carries timestamps or a spin bit, composed otherwise."""
        lan = self.lan.get(ip)
        wan = Dist(cap=192)
        for _f, xp in flows:
            for x in xp.wan.samples:
                wan.add(x)
            for x in xp.spin.samples:
                wan.add(x)
        if len(wan) >= 3:
            return compose(lan, wan) if lan is not None and len(lan) else wan, "measured"
        tn = TIN_NAMES.get(tin, "besteffort")
        return compose(lan, self.queues["down"].get(tn), self.queues["up"].get(tn), self.path), "estimated"

    def _app(self, ip: str, label: str, s: dict, plan, rt_loss: dict, names: dict, locate) -> dict:
        flows = s["flows"]
        lat, source = self.latency(ip, flows, s["tin"])
        pk = sum(xp.pk for _f, xp in flows)
        retx = sum(xp.retx for _f, xp in flows)
        tcp = any(f.proto == 6 for f, _ in flows)
        if tcp and pk:
            loss = 100.0 * retx / pk
        else:
            loss = 100.0 * max(rt_loss.get("down", 0.0), rt_loss.get("up", 0.0)) if s["tin"] in (0, 1, 2) else None
        from .features import recent_rates
        kd = sum(recent_rates(f)[1] for f, _ in flows)
        ku = sum(recent_rates(f)[0] for f, _ in flows)
        avail = self._available(plan, s["tin"])
        pct = lat.percentiles((50, 90, 99))
        sc = score(self.req.get(label, {}), pct, loss, avail)
        app = {"ip": ip, "device": names.get(ip, ""), "label": label, "flows": len(flows),
               "tin": TIN_NAMES.get(s["tin"]) if s["tin"] is not None else None,
               "qoo": sc["qoo"], "grade": grade(sc["qoo"]), "worst": sc["worst"], "parts": sc["parts"],
               "latency_ms": {k: round(v, 1) for k, v in pct.items()}, "latency_source": source,
               "loss_pct": round(loss, 3) if loss is not None else None,
               "kbit_down": round(kd, 1), "kbit_up": round(ku, 1),
               "l4s": self._l4s(flows), "dscp": next((xp.dscp for _f, xp in flows if xp.dscp), None)}
        if label in CALL_LABELS:
            app["call"] = self._call(flows)
        if label == "streaming":
            app["video"] = self._video(flows, kd)
        if locate is not None:
            app["bottleneck"] = locate(ip)
        return app

    @staticmethod
    def _available(plan, tin) -> float | None:
        if not plan or tin is None or "down" not in plan:
            return None
        g = plan["down"]["guarantee"]
        # an app can count on its tin's guarantee plus whatever the lower tins do not use; report the guarantee
        # plus the best-effort share as a conservative "available" figure
        return float(g.get(tin, 0.0)) + float(plan["down"]["rate"]) * 0.25

    @staticmethod
    def _l4s(flows) -> bool:
        e = sum(xp.ect1 for _f, xp in flows)
        n = sum(xp.ecn_pk for _f, xp in flows)
        return n >= 20 and e / n >= 0.5

    @staticmethod
    def _call(flows) -> dict:
        """Frames per second each way from bursts of video-size packets over the last 4 s."""
        f, xp = max(flows, key=lambda x: sum(x[1].vid_win))
        span = max(1, len(xp.fr_dn)) * WINDOW_S
        fps_dn = sum(xp.fr_dn) / span
        fps_up = sum(xp.fr_up) / span
        frozen = sum(1 for fr, pk in zip(xp.fr_dn, xp.vid_win) if pk > 0 and fr == 0)
        if fps_dn < 1 and fps_up < 1:
            tier, kind = "audio only", "voice"
        else:
            kind = "video"
            tier = "smooth" if fps_dn >= 24 else "ok" if fps_dn >= 15 else "choppy" if fps_dn >= 5 else "frozen"
        return {"kind": kind, "fps_down": round(fps_dn, 1), "fps_up": round(fps_up, 1), "tier": tier,
                "frozen_windows": frozen, "method": "estimated from packet bursts (header-only)"}

    @staticmethod
    def _video(flows, kbit_down: float) -> dict:
        f, xp = max(flows, key=lambda x: x[1].on_s)
        res = ("low (480p or less)" if kbit_down < 1500 else "HD (720p)" if kbit_down < 4000
               else "Full HD (1080p)" if kbit_down < 9000 else "4K class")
        return {"bitrate_kbit": round(kbit_down), "resolution_class": res, "startup_s": xp.startup_s,
                "method": "estimated from download bitrate and pauses (header-only)"}
