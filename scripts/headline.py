#!/usr/bin/env python3
"""Print the README "Results at a glance" table from results/summary.json."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICK = [
    ("s1_iso_during_call", "audio_up.owd_p99", "S1 ISO download + backup during a call: call delay p99, upload"),
    ("s1_iso_during_call", "audio_down.owd_p99", "S1: call delay p99, download"),
    ("s1_iso_during_call", "browse.page_load_ms_p95", "S1: web page load p95"),
    ("s1_iso_during_call", "iso_download.goodput_mbps", "S1: ISO download goodput"),
    ("s2_wan_drop", "audio_down_settled.owd_p99", "S2 link drops 100 to 20 Mbit/s: call delay p99 once settled"),
    ("s2_wan_drop", "audio_down_post.mos_mean", "S2: call quality (MOS) after the drop"),
    ("s2_wan_drop", "recovery_s", "S2: time until call delay is back within 30 ms"),
    ("s2_wan_drop", "tv_stream.rebuffer_s", "S2: TV rebuffering"),
    ("s3_tvs_and_gaming", "game_down.owd_p99", "S3 three TVs + game + update: game delay p99, download"),
    ("s3_tvs_and_gaming", "tv_mean_bitrate_mbps", "S3: mean TV bitrate"),
    ("s3_tvs_and_gaming", "game_update.starved_seconds", "S3: update seconds with no progress"),
]
NAMES = {"noqos": "No QoS", "static": "Static QoS", "adaptive_rules": "IntentQoS rules", "adaptive_ml": "IntentQoS ML",
         "adaptive_ml_intent": "IntentQoS ML + lease"}


def fmt(v, unit):
    if v is None:
        return "–"
    if unit in ("MOS", "index"):
        return f"{v:.2f}"
    if unit == "fraction":
        return f"{v * 100:.1f}%"
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"


def main():
    s = json.load(open(os.path.join(ROOT, "results", "summary.json")))
    arms = ["noqos", "static", "adaptive_rules", "adaptive_ml"]
    out = ["| Metric (median of seeds) | " + " | ".join(NAMES[a] for a in arms) + " |", "|---|" + "---|" * len(arms)]
    for sc, path, label in PICK:
        m = next((x for x in s.get(sc, {}).get("metrics", []) if x["path"] == path), None)
        if not m:
            continue
        unit = m["unit"]
        cells = []
        for a in arms:
            x = m["arms"].get(a)
            v = fmt(x["median"], unit) if x else "–"
            cells.append(f"**{v}**" if a == "adaptive_ml" else v)
        u = "" if unit in ("MOS", "index", "fraction") else f" ({unit})"
        out.append(f"| {label}{u} | " + " | ".join(cells) + " |")
    s4 = s.get("s4_intent")
    if s4:
        out.append("")
        out.append("| S4 two calls on 10/3 Mbit/s | without lease | with lease on the work call |")
        out.append("|---|---|---|")
        for path in ("laptop_video_up.owd_p99", "laptop_video_up.loss", "phone_audio_up.mos_mean", "backup.min_1s_mbps"):
            m = next((x for x in s4["metrics"] if x["path"] == path), None)
            if not m:
                continue
            a, b = m["arms"].get("adaptive_ml"), m["arms"].get("adaptive_ml_intent")
            u = "" if m["unit"] in ("MOS", "index", "fraction") else f" ({m['unit']})"
            out.append(f"| {m['label']}{u} | {fmt(a['median'], m['unit']) if a else '–'} | "
                       f"**{fmt(b['median'], m['unit']) if b else '–'}** |")
    s5 = s.get("s5_faults")
    if s5:
        m = next((x for x in s5["metrics"] if x["path"] == "watchdog_reaction_s"), None)
        if m and m["arms"].get("adaptive_ml"):
            x = m["arms"]["adaptive_ml"]
            out.append("")
            out.append(f"S5 fault injection: after `kill -9` of the engine the watchdog installed the safe state in "
                       f"**{x['median']:.1f} s** (median of {x['n']} runs; heartbeat timeout 2 s).")
    print("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())
