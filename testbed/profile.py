"""Field-calibrated emulation profiles (testbed/profiles/*.json).

Select one with the environment variable IQ_PROFILE (e.g. IQ_PROFILE=india_ftth_evening). Without it the
testbed behaves exactly as before (the lab profile used for results/). Every value in a profile carries its
reasoning and sources; the profile is still EMULATION and is labelled as such in every result.
"""
from __future__ import annotations

import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
ACCESS_CONF = "/run/iq-access.conf"
CAPACITY_CONF = "/run/iq-capacity.json"
DEVICE_HOST = {"laptop": 10, "desktop": 11, "tv1": 12, "tv2": 13, "tv3": 14, "console": 15, "phone": 16, "nas": 17}

_cache: dict | None = None


def active() -> dict | None:
    global _cache
    name = os.environ.get("IQ_PROFILE", "").strip()
    if not name:
        return None
    if _cache is None or _cache.get("name") != name:
        path = name if name.endswith(".json") else os.path.join(HERE, "profiles", f"{name}.json")
        with open(path) as f:
            _cache = json.load(f)
    return _cache


def _lognormal_from(median: float, p90: float, cap: float, rnd: random.Random) -> float:
    sigma = math.log(p90 / median) / 1.2816
    return min(cap, median * math.exp(sigma * rnd.gauss(0, 1)))


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_access(seed: int, failover: bool = False, epoch: int = 0) -> dict:
    """Write the access emulator's configuration for one run (same seed => same impairments for every arm)."""
    P = active()
    rnd = random.Random(seed * 104729 + 17)
    nb = {b: _lognormal_from(v["median"], v["p90"], v["cap"], rnd) for b, v in P["wifi"]["neighbour_busy"].items()}
    lines = [f"seed={seed}", f"epoch={epoch}"]   # a new seed or epoch restarts every random process
    for d in ("down", "up"):
        for k, v in P["wan"][d].items():
            lines.append(f"{d}.{k}={v}")
        if failover:
            fo = P["failover_4g"]
            lines += [f"{d}.extra_ms={fo['extra_ms']}", f"{d}.jitter_ms={fo['jitter_ms']}", f"{d}.loss={fo['loss']}",
                      f"{d}.ge_p=0"]
    for k, v in P["wifi"]["params"].items():
        lines.append(f"{k}={v}")
    for dev, band in P["wifi"]["stations"].items():
        lines.append(f"sta.{DEVICE_HOST[dev]}={band},{nb[str(band)]:.3f},{1 if dev in P['wifi'].get('psm', []) else 0}")
    _atomic_write(ACCESS_CONF, "\n".join(lines) + "\n")
    return {"seed": seed, "epoch": epoch, "failover": failover, "neighbour_busy": {k: round(v, 3) for k, v in nb.items()}}


def write_capacity(seed: int, enabled: bool = True) -> None:
    P = active()
    ec = P["evening_capacity"]
    _atomic_write(CAPACITY_CONF, json.dumps({"seed": seed, "enabled": enabled, "levels": ec["levels"],
                                             "hold_mean_s": ec["hold_mean_s"]}))
