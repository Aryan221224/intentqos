"""Unit tests for the console services (grade, device registry, modes, schedules). No root, no network.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intentqos import config as C  # noqa: E402
from intentqos.console import (MODES, DeviceRegistry, GradeTracker, ProcStats, Scheduler, activity,  # noqa: E402
                               grade)
from intentqos.intents import IntentManager  # noqa: E402


class TestGrade(unittest.TestCase):
    def test_thresholds_match_waveform(self):
        self.assertEqual([grade(x) for x in (0, 4.9, 5, 29, 30, 59, 60, 199, 200, 399, 400, 2000)],
                         ["A+", "A+", "A", "A", "B", "B", "C", "C", "D", "D", "F", "F"])

    def test_p95_ignores_one_spike_and_window_expires(self):
        g = GradeTracker(window_s=60)
        for i in range(100):
            g.add(i * 0.5, 2.0, 1.0)
        g.add(50.0, 300.0, 1.0)               # a single spike out of 101 samples
        self.assertEqual(g.summary()["down"]["grade"], "A+")
        for i in range(60):                    # a sustained problem is not hidden
            g.add(51 + i, 80.0, 1.0)
        s = g.summary()
        self.assertEqual(s["down"]["grade"], "C")
        self.assertEqual(s["overall"]["grade"], "C")   # overall = worse direction
        for i in range(130):                   # and it ages out after the window
            g.add(112 + i * 0.5, 1.0, 1.0)
        self.assertEqual(g.summary()["overall"]["grade"], "A+")


class TestDevices(unittest.TestCase):
    def test_registry_merges_config_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "devices.json")
            r = DeviceRegistry(p, {"laptop": "10.0.0.2", "tv1": "10.0.0.3"})
            self.assertEqual(r.get("10.0.0.3")["type"], "tv")
            r.set("10.0.0.2", "Asha's laptop", "laptop")
            with self.assertRaises(ValueError):
                r.set("10.0.0.2", None, "toaster")
            r2 = DeviceRegistry(p, {"laptop": "10.0.0.2"})
            self.assertEqual(r2.names()["10.0.0.2"], "Asha's laptop")
            self.assertEqual(r2.get("10.9.9.9"), {"name": "", "type": "other"})

    def test_activity_prefers_the_most_sensitive_use(self):
        self.assertEqual(activity({"bulk": 3, "video": 1}), "On a video call")
        self.assertEqual(activity({"web": 2}), "Browsing")
        self.assertEqual(activity({}), "Idle")


class TestModesAndSchedules(unittest.TestCase):
    def _im(self, d):
        cfg = C.load(None)
        cfg["intents"]["store_path"] = os.path.join(d, "intents.json")
        return IntentManager(cfg, lambda p: p, lambda *a, **k: None)

    def test_modes_are_valid_lease_requests(self):
        with tempfile.TemporaryDirectory() as d:
            im = self._im(d)
            for m in MODES:
                r = im.request({"target": "10.0.0.2", "goal": m["goal"], "priority": m["priority"],
                                "duration_s": m["duration_s"], "demand_down_kbit": m["demand_down_kbit"],
                                "demand_up_kbit": m["demand_up_kbit"]}, 100000, 20000, dry_run=True)
                self.assertTrue(r["ok"], (m["id"], r))

    def test_schedule_starts_one_lease_per_day_inside_its_window(self):
        with tempfile.TemporaryDirectory() as d:
            im = self._im(d)
            ev = []
            sc = Scheduler(os.path.join(d, "schedules.json"), im, lambda *a: ev.append(a))
            lt = time.localtime()
            now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 10, 0, 0, 0, 0, -1))
            wd = time.localtime(now).tm_wday
            sc.add({"target": "10.0.0.2", "days": [wd], "start": "09:00", "end": "11:00", "label": "work"})
            with self.assertRaises(ValueError):
                sc.add({"target": "10.0.0.2", "days": [wd], "start": "12:00", "end": "11:00"})
            sc.tick(now, 100000, 20000)
            sc.tick(now + 60, 100000, 20000)          # no second lease the same day
            act = [i for i in im.intents.values() if i.status == "active"]
            self.assertEqual(len(act), 1)
            self.assertAlmostEqual(act[0].expires - now, 3600, delta=1)   # ends with the window
            sc2 = Scheduler(os.path.join(d, "schedules.json"), im, lambda *a: None)
            self.assertEqual(len(sc2.items), 1)
            self.assertTrue(sc2.remove(sc2.items[0]["id"]))
            with open(os.path.join(d, "schedules.json")) as f:
                self.assertEqual(json.load(f)["items"], [])

    def test_schedule_outside_window_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            im = self._im(d)
            sc = Scheduler(os.path.join(d, "schedules.json"), im, lambda *a: None)
            lt = time.localtime()
            now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 20, 0, 0, 0, 0, -1))
            sc.add({"target": "10.0.0.2", "days": list(range(7)), "start": "09:00", "end": "11:00"})
            sc.tick(now, 100000, 20000)
            self.assertEqual(len(im.intents), 0)


class TestProc(unittest.TestCase):
    def test_sample_shape(self):
        s = ProcStats().sample()
        self.assertGreater(s["rss_mb"], 1)
        self.assertGreaterEqual(s["threads"], 1)


if __name__ == "__main__":
    unittest.main()
