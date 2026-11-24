"""Dual stack: one device, one IPv4 address and several IPv6 addresses (stable + RFC 8981 temporary).

No root, no network: the neighbour table is injected, and the netlink parser is fed kernel-format messages.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LAPTOP_MAC, TABLET_MAC, SENSOR_MAC = "3c:22:fb:10:00:05", "f0:99:b6:10:00:07", "b8:27:eb:10:00:09"
TABLE = [
    ("192.168.8.5", LAPTOP_MAC),
    ("2401:4900:1c2a:5f00:3e22:fbff:fe10:5", LAPTOP_MAC),          # stable (EUI-64)
    ("2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3", LAPTOP_MAC),       # temporary (privacy) address
    ("fe80::3e22:fbff:fe10:5", LAPTOP_MAC),                        # link-local
    ("192.168.8.7", TABLET_MAC),
    ("2401:4900:1c2a:5f00:51a:9f3:c2d4:8e01", TABLET_MAC),
    ("2401:4900:1c2a:5f00:ba27:ebff:fe10:9", SENSOR_MAC),          # IPv6-only device
]


def nl_neigh(family: int, ip: str, mac: str, state: int = 0x02) -> bytes:
    """One RTM_NEWNEIGH message as the kernel sends it (ndmsg + NDA_DST + NDA_LLADDR)."""
    dst = socket.inet_pton(family, ip)
    ll = bytes(int(x, 16) for x in mac.split(":"))

    def attr(t, v):
        a = struct.pack("HH", 4 + len(v), t) + v
        return a + b"\0" * ((4 - len(a) % 4) % 4)
    body = struct.pack("BBHiHBB", family, 0, 0, 3, state, 0, 1) + attr(1, dst) + attr(2, ll)
    return struct.pack("IHHII", 16 + len(body), 28, 2, 1, 0) + body


class NeighbourParsing(unittest.TestCase):
    def test_kernel_messages_both_families(self):
        from intentqos.neighbours import parse_neigh
        buf = (nl_neigh(socket.AF_INET, "192.168.8.5", LAPTOP_MAC) +
               nl_neigh(socket.AF_INET6, "2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3", LAPTOP_MAC) +
               nl_neigh(socket.AF_INET6, "2401:4900:1c2a:5f00::dead", TABLET_MAC, state=0x20) +   # FAILED
               struct.pack("IHHII", 20, 3, 2, 1, 0) + b"\0" * 4)                                   # DONE
        out = []
        self.assertTrue(parse_neigh(buf, out))
        self.assertEqual(out, [("192.168.8.5", LAPTOP_MAC), ("2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3", LAPTOP_MAC)])


class DeviceGrouping(unittest.TestCase):
    def keys(self, rows=TABLE, idents=None):
        from intentqos.neighbours import DeviceKeys
        d = DeviceKeys(reader=lambda: list(rows))
        d.refresh(idents or {}, force=True)
        return d

    def test_ipv6_addresses_join_the_devices_ipv4_key(self):
        d = self.keys()
        for ip, mac in TABLE:
            if mac == LAPTOP_MAC:
                self.assertEqual(d.key(ip), "192.168.8.5", ip)
        self.assertEqual(d.key("2401:4900:1c2a:5f00:51a:9f3:c2d4:8e01"), "192.168.8.7")
        self.assertEqual(d.addresses("192.168.8.5")[0], "192.168.8.5")
        self.assertEqual(len(d.addresses("192.168.8.5")), 4)
        self.assertEqual(d.summary()["devices"], 3)

    def test_ipv6_only_device_keeps_its_key_when_its_address_rotates(self):
        d = self.keys()
        first = d.key("2401:4900:1c2a:5f00:ba27:ebff:fe10:9")
        rotated = TABLE + [("2401:4900:1c2a:5f00:77aa:1234:5678:9abc", SENSOR_MAC)]
        d.reader = lambda: list(rotated)
        d.refresh({}, force=True)
        self.assertEqual(d.key("2401:4900:1c2a:5f00:77aa:1234:5678:9abc"), first)

    def test_quiet_device_found_through_dhcp_records(self):
        # the laptop's IPv4 has aged out of the ARP table, but DHCP knows its MAC
        rows = [r for r in TABLE if r[0] != "192.168.8.5"]
        d = self.keys(rows, {"192.168.8.5": {"mac": LAPTOP_MAC.upper()}})
        self.assertEqual(d.key("2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3"), "192.168.8.5")

    def test_unknown_address_is_its_own_device(self):
        d = self.keys([])
        self.assertEqual(d.key("2401:4900::1"), "2401:4900::1")
        self.assertEqual(d.key("192.168.8.99"), "192.168.8.99")


class LeaseCoversIPv6(unittest.TestCase):
    def test_work_call_over_ipv6_is_boosted_by_a_lease_on_the_laptop(self):
        from intentqos.capture import Flow
        from intentqos.policy import TIN_BE, TIN_INTENT, TIN_VOICE, flow_tins
        d = DeviceGrouping().keys()
        call6 = Flow(("2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3", "2404:6800:4007:81a::200e", 17, 51000, 3478), 6, 0.0)
        video6 = Flow(("2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3", "2404:6800:4007:81a::200e", 17, 51002, 3478), 6, 0.0)
        tv6 = Flow(("2401:4900:1c2a:5f00:51a:9f3:c2d4:8e01", "2404:6800:4007:81a::2011", 6, 40000, 443), 6, 0.0)
        for f in (call6, video6, tv6):
            f.dev = d.key(f.lan_ip)
        dec = {call6.id(): types.SimpleNamespace(committed="voip"),
               video6.id(): types.SimpleNamespace(committed="video"),
               tv6.id(): types.SimpleNamespace(committed="bulk")}
        lease = types.SimpleNamespace(target="192.168.8.5", goal="low_latency")
        tins = flow_tins([call6, video6, tv6], dec, [lease])
        self.assertEqual(tins[call6.key], TIN_VOICE)
        self.assertEqual(tins[video6.key], TIN_INTENT)
        self.assertNotIn(tins[tv6.key], (TIN_INTENT, TIN_VOICE))
        self.assertNotEqual(TIN_BE, TIN_INTENT)


class PauseCoversIPv6(unittest.TestCase):
    def test_paused_device_is_paused_on_its_ipv6_addresses_and_resumed_on_all(self):
        from intentqos.daemon import Engine
        calls = []
        marker = types.SimpleNamespace(pause=lambda ip, s: calls.append(("pause", ip)),
                                       resume=lambda ip: calls.append(("resume", ip)))
        devreg = types.SimpleNamespace(data={"192.168.8.7": {"paused_until": 0}, "192.168.8.5": {}})
        eng = types.SimpleNamespace(devkeys=DeviceGrouping().keys(), marker=marker, devreg=devreg, _pause_extra={})
        Engine._sync_pauses(eng, 1000.0)
        self.assertEqual(calls, [("pause", "2401:4900:1c2a:5f00:51a:9f3:c2d4:8e01")])
        Engine._sync_pauses(eng, 1001.0)                         # nothing new: no repeated nft calls
        self.assertEqual(len(calls), 1)
        devreg.data["192.168.8.7"]["paused_until"] = None
        Engine._sync_pauses(eng, 1002.0)
        self.assertEqual(calls[-1], ("resume", "2401:4900:1c2a:5f00:51a:9f3:c2d4:8e01"))


class DeviceListIsPerDevice(unittest.TestCase):
    def test_one_row_per_dual_stack_device(self):
        from intentqos.capture import Flow
        from intentqos.daemon import Engine
        d = DeviceGrouping().keys()
        flows = [Flow(("192.168.8.5", "142.250.183.14", 6, 50000, 443), 4, 0.0),
                 Flow(("2401:4900:1c2a:5f00:9d1e:41c2:7a0b:11e3", "2404:6800:4007:81a::200e", 17, 51000, 3478), 6, 0.0),
                 Flow(("2401:4900:1c2a:5f00:3e22:fbff:fe10:5", "2606:4700::6810:84e5", 6, 50001, 443), 6, 0.0)]
        for f in flows:
            f.dev = d.key(f.lan_ip)
        eng = types.SimpleNamespace(flows=flows, active_intents=[], devices={"192.168.8.5": "Work laptop"},
                                    decisions={}, usage=types.SimpleNamespace(days={}), idents={},
                                    devreg=types.SimpleNamespace(data={}, get=lambda ip: {}))
        out = Engine._devices(eng)
        rows = [r for r in out if r["flows"]]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["name"], "Work laptop")
        self.assertTrue(rows[0]["ipv6"])
        self.assertEqual(len(rows[0]["addresses"]), 3)
        self.assertEqual(rows[0]["addresses"][0], "192.168.8.5")


if __name__ == "__main__":
    unittest.main()
