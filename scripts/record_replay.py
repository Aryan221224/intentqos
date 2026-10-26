#!/usr/bin/env python3
"""Record a live testbed session for the console's replay mode.

Needs the lab server (sudo make lab). Plays a scripted household evening -- a video call, an ISO
download, a cloud backup, three TVs, a game, a link drop to 20/5 Mbit/s, a work-call lease, the
link coming back -- and captures one engine snapshot per second, plus the history, system info,
setup check, model report, kernel state and experiment results.

    python3 scripts/record_replay.py --out results/replay.json [--seconds 150]
    python3 scripts/build_replay.py results/replay.json docs/console-replay.html
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8090"


def call(method: str, path: str, body: dict | None = None, timeout: float = 8.0):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


NAMES = {"192.168.50.10": ("Asha's laptop", "laptop"), "192.168.50.11": ("Study desktop", "desktop"),
         "192.168.50.12": ("Living-room TV", "tv"), "192.168.50.13": ("Bedroom TV", "tv"),
         "192.168.50.14": ("Kids' TV", "tv"), "192.168.50.15": ("Game console", "console"),
         "192.168.50.16": ("Ravi's phone", "phone"), "192.168.50.17": ("Home NAS", "nas")}

STORY = [  # (second, description, action)
    (2, "video call starts", lambda: call("POST", "/lab/traffic", {"preset": "call"})),
    (3, "browsing", lambda: call("POST", "/lab/traffic", {"preset": "browse"})),
    (12, "ISO download", lambda: call("POST", "/lab/traffic", {"preset": "iso"})),
    (20, "cloud backup", lambda: call("POST", "/lab/traffic", {"preset": "backup"})),
    (32, "three TVs", lambda: call("POST", "/lab/traffic", {"preset": "tvs"})),
    (44, "online game", lambda: call("POST", "/lab/traffic", {"preset": "game"})),
    (60, "link drops to 20/5", lambda: call("POST", "/lab/condition", {"down": 20, "up": 5})),
    (84, "work-call lease", lambda: call("POST", "/api/intents", {"target": "192.168.50.10", "goal": "low_latency",
                                                                  "duration_s": 3600, "priority": 3, "requester": "asha",
                                                                  "label": "Board meeting"})),
    (104, "link restored", lambda: call("POST", "/lab/condition", {"down": 100, "up": 20})),
    (122, "ISO download finishes", lambda: call("POST", "/lab/traffic", {"preset": "iso", "action": "stop"})),
]


def slim(st: dict) -> dict:
    st = dict(st)
    st["flows"] = st.get("flows", [])[:40]
    st["events"] = st.get("events", [])[-50:]
    st["apply_log"] = st.get("apply_log", [])[:40]
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/replay.json")
    ap.add_argument("--seconds", type=int, default=150)
    a = ap.parse_args()
    print("resetting the lab", flush=True)
    call("POST", "/lab/reset")
    for it in call("GET", "/api/state").get("intents", []):
        if it.get("status") in ("active", "scheduled"):
            call("DELETE", f"/api/intents/{it['id']}")
    call("POST", "/lab/engine", {"action": "restart", "classifier": "ml"})
    t = time.time() + 20
    while time.time() < t and not call("GET", "/api/state").get("t"):
        time.sleep(0.5)
    for ip, (n, ty) in NAMES.items():
        call("PUT", f"/api/devices/{ip}", {"name": n, "type": ty})
    time.sleep(4)
    frames, labs, marks = [], [], []
    t0 = time.time()
    story = list(STORY)
    for i in range(a.seconds):
        while story and story[0][0] <= i:
            sec, what, fn = story.pop(0)
            fn()
            marks.append({"i": i, "what": what})
            print(f"[{i:3d}s] {what}", flush=True)
        frames.append(slim(call("GET", "/api/state")))
        labs.append(call("GET", "/lab/status"))
        nxt = t0 + i + 1
        time.sleep(max(0.0, nxt - time.time()))
    first = frames[0]["t"] - 1
    hist = [r for r in call("GET", f"/api/history?since={first - 300}") if r["t"] <= frames[-1]["t"]]
    rec = {"recorded": time.strftime("%Y-%m-%d %H:%M"), "frames": frames, "lab_at": labs, "history": hist, "marks": marks,
           "system": call("GET", "/api/system"), "setup": call("GET", "/api/setup-check"),
           "model": call("GET", "/api/model"), "inspect": call("GET", "/api/inspect"),
           "modes": call("GET", "/api/modes"), "experiments": call("GET", "/lab/experiments")}
    rec["experiments"]["jobs"] = []
    with open(a.out, "w") as f:
        json.dump(rec, f, separators=(",", ":"))
    call("POST", "/lab/reset")
    print(f"wrote {a.out}: {len(frames)} frames, {len(hist)} history rows", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
