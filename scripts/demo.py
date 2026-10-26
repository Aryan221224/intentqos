#!/usr/bin/env python3
"""Scripted live demo (golden path) for presenting IntentQoS. Run with the engine already up:

    sudo make testbed && sudo make engine        # terminal 1, dashboard at http://10.255.0.2:8080/
    sudo python3 scripts/demo.py                 # terminal 2

Timeline (about 80 s), narrated on the console so the presenter can talk over it:
   0 s  a work video call starts on the laptop
   8 s  a 6-connection ISO download and a cloud backup start          -> call delay stays flat
  25 s  the ISP link degrades from 100 to 20 Mbit/s                    -> estimator cuts the shaper
  40 s  "prioritise my work call" lease for the laptop                 -> lease shown with assurance
  60 s  the link recovers to 100 Mbit/s                                -> shaper probes back up
At the end it prints the call's measured one-way delay per phase.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from testbed import analysis as A  # noqa: E402
from testbed import testbed as TB  # noqa: E402
from testbed.runner import Run  # noqa: E402


def say(t0, msg):
    print(f"[{time.time() - t0:5.1f}s] {msg}", flush=True)


def main() -> int:
    out = "/tmp/intentqos-demo"
    os.system(f"rm -rf {out}")
    TB.set_isp_rate(100, 20)
    run = Run(out)
    if not run.wait_api(3):
        print("The engine is not running. Start it first: sudo make engine", file=sys.stderr)
        return 2
    run.server()
    run.start_clock()
    t0 = run.t0
    D = 80.0
    run.flow(0, "voip", "call_audio", "laptop", D - 1)
    run.flow(0.2, "video", "call_video", "laptop", D - 1.2)
    run.flow(0.5, "abr", "tv_stream", "tv1", D - 2, ladder="1.5,3,6,10,16")
    run.flow(1.0, "gaming", "game", "console", D - 2)
    say(t0, "Work video call, a TV stream and a game are running. Watch the lanes and the delay chart.")
    time.sleep(8)
    run.flow(8, "bulk", "iso", "desktop", D - 10, conns=6, direction="down")
    run.flow(8.3, "bulk", "backup", "nas", D - 10.3, conns=2, direction="up")
    say(t0, "A 6-connection ISO download and a cloud backup start. Bulk moves to the bottom lane, call delay stays flat.")
    time.sleep(17)
    TB.set_isp_rate(20, 5)
    say(t0, "The ISP link drops to 20/5 Mbit/s. The estimator sees delay rise and cuts the shaper below it.")
    time.sleep(15)
    r = run.api("POST", "/api/intents", {"target": "laptop", "duration_s": 1800, "goal": "low_latency", "priority": 3,
                                         "requester": "presenter", "label": "work call"})
    if r.get("ok"):
        say(t0, f"Lease granted: {r['intent']['granted_down_kbit'] / 1000:.1f}/{r['intent']['granted_up_kbit'] / 1000:.1f} "
                "Mbit/s reserved for the laptop for 30 min. See 'Prioritise something'.")
    else:
        say(t0, f"Lease refused: {r.get('reason')}")
    time.sleep(20)
    TB.set_isp_rate(100, 20)
    say(t0, "The link recovers to 100 Mbit/s. The shaper probes back up in small steps.")
    time.sleep(D - 60 + 3)
    run.stop()
    a = A.realtime_records(out, "call_audio")
    if a:
        print("\nCall audio one-way delay, download (ms):")
        for name, (x, y) in {"call alone": (2, 8), "download running": (10, 25), "link at 20 Mbit/s": (27, 40),
                             "with the lease": (42, 60), "link recovered": (62, 78)}.items():
            m = A.realtime_metrics(a["down"], t0 + x, t0 + y)
            if m:
                print(f"  {name:20s} p50 {m['owd_p50']:6.1f}   p99 {m['owd_p99']:6.1f}   loss {m['loss'] * 100:4.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
