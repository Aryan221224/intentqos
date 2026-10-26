"""Dead-man watchdog: an independent process that returns the gateway to a known-safe state.

It does not import the policy engine's runtime state -- only the config and the
backend code needed to write the safe baseline. If the engine's heartbeat is
older than `timeout_s` (engine crashed, hung, was killed, or the box is
overloaded), the watchdog:
  1. removes the nftables flow-marking table (all per-flow priorities and
     intent boosts vanish at once -- leases cannot outlive the engine),
  2. installs the safe plan (shape at safe_fraction of nominal, fair classes,
     floors) or removes shaping entirely if exit_state = "passthrough",
  3. logs the action to the shared events file, and re-arms when the
     heartbeat becomes fresh again.

    python3 -m intentqos.watchdog --config config/testbed.toml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import config as C
from .enforce import FlowMarker, HtbBackend, CakeBackend, safe_plan, run


def apply_safe(cfg: dict, backend_name: str, lan: str, wan: str) -> str:
    state = cfg["watchdog"]["exit_state"]
    run(["nft", "delete", "table", "inet", cfg["enforce"]["nft_table"]], check=False)
    if state == "passthrough":
        for dev in (lan, wan):
            run(["tc", "qdisc", "del", "dev", dev, "root"], check=False)
        return "passthrough"
    be = CakeBackend(lan, wan, cfg) if backend_name == "cake" else HtbBackend(lan, wan, cfg)
    be.setup(safe_plan(cfg))
    return f"safe ({backend_name} at {cfg['policy']['safe_fraction'] * 100:.0f}% of nominal)"


def _status(path: str, d: dict) -> None:
    """The watchdog's own heartbeat, so the console can show that the safety net is armed."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args(argv)
    cfg = C.load(a.config)
    for kv in a.set:
        k, v = kv.split("=", 1)
        C.set_dotted(cfg, k, C.parse_value(v))
    hb_path = cfg["watchdog"]["heartbeat_path"]
    timeout = float(cfg["watchdog"]["timeout_s"])
    events = os.path.join(C.resolve(cfg, cfg["data"]["dir"]), "events.jsonl")
    tripped = False
    status_path = cfg["watchdog"].get("status_path") or os.path.join(os.path.dirname(hb_path), "watchdog.json")
    trips, last_trip = 0, None
    print(f"watchdog: heartbeat {hb_path}, timeout {timeout}s", flush=True)
    while True:
        try:
            with open(hb_path) as f:
                hb = json.load(f)
        except (FileNotFoundError, ValueError):
            hb = None
        now = time.time()
        stale = hb is not None and not hb.get("stopped") and now - hb.get("ts", 0) > timeout
        if stale and not tripped:
            t0 = time.monotonic()
            what = apply_safe(cfg, hb.get("backend", "htb"), hb.get("lan", cfg["interfaces"]["lan"]),
                              hb.get("wan", cfg["interfaces"]["wan"]))
            ms = (time.monotonic() - t0) * 1000
            msg = (f"engine heartbeat stale ({now - hb['ts']:.1f}s > {timeout}s): restored {what} "
                   f"in {ms:.0f} ms")
            print(msg, flush=True)
            try:
                with open(events, "a") as f:
                    f.write(json.dumps({"seq": 0, "t": now, "kind": "watchdog", "level": "warn", "msg": msg,
                                        "stale_s": round(now - hb["ts"], 3), "restore_ms": round(ms, 1)}) + "\n")
            except OSError:
                pass
            tripped = True
            trips += 1
            last_trip = {"t": now, "msg": msg, "restore_ms": round(ms, 1)}
        elif hb is not None and not stale and tripped and not hb.get("stopped"):
            print("watchdog: engine heartbeat is fresh again; re-armed", flush=True)
            tripped = False
        _status(status_path, {"ts": time.time(), "pid": os.getpid(), "tripped": tripped, "trips": trips,
                              "last_trip": last_trip, "timeout_s": timeout,
                              "engine_age_s": round(now - hb.get("ts", now), 2) if hb else None})
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
