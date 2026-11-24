"""Fibre -> 4G failover behaviour of the estimator, found by the full-system emulation (systemsim/).

On the real OpenWrt image the cable moved to the 4G Bridge laptop while the Test Kit loaded the line: the probes
went silent for ~40 s (the 4G modem's 300 ms buffer full at the fibre rate) and blind mode held the fibre rate.
"""
import unittest

from intentqos.config import load as load_config
from intentqos.estimator import CapacityEstimator


def est():
    c = load_config(None)
    c["link"]["down_mbit"], c["link"]["up_mbit"] = 30, 30
    ev = []
    e = CapacityEstimator(c, lambda k, m, lvl="info": ev.append((k, m, lvl)), prober=None)
    e.enabled = True
    return e, ev


class BlindUnderLoad(unittest.TestCase):
    def test_loaded_and_blind_backs_off_to_floor(self):
        e, ev = est()
        base = e.down.base
        e.last_sample_t = 0.0
        rates = []
        for i in range(40):                      # 8 s of ticks with no probe replies, line fully used
            now = 10.0 + i * 0.2
            rd, ru = e.tick(now, e.down.rate * 0.95, e.up.rate * 0.95)
            rates.append(rd)
        self.assertTrue(e.blind)
        self.assertLess(rates[-1], 0.5 * base)
        self.assertGreaterEqual(rates[-1], 0.3 * base - 1)
        self.assertTrue(any("silent under load" in m for _k, m, _l in ev))

    def test_idle_and_blind_holds(self):
        e, _ = est()
        r0 = e.down.rate
        e.last_sample_t = 0.0
        for i in range(40):
            e.tick(10.0 + i * 0.2, 0.0, 0.0)
        self.assertEqual(e.down.state, "blind")
        self.assertAlmostEqual(e.down.rate, min(r0, e.down.base))


class PathChange(unittest.TestCase):
    def test_gateway_change_resets_baselines_and_rates(self):
        e, ev = est()
        e.baseline[("1.1.1.1", "down")] = 5.0
        e.path_changed(100.0, "gateway 192.168.29.1 -> 10.42.0.1")
        self.assertEqual(e.baseline, {})
        self.assertAlmostEqual(e.down.rate, max(e.down.min, 0.3 * e.down.base))
        self.assertEqual(e.up.state, "path-change")
        self.assertTrue(any("WAN path changed" in m for _k, m, _l in ev))

    def test_daemon_watch_calls_estimator_once_per_change(self):
        from intentqos.daemon import Engine
        calls = []

        class Fake:
            wan = "eth1"
            estimator = type("E", (), {"path_changed": lambda self, now, why: calls.append(why)})()
        f = Fake()
        gws = iter(["192.168.29.1", "192.168.29.1", "10.42.0.1", "10.42.0.1", "192.168.29.1"])
        f._wan_gateway = lambda: next(gws)
        f._watch_watchdog = lambda now: None
        for i in range(5):
            Engine._watch_wan(f, 10.0 + 2 * i)
        self.assertEqual(calls, ["gateway 192.168.29.1 -> 10.42.0.1", "gateway 10.42.0.1 -> 192.168.29.1"])


if __name__ == "__main__":
    unittest.main()


class WatchdogTrip(unittest.TestCase):
    def test_safe_plan_never_raises_the_last_applied_rate(self):
        from intentqos.enforce import safe_plan
        c = load_config(None)
        c["link"]["down_mbit"], c["link"]["up_mbit"] = 30, 30
        full = safe_plan(c)
        capped = safe_plan(c, {"down": 8100, "up": 6600})
        self.assertAlmostEqual(full["down"]["rate"], 30000 * c["policy"]["safe_fraction"])
        self.assertEqual((capped["down"]["rate"], capped["up"]["rate"]), (8100.0, 6600.0))
        self.assertEqual(safe_plan(c, {"down": 90000})["down"]["rate"], full["down"]["rate"])

    def test_engine_reapplies_after_a_trip(self):
        import json
        import os
        import tempfile
        from intentqos.daemon import Engine
        d = tempfile.mkdtemp()
        wd = os.path.join(d, "watchdog.json")
        calls, events = [], []

        class Marker:
            current = {("a",): 1}

            def sync(self, desired):
                calls.append(("sync", dict(desired)))
                return len(desired)

        class Enf:
            applied = {"down": {"rate": 8100}, "up": {"rate": 6600}}

            def setup(self, plan):
                calls.append(("setup", plan["down"]["rate"]))

        class Fake:
            wd_path = wd
            bypass_until = None
            enforcer = Enf()
            marker = Marker()
            flow_tin = {("f1",): 2, ("f2",): 3}

            def emit(self, *a):
                events.append(a)
        f = Fake()
        json.dump({"trips": 0}, open(wd, "w"))
        Engine._watch_watchdog(f, 1.0)            # first look: remember, do nothing
        self.assertEqual(calls, [])
        json.dump({"trips": 1}, open(wd, "w"))
        Engine._watch_watchdog(f, 2.0)
        self.assertEqual(calls[0], ("setup", 8100))
        self.assertEqual(calls[1][0], "sync")
        self.assertTrue(any("watchdog had stepped in" in str(e) for e in events))


class IdentityAcrossReboot(unittest.TestCase):
    def test_records_survive_a_restart(self):
        import json
        import os
        import tempfile
        from intentqos.identity import Identity
        d = tempfile.mkdtemp()
        hook, cache = os.path.join(d, "dhcp.jsonl"), os.path.join(d, "identity.json")
        with open(hook, "w") as f:
            f.write(json.dumps({"ip": "192.168.8.172", "mac": "00:d9:d1:10:00:08", "hostname": "PS5-7781"}) + "\n")
        cfg = {"identity": {"dhcp_hook_path": hook, "leases_path": os.path.join(d, "none")}}
        a = Identity(cfg, cache)
        self.assertEqual(a.refresh()["192.168.8.172"]["type"], "console")
        self.assertTrue(os.path.exists(cache))
        os.remove(hook)                                   # the router restarted: /tmp is empty
        b = Identity(cfg, cache).refresh()
        self.assertEqual(b["192.168.8.172"]["type"], "console")
        self.assertIn("remembered", b["192.168.8.172"]["why"])
