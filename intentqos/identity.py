"""Device identity: what kind of device each LAN address is, from DHCP and the MAC address.

Used for policy priors and for the household app (a camera's uploads default to bulk, a games console
is a likely gaming device, a work laptop is where call boosts get offered). Sources, all local:

* DHCP: the parameter request list (option 55, the classic "DHCP fingerprint"), the vendor class
  (option 60) and the host name. On OpenWrt a hotplug hook (router/kit/files/etc/hotplug.d/dhcp/
  90-intentqos) appends dnsmasq's values to /tmp/intentqos/dhcp.jsonl; the lease file supplies names.
* MUD (RFC 8520): a device may announce a Manufacturer Usage Description URL in DHCP option 161; when
  dnsmasq passes it on, it is recorded and shown (the URL names the maker's own description).
* MAC address: locally administered addresses are randomised "private" addresses (phones, laptops);
  a few well-known vendor prefixes are listed below.

The rules here are a small built-in set (a stand-in for a fingerprint database such as Fingerbank,
whose data has its own licence). A type the household sets in the console always wins.
"""
from __future__ import annotations

import json
import os
import re

TYPES = ("phone", "laptop", "tv", "console", "camera", "speaker", "printer", "iot", "other")

# host-name patterns (case-insensitive) -> type
HOSTNAME_RULES = [
    (r"iphone|android|galaxy|pixel|redmi|oneplus|vivo|oppo|realme|^sm-", "phone"),
    (r"macbook|laptop|^desktop-|^laptop-|thinkpad|surface|ubuntu|fedora|-pc$", "laptop"),
    (r"chromecast|firetv|fire-tv|roku|appletv|apple-tv|bravia|smart-?tv|webos|tizen|mibox|androidtv", "tv"),
    (r"ps[345]|playstation|xbox|nintendo|switch|steamdeck", "console"),
    (r"cam|ipc|dvr|nvr|doorbell|ring-", "camera"),
    (r"echo|alexa|google-?home|nest-?(mini|audio)|homepod|sonos", "speaker"),
    (r"printer|epson|canon|brother|hp[0-9a-f]{6}", "printer"),
    (r"esp[-_]|tasmota|shelly|tuya|smartplug|bulb|wiz", "iot"),
]
# DHCP vendor class (option 60) prefixes -> type
VENDOR_RULES = [("android-dhcp", "phone"), ("MSFT", "laptop"), ("dhcpcd", "iot"), ("udhcp", "iot"),
                ("PS4", "console"), ("PS5", "console"), ("Nintendo", "console")]
# option-55 fingerprints -> type: none built in. An operator can supply a file of verified fingerprints
# ({"1,3,6,15": "laptop", ...}) via [identity] fingerprints_path; we do not ship guesses as facts.
FINGERPRINTS: dict[str, str] = {}
# vendor MAC prefixes (OUI) -> type: only unambiguous single-purpose makers
OUI = {"b8:27:eb": "iot", "dc:a6:32": "iot"}      # Raspberry Pi boards

# what a device type suggests for policy and offers
PRIORS = {"console": {"likely": "gaming"}, "tv": {"likely": "streaming"}, "camera": {"uploads": "bulk"},
          "laptop": {"offers": True}, "phone": {"offers": True}}


def private_mac(mac: str) -> bool:
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def guess(hostname: str = "", vendor: str = "", opts: str = "", mac: str = "",
          fingerprints: dict | None = None) -> tuple[str, str]:
    """(type, reason) from the strongest evidence available."""
    o = (opts or "").replace(" ", "")
    fp = fingerprints if fingerprints is not None else FINGERPRINTS
    if o and o in fp:
        return fp[o], "DHCP fingerprint"
    for pre, t in VENDOR_RULES:
        if vendor and vendor.startswith(pre):
            return t, f"DHCP vendor class {vendor.split()[0]}"
    h = (hostname or "").lower()
    for rx, t in HOSTNAME_RULES:
        if h and re.search(rx, h):
            return t, f"host name '{hostname}'"
    if mac and mac[:8].lower() in OUI:
        return OUI[mac[:8].lower()], "vendor MAC prefix"
    if mac and private_mac(mac):
        return "phone", "private (randomised) MAC address, typical of phones and laptops"
    return "other", "no identifying DHCP or MAC information"


class Identity:
    def __init__(self, cfg: dict):
        c = cfg.get("identity", {}) if isinstance(cfg.get("identity"), dict) else {}
        self.hook_path = c.get("dhcp_hook_path", "/tmp/intentqos/dhcp.jsonl")
        self.leases_path = c.get("leases_path", "/tmp/dhcp.leases")
        self.seen: dict[str, dict] = {}          # ip -> record
        self._pos = 0
        self.fingerprints = dict(FINGERPRINTS)
        if c.get("fingerprints_path"):
            try:
                with open(c["fingerprints_path"]) as f:
                    self.fingerprints.update({str(k): str(v) for k, v in json.load(f).items() if v in TYPES})
            except (OSError, ValueError, AttributeError):
                pass

    def _read_hook(self) -> None:
        try:
            with open(self.hook_path) as f:
                f.seek(self._pos)
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    ip = r.get("ip")
                    if ip:
                        self.seen.setdefault(ip, {}).update({k: v for k, v in r.items() if v})
                self._pos = f.tell()
            if os.path.getsize(self.hook_path) > 512 * 1024:      # tmpfs: keep it small
                open(self.hook_path, "w").close()
                self._pos = 0
        except OSError:
            pass

    def _read_leases(self) -> None:
        try:
            with open(self.leases_path) as f:
                for ln in f:
                    p = ln.split()
                    if len(p) >= 4:
                        rec = self.seen.setdefault(p[2], {})
                        rec.setdefault("mac", p[1].lower())
                        if p[3] != "*":
                            rec.setdefault("hostname", p[3])
        except OSError:
            pass

    def refresh(self) -> dict[str, dict]:
        self._read_leases()
        self._read_hook()
        out = {}
        for ip, r in self.seen.items():
            t, why = guess(r.get("hostname", ""), r.get("vendor", ""), r.get("opts", ""), r.get("mac", ""),
                           self.fingerprints)
            out[ip] = {"type": t, "why": why, "hostname": r.get("hostname"), "mac": r.get("mac"),
                       "mud_url": r.get("mud"), "private_mac": private_mac(r.get("mac", "")),
                       "priors": PRIORS.get(t, {})}
        return out
