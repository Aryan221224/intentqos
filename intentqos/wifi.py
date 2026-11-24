"""Whole-home bottleneck locator: is a device held back by Wi-Fi, by the line, or by the ISP?

Passive test after Sundaresan et al., "Home Network or Access Link? Locating Last-Mile Downstream
Throughput Bottlenecks" (PAM 2016), run at the router on downstream TCP traffic:

  1. not enough packets            -> "idle" (no verdict)
  2. packet gaps vary a lot         -> no last-mile bottleneck: the server or the path limits the flow
     (coefficient of variation of inter-arrival times, cvt, >= 0.8)
  3. steady gaps (cvt < 0.8) and LAN RTT > 15 ms -> the Wi-Fi (or the device) is the bottleneck
  4. steady gaps and normal LAN RTT -> the access line is the bottleneck (IntentQoS shapes it)

The LAN RTT is the router -> device -> router part of TCP timestamps (capture.py), so it needs no
probe traffic. On OpenWrt the radio's own per-station counters (`iw station dump`: signal, PHY rates,
retries) are added when available, and turned into advice the household can act on.

Upstream problems (delay that rises on the internet path while the home is quiet, or many homes on one
OLT at once) are judged by the controller, which sees the fleet (controller/core.py, localise()).

RFC 8325 QoS Map: `qos_map_set()` builds the hostapd QoS Map Set that maps DSCP to Wi-Fi user priority
as RFC 8325 section 4.3 recommends (for Wi-Fi at the network edge), so voice marked EF uses the voice
access category instead of the video one. The router installer sets it on each AP interface.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time

from .capture import WINDOW_S

CVT_MAX = 0.8          # below: packets arrive paced by a bottleneck upstream of the router's LAN side
LAN_RTT_MS = 15.0      # above (median): the Wi-Fi link is the bottleneck
MIN_PPS = 100.0        # fewer downstream packets per second than this: not enough demand to judge

# RFC 8325 section 4.3, Figure 1, Wi-Fi at the edge (CS6/CS7 -> UP 0, section 8.2); unused codepoints -> UP 0
RFC8325 = [(46, 6), (44, 6), (40, 5), (34, 4), (36, 4), (38, 4), (32, 4), (26, 4), (28, 4), (30, 4), (24, 4),
           (18, 3), (20, 3), (22, 3), (16, 0), (10, 0), (12, 0), (14, 0), (0, 0), (8, 1), (48, 0)]


def qos_map_set() -> str:
    """hostapd `qos_map_set`: up to 21 DSCP,UP exceptions, then (low,high) DSCP ranges for UP 0..7."""
    ex = ",".join(f"{d},{u}" for d, u in RFC8325)
    ranges = ["0,63"] + ["255,255"] * 7          # anything not listed above -> UP 0 (best effort)
    return ex + "," + ",".join(ranges)


def cvt_of(windows) -> tuple[float | None, float]:
    """Coefficient of variation of downstream packet gaps over closed windows, and downstream packets/s."""
    n = sum(w.iat_n_dn for w in windows)
    if n < 2:
        return None, 0.0
    s = sum(w.iat_s_dn for w in windows)
    q = sum(w.iat_q_dn for w in windows)
    mean = s / n
    var = max(0.0, q / n - mean * mean)
    pps = sum(w.pk_dn for w in windows) / max(WINDOW_S, len(windows) * WINDOW_S)
    return ((var ** 0.5) / mean if mean > 0 else None), pps


class Stations:
    """Per-station Wi-Fi counters from `iw` (OpenWrt), keyed by MAC; refreshed at most every 5 s."""

    def __init__(self):
        self.iw = shutil.which("iw")
        self.data: dict[str, dict] = {}
        self.t = 0.0

    @staticmethod
    def parse(text: str, iface: str = "") -> dict[str, dict]:
        out: dict[str, dict] = {}
        cur = None
        for ln in text.splitlines():
            m = re.match(r"Station ([0-9a-f:]{17})", ln.strip(), re.I)
            if m:
                cur = out.setdefault(m.group(1).lower(), {"iface": iface})
                continue
            if cur is None or ":" not in ln:
                continue
            k, v = (x.strip() for x in ln.split(":", 1))
            num = re.match(r"(-?[\d.]+)", v)
            if not num:
                continue
            x = float(num.group(1))
            if k == "signal":
                cur["signal_dbm"] = x
            elif k == "tx bitrate":
                cur["tx_mbit"] = x
            elif k == "rx bitrate":
                cur["rx_mbit"] = x
            elif k == "expected throughput":
                cur["expected_mbit"] = x
            elif k == "tx retries":
                cur["tx_retries"] = int(x)
            elif k == "tx packets":
                cur["tx_packets"] = int(x)
            elif k == "tx failed":
                cur["tx_failed"] = int(x)
        for st in out.values():
            if st.get("tx_packets"):
                st["retry_pct"] = round(100.0 * st.get("tx_retries", 0) / st["tx_packets"], 1)
        return out

    def refresh(self) -> dict[str, dict]:
        if not self.iw or time.monotonic() - self.t < 5.0:
            return self.data
        self.t = time.monotonic()
        data: dict[str, dict] = {}
        try:
            devs = subprocess.run([self.iw, "dev"], capture_output=True, text=True, timeout=2).stdout
            for iface in re.findall(r"Interface (\S+)", devs):
                txt = subprocess.run([self.iw, "dev", iface, "station", "dump"], capture_output=True, text=True,
                                     timeout=2).stdout
                data.update(self.parse(txt, iface))
        except (OSError, subprocess.SubprocessError):
            pass
        self.data = data
        return data


def arp_table(path: str = "/proc/net/arp") -> dict[str, str]:
    """ip -> mac from the kernel's neighbour table (IPv4)."""
    out = {}
    try:
        with open(path) as f:
            next(f, None)
            for ln in f:
                p = ln.split()
                if len(p) >= 4 and p[3] != "00:00:00:00:00:00":
                    out[p[0]] = p[3].lower()
    except OSError:
        pass
    return out


