"""Dual-stack devices: which IPv4 and IPv6 addresses belong to the same device.

Everything the household sees is per device (names, leases, pause, fairness, experience), but packets carry
addresses, and on a dual-stack line one device has one IPv4 address and several IPv6 addresses: a stable one
and RFC 8981 temporary addresses that change every day or so. Indian fibre lines (Jio, Airtel) hand out IPv6
by default, and Google Meet, YouTube and most CDNs prefer it, so without this a lease on the work laptop's
IPv4 address would not cover its IPv6 call, the device list would show the laptop several times without a
name, and a paused tablet would keep browsing over IPv6.

The kernel's neighbour table (ARP for IPv4, NDP for IPv6) says which link-layer address each LAN address
belongs to. Addresses with the same MAC are one device. The device key is the device's IPv4 address when it
has one (the address the household, the DHCP records and the leases already use); an IPv6-only device keeps
the first address it was seen with, so its key does not change when its temporary address rotates.

Inputs: the neighbour table (one netlink RTM_GETNEIGH dump, `ip neigh` as fallback), the DHCP records from
identity.py (ip -> mac). Output: key(ip) -> device key. Failure: no table -> every address is its own device
(the behaviour before this module), never an error in the control loop.
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import subprocess
import time

_RTM_GETNEIGH, _NLM_F_REQUEST, _NLM_F_DUMP, _NLMSG_DONE, _NLMSG_ERROR = 30, 0x1, 0x300, 3, 2
_NDA_DST, _NDA_LLADDR = 1, 2
_NUD_INCOMPLETE, _NUD_FAILED, _NUD_NOARP = 0x01, 0x20, 0x40


def parse_neigh(data: bytes, out: list) -> bool:
    """Append (ip, mac) from one netlink receive buffer of RTM_NEWNEIGH messages; True when the dump is done."""
    off = 0
    while off + 16 <= len(data):
        ln, ty, _fl, _seq, _pid = struct.unpack_from("IHHII", data, off)
        if ln < 16 or ty == _NLMSG_DONE:
            return True
        if ty == _NLMSG_ERROR:
            raise OSError("netlink error")
        fam, _p1, _p2, _idx, state, _flags, _t = struct.unpack_from("BBHiHBB", data, off + 16)
        if not state & (_NUD_INCOMPLETE | _NUD_FAILED | _NUD_NOARP):
            a, p, end = {}, off + 28, off + ln
            while p + 4 <= end:
                al, at = struct.unpack_from("HH", data, p)
                if al < 4:
                    break
                a[at & 0x3FFF] = data[p + 4:p + al]
                p += (al + 3) & ~3
            dst, ll = a.get(_NDA_DST), a.get(_NDA_LLADDR)
            if dst and ll and len(ll) == 6 and fam in (socket.AF_INET, socket.AF_INET6):
                out.append((socket.inet_ntop(fam, dst), ":".join(f"{b:02x}" for b in ll)))
        off += (ln + 3) & ~3
    return False


def neigh_netlink() -> list[tuple[str, str]]:
    """[(ip, mac)] from the kernel neighbour table, both families, in one dump. Raises OSError."""
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)    # NETLINK_ROUTE
    try:
        s.settimeout(1.0)
        ndm = struct.pack("BBHiHBB", socket.AF_UNSPEC, 0, 0, 0, 0, 0, 0)
        s.send(struct.pack("IHHII", 16 + len(ndm), _RTM_GETNEIGH, _NLM_F_REQUEST | _NLM_F_DUMP, 1, 0) + ndm)
        out: list = []
        while not parse_neigh(s.recv(1 << 16), out):
            pass
        return out
    finally:
        s.close()


def neigh_ip() -> list[tuple[str, str]]:
    """Fallback: `ip neigh show` (both families)."""
    r = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=3)
    out = []
    for ln in r.stdout.splitlines():
        p = ln.split()
        if "lladdr" in p and p[-1] not in ("FAILED", "INCOMPLETE"):
            out.append((p[0], p[p.index("lladdr") + 1].lower()))
    return out


def _usable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_multicast or a.is_unspecified or a.is_loopback)


class DeviceKeys:
    """ip -> device key, from the neighbour table and the DHCP records (refreshed every `every` seconds)."""

    def __init__(self, every: float = 5.0, reader=None):
        self.every = every
        self.reader = reader                       # tests inject a function returning [(ip, mac)]
        self.t = -1e9
        self.mac_of: dict[str, str] = {}           # ip -> mac (current table + DHCP)
        self.key_of: dict[str, str] = {}           # ip -> device key
        self.first6: dict[str, str] = {}           # mac -> first IPv6 address seen (key of an IPv6-only device)
        self.source = "none"

    def _read(self) -> list[tuple[str, str]]:
        if self.reader is not None:
            return self.reader()
        if self.source != "ip":
            try:
                rows = neigh_netlink()
                self.source = "netlink"
                return rows
            except OSError:
                self.source = "ip"                 # e.g. no NETLINK_ROUTE access: use `ip` from now on
        try:
            return neigh_ip()
        except (OSError, subprocess.SubprocessError):
            return []

    def refresh(self, idents: dict | None = None, now: float | None = None, force: bool = False) -> bool:
        """Re-read the table when due. idents: identity.refresh() output (ip -> {"mac": ...}). True if read."""
        now = time.monotonic() if now is None else now
        if not force and now - self.t < self.every:
            return False
        self.t = now
        mac_of = {ip: mac for ip, mac in self._read() if _usable(ip) and mac != "00:00:00:00:00:00"}
        for ip, r in (idents or {}).items():       # DHCP records: a device that is quiet right now still counts
            m = (r or {}).get("mac")
            if m and ":" not in ip:
                mac_of.setdefault(ip, str(m).lower())
        v4_of: dict[str, str] = {}
        for ip, mac in sorted(mac_of.items()):
            if ":" not in ip:
                v4_of.setdefault(mac, ip)
        key_of = {}
        for ip, mac in mac_of.items():
            if ":" in ip:
                key_of[ip] = v4_of.get(mac) or self.first6.setdefault(mac, ip)
            else:
                key_of[ip] = v4_of.get(mac, ip)
        self.mac_of, self.key_of = mac_of, key_of
        return True

    def key(self, ip: str) -> str:
        return self.key_of.get(ip, ip)

    def addresses(self, key: str) -> list[str]:
        """Every address of the device with this key (the key itself first)."""
        return [key] + sorted(ip for ip, k in self.key_of.items() if k == key and ip != key)

    def summary(self) -> dict:
        n6 = sum(1 for ip in self.mac_of if ":" in ip)
        grouped = sum(1 for ip, k in self.key_of.items() if ":" in ip and ":" not in k)
        return {"source": self.source, "addresses": len(self.mac_of), "ipv6_addresses": n6,
                "ipv6_grouped_with_ipv4": grouped, "devices": len(set(self.key_of.values()))}
