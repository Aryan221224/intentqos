#!/usr/bin/env python3
"""Evening capacity variation for the field-calibrated profile.

Every hold period (exponential, mean from the profile) the available downstream capacity is set to
plan x factor, the factor drawn from the profile's levels (e.g. 85 % of the time 0.9-1.0, 12 % 0.6-0.9,
3 % 0.3-0.6). Uses `tc qdisc change` so the bottleneck queue is kept. Re-seeded whenever the run seed
changes, so every arm of an experiment sees the same capacity trace. EMULATION.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import time

from . import profile as PR
from . import testbed as TB


def main() -> int:
    seed, rnd, next_t, factor = None, None, 0.0, 1.0
    applied = None
    while True:
        try:
            cfg = json.load(open(PR.CAPACITY_CONF))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.2)
            continue
        if cfg["seed"] != seed:
            seed, rnd = cfg["seed"], random.Random(cfg["seed"] * 7907 + 3)
            factor, next_t = 1.0, time.time() + rnd.expovariate(1.0 / cfg["hold_mean_s"])
            applied = None
        now = time.time()
        if cfg.get("enabled", True) and now >= next_t:
            u, acc = rnd.random(), 0.0
            for share, lo, hi in cfg["levels"]:
                acc += share
                if u <= acc:
                    factor = rnd.uniform(lo, hi)
                    break
            next_t = now + rnd.expovariate(1.0 / cfg["hold_mean_s"])
        st = TB._read_state()
        if st.get("down_mbit") and st.get("limit_down"):
            want = (round(st["down_mbit"] * st.get("rate_factor", 1.0) * (factor if cfg.get("enabled", True) else 1.0), 2),
                    st["limit_down"])
            if want != applied:
                rate, lim = want
                burst = max(16_000, int(rate * 1e6 / 8 / 250))
                subprocess.run(["ip", "netns", "exec", TB.NS_ISP, "tc", "qdisc", "change", "dev", TB.ISP_GW, "root",
                                "tbf", "rate", f"{rate}mbit", "burst", str(burst), "limit", str(lim)],
                               capture_output=True)
                with open("/run/iq-capacity.log", "a") as f:
                    f.write(json.dumps({"t": time.time(), "seed": seed, "factor": round(factor, 3), "rate": rate}) + "\n")
                applied = want
        time.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(main())