class BottleneckLocator:
    def __init__(self, cfg: dict):
        c = cfg.get("wifi", {}) if isinstance(cfg.get("wifi"), dict) else {}
        self.cvt_max = float(c.get("cvt_max", CVT_MAX))
        self.lan_rtt_ms = float(c.get("lan_rtt_ms", LAN_RTT_MS))
        self.min_pps = float(c.get("min_pps", MIN_PPS))
        self.stations = Stations()
        self.verdicts: dict[str, dict] = {}
        self.history: dict[str, list] = {}

    def update(self, flows: list, lan_rtt: dict, now: float | None = None) -> dict[str, dict]:
        """flows: active capture flows; lan_rtt: ip -> deltaq.Dist of LAN RTT samples (experience.py)."""
        now = time.time() if now is None else now
        by_dev: dict[str, list] = {}
        for f in flows:
            if f.proto == 6:
                by_dev.setdefault(getattr(f, "dev", f.lan_ip), []).append(f)
        st = self.stations.refresh()
        arp = arp_table() if st else {}
        out: dict[str, dict] = {}
        for ip, fl in by_dev.items():
            busiest = max(fl, key=lambda f: sum(w.by_dn for w in list(f.hist)[-8:]))
            ws = list(busiest.hist)[-8:]
            cvt, pps = cvt_of(ws)
            d = lan_rtt.get(ip)
            rtt = d.pct(50) if d is not None and len(d) >= 3 else None
            v = self.judge(cvt, pps, rtt)
            v.update({"cvt": round(cvt, 3) if cvt is not None else None, "pps": round(pps),
                      "lan_rtt_ms": round(rtt, 1) if rtt is not None else None})
            mac = arp.get(ip)
            if mac and mac in st:
                v["radio"] = st[mac]
                v["advice"] = self.advice(v)
            out[ip] = v
            h = self.history.setdefault(ip, [])
            h.append((round(now), v["where"]))
            del h[:-120]
        self.verdicts = out
        return out

    def judge(self, cvt, pps, lan_rtt) -> dict:
        if pps < self.min_pps or cvt is None:
            return {"where": "idle", "why": "not enough traffic to tell"}
        if cvt >= self.cvt_max:
            return {"where": "none", "why": "packet gaps vary: the far end or the internet path limits this flow, "
                                             "not the home's Wi-Fi or line"}
        if lan_rtt is not None and lan_rtt > self.lan_rtt_ms:
            return {"where": "wifi", "why": f"steady packets but {lan_rtt:.0f} ms round trip inside the home: "
                                             "the Wi-Fi link to this device is the bottleneck"}
        return {"where": "line", "why": "steady packets and a quick Wi-Fi: the broadband line is the bottleneck "
                                        "(IntentQoS shapes it)"}

    @staticmethod
    def advice(v: dict) -> str:
        r = v.get("radio") or {}
        sig = r.get("signal_dbm")
        if v["where"] != "wifi":
            return ""
        if sig is not None and sig < -70:
            return f"weak Wi-Fi signal ({sig:.0f} dBm): move closer to the router or add a mesh point"
        if r.get("retry_pct", 0) > 20:
            return f"many Wi-Fi retries ({r['retry_pct']:.0f}%): interference; try the 5 GHz network or another channel"
        return "the Wi-Fi link is busy: try the 5 GHz network or a cable for this device"

    def locate(self, ip: str) -> dict | None:
        v = self.verdicts.get(ip)
        return {"where": v["where"], "why": v["why"]} if v else None

    def snapshot(self) -> dict:
        return {"devices": self.verdicts, "method": "Sundaresan et al. (PAM 2016): cvt < 0.8 and LAN RTT > 15 ms",
                "qos_map": qos_map_set()}

