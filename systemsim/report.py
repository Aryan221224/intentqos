#!/usr/bin/env python3
"""Build the full-system test report from the captures.

    report.py CAPTURE_DIR OUT_DIR      (CAPTURE_DIR holds run1/ run2/ run3/ screens/ calibration.json)

Writes OUT_DIR/summary.json, OUT_DIR/charts/*.png and copies the evidence (screens, timelines, router
internals, engine snapshots, emulator logs, Test Kit reports). REPORT.md is written by hand from summary.json.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import shutil
import statistics as st
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

CAP, OUT = sys.argv[1], sys.argv[2]
WORK = os.path.dirname(CAP.rstrip("/"))
os.makedirs(os.path.join(OUT, "charts"), exist_ok=True)
INK, OFF, ON, GRID = "#1f2a37", "#b4532a", "#1d6fa5", "#d9dee5"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": "#8a94a3", "axes.grid": True, "grid.color": GRID,
                     "axes.spines.top": False, "axes.spines.right": False})


def jl(path: str) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    for ln in open(path, errors="replace"):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


S: dict = {"calibration": json.load(open(os.path.join(CAP, "calibration.json")))}

# ------------------------------------------------------------------ timelines
tl = []
for run in ("run1", "run2", "run3"):
    for r in jl(os.path.join(CAP, run, "timeline.jsonl")):
        r["run"] = run
        tl.append(r)
for f, src in ((os.path.join(CAP, "screens", "router-setup-ui.jsonl"), "router_setup_ui"),
               (os.path.join(CAP, "screens", "testkit-ui.jsonl"), "testkit_ui"),
               (os.path.join(CAP, "screens", "bridge-ui.jsonl"), "bridge_ui"),
               (os.path.join(CAP, "run3", "resilience", "resilience.jsonl"), "resilience"),
               (os.path.join(WORK, "logs", "cable.jsonl"), "cable")):
    for r in jl(f):
        r["source"] = src
        r.pop("text", None) if src == "testkit_ui" and r.get("event") == "live" else None
        tl.append(r)
tl.sort(key=lambda r: r.get("t", 0))
S["timeline_events"] = len(tl)

# ------------------------------------------------------------------ calibration chart
cal = S["calibration"]["load_tests"]
fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
for i, (key, lab) in enumerate((("before_engine_fix", "before the engine fix"), ("after_engine_fix", "after"))):
    rows = cal[key]
    xs = [r["plan"] for r in rows]
    ax[0].plot(xs, [r["delivered_mbit"] for r in rows], "o-", color=[OFF, ON][i], label=lab)
    ax[1].plot(xs, [r["router_busy_pct"] for r in rows], "o-", color=[OFF, ON][i], label=lab)
ax[0].plot([30, 100], [30, 100], ":", color="#8a94a3", label="plan rate")
ax[0].set_xlabel("plan (Mbit/s)"); ax[0].set_ylabel("delivered (Mbit/s)"); ax[0].set_title("Throughput through the router VM")
ax[1].set_xlabel("plan (Mbit/s)"); ax[1].set_ylabel("router CPU busy (% of 2 cores)"); ax[1].set_ylim(0, 105)
ax[1].set_title("Router CPU at that load")
ax[0].legend(frameon=False, fontsize=8); ax[1].legend(frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "charts", "calibration.png"), dpi=130); plt.close(fig)

# ------------------------------------------------------------------ Test Kit results
tk = sorted(glob.glob(os.path.join(WORK, "home", "laptop", "Documents", "IntentQoS Reports", "*.json")))
S["testkit"] = []
S["failover"] = []
for f in tk:
    d = json.load(open(f))
    for r in d.get("results") or []:
        row = {"id": r["id"], "name": r["name"], "report": os.path.basename(f)}
        for arm in ("off", "on"):
            a = r.get(arm) or {}
            row[arm] = {k: a.get(k) for k in ("p50", "p95", "p99", "loss_pct", "down_mbps", "up_mbps")}
        if r.get("home"):
            row["home"] = {"capacity_mbps": r["home"].get("capacity", {}).get("max_ip_capacity_mbps"),
                           "rpm": r["home"].get("rpm", {}).get("rpm"),
                           "loaded_rpm": r["home"].get("rpm", {}).get("loaded_rpm")}
        S["testkit"].append(row)
    for fo in d.get("failover") or []:
        S["failover"].append({"qos": fo["qos"], "switch_t": fo["switch_t"], "report": os.path.basename(f),
                              "before": {k: fo["before"].get(k) for k in ("p50", "p95", "p99", "loss_pct", "down_mbps", "up_mbps")},
                              "after": {k: fo["after"].get(k) for k in ("p50", "p95", "p99", "loss_pct", "down_mbps", "up_mbps")},
                              "samples": fo.get("samples")})
rows = [r for r in S["testkit"] if r["off"].get("p99")]
if rows:
    fig, ax = plt.subplots(figsize=(9, 3.4))
    import numpy as np
    x = np.arange(len(rows))
    for j, (arm, col) in enumerate((("off", OFF), ("on", ON))):
        ax.bar(x + (j - 0.5) * 0.38, [r[arm]["p99"] for r in rows], 0.36, color=col, label=f"IntentQoS {arm}: p99")
        ax.scatter(x + (j - 0.5) * 0.38, [r[arm]["p50"] for r in rows], color="white", edgecolor=INK, zorder=3,
                   s=22, label=f"p50" if j == 0 else None)
    ax.set_xticks(x, [r["name"].replace(" while ", "\nwhile ").replace(" (", "\n(") for r in rows], fontsize=8)
    ax.set_ylabel("delay of call-like packets (ms)")
    ax.set_title("Test Kit on the work laptop (5 GHz): delay under load, IntentQoS off vs on")
    ax.legend(frameon=False, fontsize=8, ncol=3)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "charts", "testkit.png"), dpi=130); plt.close(fig)

FO_LABEL = {"1327": "flap (cable not moved)", "1329": "flap (cable not moved)", "1332": "on, engine as first installed",
            "1335": "off (no QoS)", "1342": "on + blind back-off + path-change reset",
            "1349": "on + watchdog keeps lower rates, engine re-applies", "1354": "on + watchdog timeout 8 s"}
for fo in S["failover"]:
    fo["label"] = FO_LABEL.get(fo["report"][-9:-5], fo["qos"])
valid = [fo for fo in S["failover"] if "flap" not in fo["label"]]
if valid:
    pick = [fo for fo in valid if fo["report"][-9:-5] in ("1335", "1332", "1349", "1354")] or valid
    fig, axs = plt.subplots(1, len(pick), figsize=(3.2 * len(pick), 3.2), sharey=True)
    axs = axs if len(pick) > 1 else [axs]
    for ax, fo in zip(axs, pick):
        sm = [x for x in (fo["samples"] or []) if x["rtt"] > 0]
        ax.plot([x["t"] - fo["switch_t"] for x in sm], [x["rtt"] for x in sm], ".", ms=2,
                color=ON if fo["qos"] == "on" else OFF)
        ax.axvline(0, color=INK, lw=1)
        ax.set_title(fo["label"], fontsize=8)
        ax.set_xlabel("s from cable move")
    axs[0].set_ylabel("delay of call-like packets (ms)")
    fig.suptitle("Fibre -> 4G failover while the Test Kit loads the line (left of the line: fibre; right: 4G)", fontsize=9)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "charts", "failover.png"), dpi=130); plt.close(fig)
    for fo in S["failover"]:
        fo.pop("samples", None)

# ------------------------------------------------------------------ household
hh = os.path.join(CAP, "run3", "household", "household.json")
if os.path.exists(hh):
    H = json.load(open(hh))
    S["household"] = H
    metrics = [("call_audio_down", "owd_p99", "call audio delay p99 (ms), down"),
               ("call_audio_up", "owd_p99", "call audio delay p99 (ms), up"),
               ("call_audio_down", "mos_mean", "call audio MOS, down"),
               ("call_video_down", "owd_p99", "call video delay p99 (ms), down"),
               ("game_down", "owd_p99", "game delay p99 (ms), down"),
               ("game_up", "owd_p99", "game delay p99 (ms), up"),
               ("iso_download", "goodput_mbps", "ISO download (Mbit/s)"),
               ("cloud_backup", "goodput_mbps", "cloud backup (Mbit/s)"),
               ("tv1", "mean_bitrate_mbps", "TV 1 bitrate (Mbit/s)"),
               ("tv3", "mean_bitrate_mbps", "TV 3 (2.4 GHz) bitrate"),
               ("phone_web", "page_load_ms_p95", "phone page load p95 (ms)")]
    S["household_table"] = []
    for flow, k, lab in metrics:
        o = (H.get("off", {}).get(flow) or {}).get(k)
        n = (H.get("on", {}).get(flow) or {}).get(k)
        S["household_table"].append({"metric": lab, "off": o, "on": n})
    fig, axs = plt.subplots(1, 4, figsize=(12, 3.2))
    for ax, (flow, k, lab) in zip(axs, [metrics[0], metrics[4], metrics[6], metrics[8]]):
        vals = [(H.get(a, {}).get(flow) or {}).get(k) or 0 for a in ("off", "on")]
        ax.bar(["off", "on"], vals, color=[OFF, ON]); ax.set_title(lab, fontsize=9)
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
    fig.suptitle("An evening at home, 30/30 Mbit/s line, every device busy (ground truth, IntentQoS off vs on)", fontsize=10)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "charts", "household.png"), dpi=130); plt.close(fig)

# ------------------------------------------------------------------ router health over the runs
fig, ax = plt.subplots(2, 1, figsize=(11, 4.6), sharex=False)
S["router_health"] = {}
for i, run in enumerate(("run2", "run3")):
    rows = [r for r in jl(os.path.join(CAP, run, "router", "sys.jsonl")) if r.get("cpu", {}).get("cpu")]
    if not rows:
        continue
    t0 = rows[0]["t"]
    xs = [(r["t"] - t0) / 60 for r in rows]
    busy = [r["cpu"]["cpu"]["busy"] for r in rows]
    sirq = [r["cpu"]["cpu"]["softirq"] for r in rows]
    memu = [(r["mem_kb"]["MemTotal"] - r["mem_kb"]["MemAvailable"]) / 1024 for r in rows if r.get("mem_kb")]
    ax[i].fill_between(xs, busy, color="#cfe0ee", label="busy")
    ax[i].plot(xs, sirq, color=ON, lw=0.8, label="softirq (packet work)")
    ax[i].set_ylim(0, 105); ax[i].set_ylabel("CPU %")
    ax[i].set_title({"run2": "Router CPU during the Test Kit run", "run3": "Router CPU during household, failover, resilience"}[run], fontsize=9)
    ax[i].legend(frameon=False, fontsize=7, loc="upper right")
    ax[i].set_xlabel("minutes")
    S["router_health"][run] = {"samples": len(rows), "busy_mean": round(st.mean(busy), 1),
                               "busy_p95": round(sorted(busy)[int(0.95 * (len(busy) - 1))], 1),
                               "seconds_above_90pct": 3 * sum(b > 90 for b in busy),
                               "mem_used_mb_max": round(max(memu), 1) if memu else None,
                               "mem_total_mb": round(rows[0]["mem_kb"]["MemTotal"] / 1024, 1)}
fig.tight_layout(); fig.savefig(os.path.join(OUT, "charts", "router_cpu.png"), dpi=130); plt.close(fig)

# ------------------------------------------------------------------ engine events
ev = []
for run in ("run1", "run2", "run3"):
    ev += [dict(r, run=run) for r in jl(os.path.join(CAP, run, "router", "events.jsonl"))]
import collections  # noqa: E402
kinds = collections.Counter((r.get("kind"), r.get("level")) for r in ev)
S["engine_events"] = {"total": len(ev), "by_kind": {f"{k}/{l}": n for (k, l), n in kinds.most_common()},
                      "watchdog_stale": sum(1 for r in ev if r.get("kind") == "watchdog"),
                      "watchdog_restore_ms": [r.get("restore_ms") for r in ev if r.get("kind") == "watchdog"],
                      "offers": [r.get("msg") for r in ev if r.get("kind") == "offer"],
                      "estimator": [r.get("msg") for r in ev if r.get("kind") == "estimator"],
                      "drift": [r.get("msg") for r in ev if r.get("kind") == "drift"],
                      "cellular": [r.get("msg") for r in ev if r.get("kind") in ("cellular", "failover", "wan")]}

# ------------------------------------------------------------------ resilience
res = jl(os.path.join(CAP, "run3", "resilience", "resilience.jsonl"))
S["resilience"] = [{k: v for k, v in r.items() if k != "samples"} for r in res]
for r in res:
    if r.get("event") == "router_while_console_away":
        S["controller_offline_samples"] = r.get("samples")

# ------------------------------------------------------------------ emulator activity
we = jl(os.path.join(WORK, "wifiemu.conf.events.jsonl"))
S["wifi_events"] = dict(collections.Counter(r.get("event") for r in we))
lt = jl(os.path.join(WORK, "logs", "link-trace.jsonl"))
S["link_trace_changes"] = dict(collections.Counter(r.get("link") for r in lt))
mir = jl(os.path.join(WORK, "logs", "mirror.jsonl"))
S["package_mirror"] = {"requests": len(mir), "bytes": sum(r.get("bytes", 0) for r in mir),
                       "packages": sorted({os.path.basename(r["path"]) for r in mir if r.get("path", "").endswith(".apk")})}
dnsq = os.path.join(WORK, "logs", "inet-dns-queries.log")
S["dns_queries"] = sum(1 for ln in open(dnsq) if " query[" in ln) if os.path.exists(dnsq) else 0

json.dump(S, open(os.path.join(OUT, "summary.json"), "w"), indent=1, default=str)

# ------------------------------------------------------------------ evidence
ev_dir = os.path.join(OUT, "evidence")
os.makedirs(ev_dir, exist_ok=True)
with gzip.open(os.path.join(ev_dir, "timeline.jsonl.gz"), "wt") as f:
    for r in tl:
        f.write(json.dumps(r, default=str) + "\n")
for run in ("run1", "run2", "run3"):
    d = os.path.join(ev_dir, run)
    os.makedirs(d, exist_ok=True)
    for sub in ("router", "engine", "host"):
        for fp in glob.glob(os.path.join(CAP, run, sub, "*")):
            if fp.endswith(".raw"):
                continue
            with open(fp, "rb") as a, gzip.open(os.path.join(d, f"{sub}-{os.path.basename(fp)}.gz"), "wb") as b:
                shutil.copyfileobj(a, b)
    for fp in glob.glob(os.path.join(CAP, run, "pcap", "*.pcap*")):
        pass  # packet headers stay in the work directory (hundreds of MB); flow summaries are in pcap-summary
for sub in ("household", "household-attempt1-no-udp", "resilience"):
    src = os.path.join(CAP, "run3", sub)
    if os.path.isdir(src):
        dst = os.path.join(ev_dir, "run3", sub)
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.pcap*"))
emu = os.path.join(ev_dir, "emulators")
os.makedirs(emu, exist_ok=True)
for fp in glob.glob(os.path.join(WORK, "logs", "*")) + glob.glob(os.path.join(WORK, "wifiemu.conf*")):
    if os.path.isfile(fp):
        with open(fp, "rb") as a, gzip.open(os.path.join(emu, os.path.basename(fp) + ".gz"), "wb") as b:
            shutil.copyfileobj(a, b)
tkd = os.path.join(ev_dir, "testkit-reports")
os.makedirs(tkd, exist_ok=True)
for fp in glob.glob(os.path.join(WORK, "home", "laptop", "Documents", "IntentQoS Reports", "*")):
    shutil.copy(fp, tkd)
for fp in glob.glob(os.path.join(CAP, "screens", "*.jsonl")):
    shutil.copy(fp, ev_dir)
print(json.dumps({k: v for k, v in S.items() if k in ("testkit", "household_table", "router_health", "engine_events",
                                                       "wifi_events", "link_trace_changes")}, default=str)[:6000])
