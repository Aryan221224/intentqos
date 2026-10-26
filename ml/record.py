#!/usr/bin/env python3
"""Record header-only flow-feature snapshots on the gateway (for training/evaluation).

Runs the SAME capture + feature code as the engine, without touching shaping.
Every 0.5 s it writes one JSON line per active flow:
  {"t", "session", "key": [lan, remote, proto, lan_port, remote_port], "age", "features": [...], "rule": label}
Ground-truth labels are joined later from the traffic generators' own records
(consented, synthetic traffic -- the labels never come from payload).

    ip netns exec iq-gw python3 ml/record.py --iface gw-lan --lan 192.168.50.0/24 --out snaps.jsonl --session s01
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intentqos import config as C  # noqa: E402
from intentqos.capture import WINDOW_S, Capture, FlowTable  # noqa: E402
from intentqos.classifier import RuleClassifier  # noqa: E402
from intentqos.features import flow_features  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface", required=True)
    ap.add_argument("--lan", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--session", required=True)
    a = ap.parse_args(argv)
    cfg = C.load(None)
    table = FlowTable(a.lan)
    cap = Capture(a.iface, table)
    rules = RuleClassifier(cfg)
    cap.start()
    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    signal.signal(signal.SIGINT, lambda *_: running.__setitem__(0, False))
    nxt = time.monotonic() + WINDOW_S
    with open(a.out, "a") as out:
        while running[0]:
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            nxt += WINDOW_S
            now = time.monotonic()
            table.roll_windows(now)
            for f in table.active(now, max_idle=3.0):
                if not f.hist:
                    continue
                rl, _ = rules.classify(f, now)
                out.write(json.dumps({"t": round(time.time(), 3), "session": a.session, "key": list(f.key),
                                      "age": round(now - f.first, 2), "features": [round(x, 5) for x in
                                                                                  flow_features(f, now)],
                                      "rule": rl}) + "\n")
            out.flush()
    cap.stop()
    print(json.dumps(cap.stats()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
