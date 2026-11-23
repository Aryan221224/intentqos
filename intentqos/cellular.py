"""Cellular-aware rate ceiling for 4G/5G backup or fixed-wireless links.

On a cellular link the capacity moves with the radio: signal quality, not only congestion. Waiting
for queues to build (bufferbloat) before cutting the rate means every fade is felt first. When the
router has its own modem, ModemManager reports the radio's signal; this module turns it into a
capacity ceiling the estimator respects, and flags sharp fades so the rate is cut before the queue
grows. Lumos5G (Narayanan et al., IMC 2020) showed that device-side signal and context predict cellular
throughput far better than history alone; this is a deliberately simple version of that idea:

    ceiling = bandwidth x spectral efficiency(SINR) x share,   efficiency = min(5.5, 0.75 log2(1 + SINR))

with the channel bandwidth and the share of the cell this home gets as settings ([cellular] in the
config). The phone-hotspot setup of the home kit has no modem on the router, so this stays off there.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import time


def spectral_efficiency(sinr_db: float) -> float:
    lin = 10 ** (sinr_db / 10.0)
    return max(0.0, min(5.5, 0.75 * math.log2(1.0 + lin)))


def ceiling_kbit(sinr_db: float, bandwidth_mhz: float, share: float) -> float:
    return bandwidth_mhz * 1e6 * spectral_efficiency(sinr_db) * share / 1000.0


def parse_mmcli_signal(text: str) -> dict:
    """`mmcli -J -m any --signal-get` -> {"tech", "rsrp", "rsrq", "sinr"} (dB / dBm), from the newest technology."""
    try:
        sig = json.loads(text)["modem"]["signal"]
    except (ValueError, KeyError, TypeError):
        return {}
    for tech in ("5g", "lte", "umts"):
        t = sig.get(tech) or {}
        vals = {}
        for k_in, k_out in (("rsrp", "rsrp"), ("rsrq", "rsrq"), ("snr", "sinr"), ("sinr", "sinr"), ("rssi", "rssi")):
            v = t.get(k_in)
            if v not in (None, "--", ""):
                try:
                    vals[k_out] = float(v)
                except (TypeError, ValueError):
                    pass
        if "sinr" in vals or "rsrp" in vals:
            vals["tech"] = tech
            return vals
    return {}


class CellularCeiling:
    def __init__(self, cfg: dict, runner=None):
        c = cfg.get("cellular", {}) if isinstance(cfg.get("cellular"), dict) else {}
        self.enabled = bool(c.get("enabled", False))
        self.bandwidth_mhz = float(c.get("bandwidth_mhz", 20.0))
        self.share = float(c.get("share", 0.35))
        self.fade_db = float(c.get("fade_db", 6.0))
        self.mmcli = shutil.which("mmcli")
        self.run = runner or self._run
        self.last: dict = {}
        self.hist: list[tuple[float, float]] = []      # (t, sinr)
        self.t = 0.0
        self._setup = False

    def _run(self, args: list[str]) -> str:
        return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout

    def poll(self, now: float | None = None) -> dict | None:
        """Read the radio at most every 2 s; returns {"ceiling_kbit", "sinr", "fade"} or None."""
        if not self.enabled or not (self.mmcli or self.run is not self._run):
            return None
        now = time.monotonic() if now is None else now
        if now - self.t < 2.0 and self.last:
            return self.last
        self.t = now
        try:
            if not self._setup:
                self.run(["mmcli", "-m", "any", "--signal-setup=2"])
                self._setup = True
            sig = parse_mmcli_signal(self.run(["mmcli", "-J", "-m", "any", "--signal-get"]))
        except (OSError, subprocess.SubprocessError):
            return None
        if "sinr" not in sig:
            return None
        self.hist.append((now, sig["sinr"]))
        self.hist = [(t, s) for t, s in self.hist if now - t <= 10.0]
        recent_max = max(s for _t, s in self.hist)
        fade = recent_max - sig["sinr"] >= self.fade_db
        self.last = {"ceiling_kbit": round(ceiling_kbit(sig["sinr"], self.bandwidth_mhz, self.share)),
                     "sinr": sig["sinr"], "rsrp": sig.get("rsrp"), "tech": sig.get("tech"), "fade": fade,
                     "method": "SINR -> spectral efficiency x bandwidth x share"}
        return self.last
