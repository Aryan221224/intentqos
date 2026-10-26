#!/usr/bin/env python3
"""Build the self-contained replay console: intentqos/web/console.html + a recording (record_replay.py).

The output needs no engine: it plays the recorded snapshots, clearly labelled as a replay. It is what
we publish for anyone without a Linux machine.

    python3 scripts/build_replay.py results/replay.json docs/console-replay.html [--fragment]

--fragment drops the <!doctype>/<html>/<head>/<body> wrapper (for hosts that add their own).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("out")
    ap.add_argument("--fragment", action="store_true")
    a = ap.parse_args()
    with open(os.path.join(ROOT, "intentqos", "web", "console.html")) as f:
        html = f.read()
    with open(a.recording) as f:
        rec = json.load(f)
    rec["force"] = True
    rec["description"] = (f"Recorded {rec.get('recorded', '')} on the namespace testbed: a video call, an ISO download, "
                          "a backup, three TVs and a game; the link drops to 20/5 Mbit/s, a work call is prioritised, "
                          "and the link recovers.")
    figs = {}
    for f in (rec.get("experiments") or {}).get("figures", []):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            with open(p, "rb") as fh:
                figs[f] = "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    rec["figures"] = figs
    # Inline the console's fonts so the file looks the same opened from disk, with no server behind it.
    def font(m: re.Match) -> str:
        p = os.path.join(ROOT, "intentqos", "web", "fonts", m.group(1))
        if not os.path.exists(p):
            return m.group(0)
        with open(p, "rb") as fh:
            return "url(data:font/woff;base64," + base64.b64encode(fh.read()).decode() + ")"
    html = re.sub(r"url\(/fonts/([\w.-]+\.woff)\)", font, html)
    data = json.dumps(rec, separators=(",", ":")).replace("</", "<\\/")
    inject = f"<script>window.IQ_REPLAY={data};</script>\n<script>\n\"use strict\";"
    html = html.replace("<script>\n\"use strict\";", inject, 1)
    if a.fragment:
        html = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>\s*", "", html, flags=re.I)
        html = re.sub(r"<meta charset[^>]*>\s*<meta name=\"viewport\"[^>]*>\s*", "", html)
        html = html.replace("</head>\n<body>\n", "").replace("</body>\n</html>\n", "")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write(html)
    print(f"wrote {a.out} ({os.path.getsize(a.out) / 1e6:.1f} MB, {len(rec['frames'])} frames, {len(figs)} figures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
