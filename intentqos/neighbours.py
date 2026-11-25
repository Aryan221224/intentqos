"""Dual-stack devices: which IPv4 and IPv6 addresses belong to the same device.

Everything the household sees is per device (names, leases, pause, fairness, experience), but packets carry
addresses, and on a dual-stack line one device has one IPv4 address and several IPv6 addresses: a stable one
and RFC 8981 temporary addresses that change every day or so. Indian fibre lines (Jio, Airtel) hand out IPv6
by default, and Google Meet, YouTube and most CDNs prefer it, so without this a lease on the work laptop's
IPv4 address would not cover its IPv6 call, the device list would show the laptop several times without a
name, and a paused tablet would keep browsing over IPv6.

The kernel's neighbour table (ARP for IPv4, NDP for IPv6) says which link-layer address each LAN address
belongs to. An IPv6 address joins the device with the same MAC; the device key is that device's IPv4 address
(the address the household, the DHCP records and the leases already use).

One MAC is not always one device. A Wi-Fi range extender in repeater mode answers for every client behind it
with its own MAC, and the lab puts all household devices on one interface. So IPv4 addresses are never merged
with each other, and an IPv6 address is grouped only when its MAC has exactly one IPv4 address; otherwise it
stays its own device (the behaviour before this module). IPv6-only devices are not grouped either.

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


def lan_prefixes6(iface: str, path: str = "/proc/net/if_inet6") -> list[str]:
    """The IPv6 prefixes on the LAN interface (global and ULA; not link-local), e.g. ["2401:4900:1c2a:5f00::/64"].

    On OpenWrt the LAN gets its global prefix from the ISP by DHCPv6 prefix delegation, and the ISP can change it
    on every reconnect, so it cannot be written into the configuration at install time like the IPv4 subnet.
    Deprecated prefixes are kept: devices go on using their old addresses for a while after a change."""
    out = set()
    try:
        with open(path) as f:
            for ln in f:
                p = ln.split()
                if len(p) < 6 or p[5] != iface or int(p[3], 16) != 0:     # scope 0 = global (ULA included)
                    continue
                plen = int(p[2], 16)
                if not 16 <= plen <= 120:                                  # a /128 is one address, not the LAN
                    continue
                addr = ":".join(p[0][i:i + 4] for i in range(0, 32, 4))
                out.add(str(ipaddress.ip_network(f"{addr}/{plen}", strict=False)))
    except (OSError, ValueError):
        pass
    return sorted(out)


def merge_subnets(configured: list[str], extra: list[str]) -> list[str]:
    """configured + the extra prefixes that no configured subnet already covers."""
    nets = [ipaddress.ip_network(s, strict=False) for s in configured]
    out = list(configured)
    for e in extra:
        n = ipaddress.ip_network(e, strict=False)
        if not any(n.version == c.version and n.subnet_of(c) for c in nets):
            out.append(e)
            nets.append(n)
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
        self.shared_macs = 0                       # MACs with several IPv4 addresses (extenders): not grouped
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
        v4s: dict[str, list] = {}
        for ip, mac in mac_of.items():
            if ":" not in ip:
                v4s.setdefault(mac, []).append(ip)
        key_of = {}
        for ip, mac in mac_of.items():
            own = v4s.get(mac, [])
            key_of[ip] = own[0] if ":" in ip and len(own) == 1 else ip
        self.mac_of, self.key_of = mac_of, key_of
        self.shared_macs = sum(1 for v in v4s.values() if len(v) > 1)
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
                "ipv6_grouped_with_ipv4": grouped, "devices": len(set(self.key_of.values())),
                "shared_macs": self.shared_macs}
