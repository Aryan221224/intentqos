#!/usr/bin/env python3
"""Aggregate experiment runs into statistics, figures and an evidence report.

    python3 -m testbed.report [--results results]

Per scenario x arm x metric: n, median, IQR, 95 % bootstrap CI of the median.
adaptive_ml vs every baseline: relative change of medians and a two-sided
Mann-Whitney U test. Writes results/summary.json, results/REPORT.md,
results/report.html (self-contained) and results/fig_*.png.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARM_ORDER = ["adaptive_ml", "adaptive_ml_bulk", "adaptive_rules", "static", "noqos", "adaptive_ml_intent"]
ARM_NAME = {"adaptive_ml": "IntentQoS (ML)", "adaptive_rules": "IntentQoS (rules)", "static": "Static QoS",
            "noqos": "No QoS", "adaptive_ml_intent": "IntentQoS (ML) + intent",
            "adaptive_ml_bulk": "IntentQoS (ML), bulk-friendly"}
# categorical slots in fixed order (validated: scripts/validate_palette.js, light surface)
ARM_COLOR = {"adaptive_ml": "#2a78d6", "adaptive_rules": "#eb6834", "static": "#1baf7a", "noqos": "#eda100",
             "adaptive_ml_intent": "#e87ba4", "adaptive_ml_bulk": "#7b62d8"}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

# (path, label, unit, better)
METRICS = {
    "s1_iso_during_call": [
        ("audio_down.owd_p99", "Call audio delay p99, download", "ms", "lower"),
        ("audio_up.owd_p99", "Call audio delay p99, upload", "ms", "lower"),
        ("audio_down_onset.owd_p99", "Call audio delay p99, first 6 s of the download", "ms", "lower"),
        ("audio_down.jitter_ms", "Call audio jitter (RFC 3550), download", "ms", "lower"),
        ("audio_down.mos_mean", "Call quality (E-model MOS), download", "MOS", "higher"),
        ("video_down.owd_p99", "Video delay p99, download", "ms", "lower"),
        ("video_up.owd_p99", "Video delay p99, upload", "ms", "lower"),
        ("video_up.loss", "Video packet loss, upload", "fraction", "lower"),
        ("iso_download.goodput_mbps", "ISO download goodput", "Mbit/s", "higher"),
        ("iso_download.starved_seconds", "ISO download seconds with no progress (< 10 kbit/s)", "s", "lower"),
        ("cloud_backup.goodput_mbps", "Cloud backup goodput", "Mbit/s", "higher"),
        ("browse.page_load_ms_p95", "Web page load p95", "ms", "lower"),
    ],
    "s2_wan_drop": [
        ("audio_down_post.owd_p99", "Call audio delay p99 after the drop, download", "ms", "lower"),
        ("audio_down_settled.owd_p99", "Call audio delay p99, 10 s after the drop onward", "ms", "lower"),
        ("peak_owd_after_drop_ms", "Peak call delay in the 15 s after the drop", "ms", "lower"),
        ("recovery_s", "Time until call delay stays within 30 ms of baseline", "s", "lower"),
        ("audio_down_post.mos_mean", "Call quality (MOS) after the drop", "MOS", "higher"),
        ("audio_up_post.owd_p99", "Call audio delay p99 after the drop, upload", "ms", "lower"),
        ("download_post.goodput_mbps", "Bulk download goodput after the drop", "Mbit/s", "higher"),
        ("download_post.starved_seconds", "Bulk download seconds with no progress after the drop (< 10 kbit/s)", "s", "lower"),
        ("tv_stream.rebuffer_s", "TV rebuffering", "s", "lower"),
    ],
    "s3_tvs_and_gaming": [
        ("game_down.owd_p99", "Game delay p99, download", "ms", "lower"),
        ("game_up.owd_p99", "Game delay p99, upload", "ms", "lower"),
        ("game_down.jitter_ms", "Game jitter, download", "ms", "lower"),
        ("tv_mean_bitrate_mbps", "Mean TV video bitrate", "Mbit/s", "higher"),
        ("tv_rebuffer_s_total", "TV rebuffering, all three TVs", "s", "lower"),
        ("tv_jain_bitrate", "Fairness across TVs (Jain, bitrate)", "index", "higher"),
        ("game_update.goodput_mbps", "Game-update download goodput", "Mbit/s", "higher"),
        ("game_update.starved_seconds", "Game-update seconds with no progress (< 10 kbit/s)", "s", "lower"),
    ],
    "s4_intent": [
        ("laptop_video_up.owd_p99", "Work call (laptop) video delay p99, upload", "ms", "lower"),
        ("laptop_audio_up.owd_p99", "Work call (laptop) audio delay p99, upload", "ms", "lower"),
        ("laptop_audio_up.mos_mean", "Work call (laptop) MOS, upload", "MOS", "higher"),
        ("laptop_video_up.loss", "Work call (laptop) video loss, upload", "fraction", "lower"),
        ("phone_video_up.owd_p99", "Other call (phone) video delay p99, upload", "ms", "lower"),
        ("phone_audio_up.mos_mean", "Other call (phone) MOS, upload", "MOS", "higher"),
        ("download.goodput_mbps", "Bulk download goodput", "Mbit/s", "higher"),
        ("backup.goodput_mbps", "Backup upload goodput", "Mbit/s", "higher"),
        ("backup.min_1s_mbps", "Backup worst 1-second goodput", "Mbit/s", "higher"),
        ("backup.starved_seconds", "Backup seconds with no progress (< 10 kbit/s)", "s", "lower"),
    ],
    "s5_faults": [
        ("watchdog_reaction_s", "Engine killed -> safe state installed", "s", "lower"),
        ("audio_down_before_kill.owd_p99", "Call delay p99, engine running", "ms", "lower"),
        ("audio_down_engine_dead_safe_state.owd_p99", "Call delay p99, engine dead (watchdog safe state)", "ms", "lower"),
        ("audio_down_after_restart.owd_p99", "Call delay p99, after restart", "ms", "lower"),
        ("audio_down_reflectors_blackholed.owd_p99", "Call delay p99, delay reflectors black-holed", "ms", "lower"),
        ("download_engine_dead.starved_seconds", "Download seconds with no progress while the engine was dead", "s", "lower"),
    ],
}
TITLES = {
    "s1_iso_during_call": "S1. A large ISO download (and a cloud backup) start during a video call",
    "s2_wan_drop": "S2. WAN capacity drops from 100 to 20 Mbit/s (upload 20 to 5)",
    "s3_tvs_and_gaming": "S3. Three TVs stream while a console games and a game update downloads",
    "s4_intent": "S4. Two video calls share a 10/3 Mbit/s link; the user prioritises the work call",
    "s5_faults": "S5. Fault injection: engine SIGKILL, restart, delay reflectors black-holed",
}
# with a field-calibrated profile (testbed/profiles) the line, the drop and the thin link are the profile's
PROFILE_TITLES = {
    "india_ftth_evening": {
        "s2_wan_drop": "S2. The fibre line fails over to a 4G backup (100/100 to 20/8 Mbit/s, +15 ms, 300 ms modem buffer)",
        "s4_intent": "S4. Two video calls on the 4G backup link (20/8 Mbit/s); the user prioritises the work call",
    },
}
DROP_TEXT = {"india_ftth_evening": "fibre fails over to 4G (20/8 Mbit/s)"}
PROFILE = None      # set from environment.json in main()


def title(sc):
    return PROFILE_TITLES.get(PROFILE or "", {}).get(sc, TITLES[sc])


def get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d or d[k] is None:
            return None
        d = d[k]
    return d if isinstance(d, (int, float)) else None


def boot_ci(x, n=2000, seed=0):
    if len(x) < 3:
        return None, None
    rng = np.random.default_rng(seed)
    meds = np.median(rng.choice(x, size=(n, len(x)), replace=True), axis=1)
    return float(np.percentile(meds, 2.5)), float(np.percentile(meds, 97.5))


def mwu(a, b):
    try:
        from scipy.stats import mannwhitneyu
        if len(a) >= 3 and len(b) >= 3:
            return float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
    except Exception:  # noqa: BLE001
        pass
    return None


def load_runs(results):
    runs = {}
    for p in glob.glob(os.path.join(results, "runs", "*", "*", "seed*", "metrics.json")):
        with open(p) as f:
            m = json.load(f)
        if "error" in m:
            continue
        m["_dir"] = os.path.dirname(p)
        runs.setdefault(m["scenario"], {}).setdefault(m["arm"], []).append(m)
    return runs


def summarise(runs):
    out = {}
    for sc, metrics in METRICS.items():
        if sc not in runs:
            continue
        arms = [a for a in ARM_ORDER if a in runs[sc]]
        out[sc] = {"title": title(sc), "arms": {a: len(runs[sc][a]) for a in arms}, "metrics": []}
        ref = "adaptive_ml_intent" if sc == "s4_intent" else "adaptive_ml"
        for path, label, unit, better in metrics:
            row = {"path": path, "label": label, "unit": unit, "better": better, "arms": {}}
            vals = {a: [v for v in (get(r, path) for r in runs[sc][a]) if v is not None] for a in arms}
            for a in arms:
                x = np.array(vals[a], dtype=float)
                if len(x) == 0:
                    continue
                lo, hi = boot_ci(x)
                row["arms"][a] = {"n": int(len(x)), "median": float(np.median(x)), "p25": float(np.percentile(x, 25)),
                                  "p75": float(np.percentile(x, 75)), "ci95": [lo, hi], "values": [float(v) for v in x]}
            if ref in row["arms"]:
                row["vs"] = {}
                for a in arms:
                    if a == ref or a not in row["arms"]:
                        continue
                    m_ref, m_a = row["arms"][ref]["median"], row["arms"][a]["median"]
                    rel = (m_ref - m_a) / abs(m_a) if m_a else None
                    row["vs"][a] = {"relative_change": rel, "p_value": mwu(vals[ref], vals[a])}
            out[sc]["metrics"].append(row)
    return out


# ------------------------------------------------------------------ figures
def _style(ax, title, unit):
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    ax.set_ylabel(unit, fontsize=9, color=INK2)
    ax.tick_params(colors=INK2, labelsize=8.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def strip(ax, runs, sc, path, title, unit, arms, log=False):
    rng = np.random.default_rng(1)
    for i, a in enumerate(arms):
        v = [x for x in (get(r, path) for r in runs[sc].get(a, [])) if x is not None]
        if not v:
            continue
        jit = rng.uniform(-0.12, 0.12, len(v))
        ax.scatter(np.full(len(v), i) + jit, v, s=26, color=ARM_COLOR[a], edgecolor="white", linewidth=1.2, zorder=3)
        med = float(np.median(v))
        ax.plot([i - 0.28, i + 0.28], [med, med], color=INK, linewidth=2, solid_capstyle="round", zorder=4)
        ax.annotate(f"{med:.0f}" if med >= 10 else f"{med:.2f}", (i + 0.31, med), fontsize=8, color=INK, va="center")
    ax.set_xticks(range(len(arms)), [ARM_NAME[a].replace("IntentQoS ", "IntentQoS\n").replace(" + ", "\n+ ") for a in arms],
                  fontsize=8.5)
    ax.set_xlim(-0.5, len(arms) - 0.3)
    if log:
        from matplotlib.ticker import NullFormatter, ScalarFormatter
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(ScalarFormatter())
        ax.yaxis.set_minor_formatter(NullFormatter())
    _style(ax, title, unit)


def fig_save(fig, path):
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="#fcfcfb")
    import matplotlib.pyplot as plt
    plt.close(fig)


def figures(runs, results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.facecolor": "#fcfcfb"})
    figs = {}
    main4 = ["noqos", "static", "adaptive_rules", "adaptive_ml"]
    if "s1_iso_during_call" in runs:
        sc = "s1_iso_during_call"
        arms = [a for a in main4 if a in runs[sc]]
        fig, axs = plt.subplots(1, 3, figsize=(13, 3.6))
        strip(axs[0], runs, sc, "audio_down.owd_p99", "Call audio delay p99, download", "ms (log)", arms, log=True)
        strip(axs[1], runs, sc, "audio_up.owd_p99", "Call audio delay p99, upload", "ms (log)", arms, log=True)
        strip(axs[2], runs, sc, "iso_download.goodput_mbps", "ISO download goodput", "Mbit/s", arms)
        fig.suptitle("S1: shaping removes upload bloat; the download keeps ~80 Mbit/s (each dot = one seed)",
                     fontsize=11, x=0.01, ha="left", color=INK)
        fig.tight_layout()
        p = os.path.join(results, "fig_s1.png")
        fig_save(fig, p)
        figs[sc] = p
    if "s2_wan_drop" in runs:
        sc = "s2_wan_drop"
        arms = [a for a in main4 if a in runs[sc]]
        fig, axs = plt.subplots(2, 1, figsize=(11, 6.4), sharex=True, gridspec_kw={"height_ratios": [1.6, 1]})
        ax = axs[0]
        for a in arms:
            bins = {}
            for r in runs[sc][a]:
                off = r["params"]["drop_t"] - 15.0
                for t, p50, mx in r.get("ts_audio_down", []):
                    bins.setdefault(round((t - off) * 2) / 2, []).append(p50)
            xs = sorted(bins)
            ys = [float(np.median(bins[x])) for x in xs]
            ax.plot(xs, ys, color=ARM_COLOR[a], linewidth=2, label=ARM_NAME[a])
        ax.axvline(15, color=INK2, linestyle=(0, (4, 4)), linewidth=1)
        ax.text(15.4, 0.93, DROP_TEXT.get(PROFILE or "", "ISP capacity drops 100 to 20 Mbit/s"), transform=ax.get_xaxis_transform(),
                fontsize=8.5, color=INK2)
        ax.set_yscale("log")
        ax.legend(fontsize=8, frameon=False, loc="upper left")
        _style(ax, "Call audio one-way delay, download (median of seeds, 0.5 s bins)", "ms (log)")
        ax2 = axs[1]
        r0 = sorted(runs[sc].get("adaptive_ml", []), key=lambda r: r["seed"])
        if r0:
            r0 = r0[0]
            tsp = os.path.join(r0["_dir"], "timeseries.jsonl")
            mk = os.path.join(r0["_dir"], "marks.json")
            if os.path.exists(tsp) and os.path.exists(mk):
                t0 = json.load(open(mk))["t0"]
                rows = [json.loads(x) for x in open(tsp)]
                off = r0["params"]["drop_t"] - 15.0
                tx = [r["t"] - t0 - off for r in rows]
                ax2.step(tx, [r["rd"] / 1000 for r in rows], where="post", color=ARM_COLOR["adaptive_ml"], linewidth=2,
                         label="IntentQoS shaper rate")
                ax2.plot(tx, [r["ad"] / 1000 for r in rows], color=INK2, linewidth=1.2, label="achieved throughput")
                cap_x = [min(tx), 15, 15, max(tx)]
                ax2.plot(cap_x, [100, 100, 20, 20], color=INK, linestyle=(0, (2, 2)), linewidth=1.2,
                         label="true ISP capacity (emulated)")
                ax2.legend(fontsize=8, frameon=False, loc="upper right")
        ax2.set_xlabel("seconds", fontsize=9, color=INK2)
        _style(ax2, f"How the estimator tracked the drop (IntentQoS ML, seed {r0['seed'] if r0 else '-'})", "Mbit/s")
        fig.tight_layout()
        p = os.path.join(results, "fig_s2.png")
        fig_save(fig, p)
        figs[sc] = p
    if "s3_tvs_and_gaming" in runs:
        sc = "s3_tvs_and_gaming"
        arms = [a for a in main4 if a in runs[sc]]
        fig, axs = plt.subplots(1, 3, figsize=(13, 3.6))
        strip(axs[0], runs, sc, "game_down.owd_p99", "Game delay p99, download", "ms (log)", arms, log=True)
        strip(axs[1], runs, sc, "tv_mean_bitrate_mbps", "Mean TV bitrate", "Mbit/s", arms)
        strip(axs[2], runs, sc, "game_update.goodput_mbps", "Game update goodput", "Mbit/s", arms)
        fig.suptitle("S3: rules give the game the lowest delay; ML recognises streaming and keeps TV bitrate up, at the update's expense",
                     fontsize=11, x=0.01, ha="left", color=INK)
        fig.tight_layout()
        p = os.path.join(results, "fig_s3.png")
        fig_save(fig, p)
        figs[sc] = p
    if "s4_intent" in runs:
        sc = "s4_intent"
        arms = [a for a in ("adaptive_ml", "adaptive_ml_intent") if a in runs[sc]]
        fig, axs = plt.subplots(1, 3, figsize=(13, 3.6))
        strip(axs[0], runs, sc, "laptop_video_up.owd_p99", "Work call video delay p99, upload", "ms (log)", arms, log=True)
        strip(axs[1], runs, sc, "phone_video_up.owd_p99", "Other call video delay p99, upload", "ms (log)", arms, log=True)
        strip(axs[2], runs, sc, "backup.goodput_mbps", "Backup upload goodput", "Mbit/s", arms)
        fig.suptitle("S4: the lease protects the call the user chose; the other call's video and the backup pay for it",
                     fontsize=11, x=0.01, ha="left", color=INK)
        fig.tight_layout()
        p = os.path.join(results, "fig_s4.png")
        fig_save(fig, p)
        figs[sc] = p
    if "s5_faults" in runs:
        sc = "s5_faults"
        r0 = sorted(runs[sc].get("adaptive_ml", []), key=lambda r: r["seed"])
        if r0:
            r0 = r0[0]
            fig, ax = plt.subplots(figsize=(11, 3.4))
            ts = r0.get("ts_audio_down", [])
            ax.plot([t for t, _, _ in ts], [m for _, _, m in ts], color=ARM_COLOR["adaptive_ml"], linewidth=1.6,
                    label="call audio delay, max per 0.5 s")
            tl = r0["timeline"]
            ax.axvspan(tl["kill"], tl["restart"], color="#eda100", alpha=0.15, lw=0)
            ax.axvspan(tl["blackhole"][0], tl["blackhole"][1], color="#e34948", alpha=0.10, lw=0)
            ax.annotate("engine SIGKILLed: watchdog installs the safe state", (tl["kill"] + 0.3, 0.92),
                        xycoords=("data", "axes fraction"), fontsize=8, color=INK)
            ax.annotate("reflectors black-holed: blind mode", (tl["blackhole"][0] + 0.3, 0.80),
                        xycoords=("data", "axes fraction"), fontsize=8, color=INK)
            ax.set_xlabel("seconds", fontsize=9, color=INK2)
            _style(ax, f"S5: the call stays usable through every injected fault (seed {r0['seed']})", "ms")
            fig.tight_layout()
            p = os.path.join(results, "fig_s5.png")
            fig_save(fig, p)
            figs[sc] = p
    return figs


# ------------------------------------------------------------------ text
def fmt(v, unit):
    if v is None:
        return "–"
    if unit == "fraction":
        return f"{v * 100:.1f}%"
    if unit in ("MOS", "index"):
        return f"{v:.2f}"
    return f"{v:.0f}" if abs(v) >= 100 else (f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}")


def markdown(summary, env, figs, results):
    tb = env.get('testbed', {})
    rdir = os.path.basename(os.path.normpath(results))
    if PROFILE:
        setup = (f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{rdir}/runs/`. **Field-calibrated profile "
                 f"`{PROFILE}`** ({(env.get('profile_title') or tb.get('profile_title') or 'see docs/FIELD_EMULATION.md')}): "
                 f"{tb.get('nominal_down', '?')}/{tb.get('nominal_up', '?')} Mbit/s plan, ISP buffer 50 ms, evening capacity "
                 f"dips, home Wi-Fi per device, line drop = failover to a 4G backup, servers {tb.get('server_cc', '?')} and "
                 f"clients {tb.get('tcp_cc', '?')}, the engine limited to a router-class CPU budget. Linux "
                 f"{env.get('kernel', '?')}, {tb.get('delay_method', '?')}. **All network conditions are emulated**; the "
                 "profile's values and sources are in `testbed/profiles/` and `docs/FIELD_EMULATION.md`.")
    else:
        setup = (f"Generated {time.strftime('%Y-%m-%d %H:%M')} from `{rdir}/runs/`. Testbed: Linux {env.get('kernel', '?')}, "
                 f"{tb.get('delay_method', 'netem')} base delay, TCP {tb.get('tcp_cc', 'cubic')} on hosts, ISP buffer "
                 f"{tb.get('isp_buffer_ms', '?')} ms at nominal rate. **All network conditions are emulated** "
                 "(network namespaces, `tbf` bottleneck); see `docs/KNOWN_LIMITATIONS.md`.")
    L = ["# IntentQoS evaluation report", "", setup, "",
         "Each cell is the **median across seeds** with the interquartile range in brackets. The last column compares "
         "IntentQoS (ML) against every other arm: relative change of the median and the two-sided Mann-Whitney U "
         "p-value. Arms: *No QoS* (bloated ISP modem only), *Static QoS* (fixed shaper at 90 % of plan down / 95 % up, fixed shares, "
         "rule classifier), *IntentQoS (rules)*, *IntentQoS (ML)*, and in S3 *IntentQoS (ML), bulk-friendly* (the operator's "
         "bulk-friendly profile: bulk keeps 12 % and half of what is left).", ""]
    for sc, s in summary.items():
        arms = [a for a in ARM_ORDER if a in s["arms"]]
        ref = "adaptive_ml_intent" if sc == "s4_intent" else "adaptive_ml"
        L += [f"## {s['title']}", "", f"Runs per arm: " + ", ".join(f"{ARM_NAME[a]} {s['arms'][a]}" for a in arms), ""]
        hdr = "| Metric | " + " | ".join(ARM_NAME[a] for a in arms) + (" | IntentQoS vs others |" if len(arms) > 1 else " |")
        L += [hdr, "|" + "---|" * (len(arms) + (2 if len(arms) > 1 else 1))]
        for m in s["metrics"]:
            cells = []
            for a in arms:
                x = m["arms"].get(a)
                cells.append(f"{fmt(x['median'], m['unit'])} [{fmt(x['p25'], m['unit'])}–{fmt(x['p75'], m['unit'])}]"
                             if x else "–")
            cmp_ = ""
            if m.get("vs"):
                parts = []
                for a, v in m["vs"].items():
                    if v["relative_change"] is None:
                        continue
                    better = (v["relative_change"] < 0) if m["better"] == "lower" else (v["relative_change"] > 0)
                    p = (", p<0.001" if v["p_value"] < 0.001 else f", p={v['p_value']:.3f}") if v["p_value"] is not None else ""
                    parts.append(f"{'better' if better else 'worse'} than {ARM_NAME[a]} by "
                                 f"{abs(v['relative_change']) * 100:.0f}%{p}")
                cmp_ = "; ".join(parts)
            u = "" if m["unit"] in ("fraction", "index") else f" ({m['unit']})"
            L.append(f"| {m['label']}{u} | " + " | ".join(cells) + (f" | {cmp_} |" if len(arms) > 1 else " |"))
        if sc in figs:
            L += ["", f"![{sc}]({os.path.basename(figs[sc])})"]
        L.append("")
    with open(os.path.join(results, "REPORT.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    return "\n".join(L)


def html(md_text, figs, results):
    try:
        import markdown as mdlib  # optional
        body = mdlib.markdown(md_text, extensions=["tables"])
    except ImportError:
        body = _mini_md(md_text)
    for sc, p in figs.items():
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        body = body.replace(f'src="{os.path.basename(p)}"', f'src="data:image/png;base64,{b64}"')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IntentQoS evaluation</title><style>
body{{margin:0;background:#fcfcfb;color:#0b0b0b;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:24px 20px 60px}} h1{{font-size:28px}} h2{{font-size:19px;margin-top:36px;border-top:2px solid #0b0b0b;padding-top:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}} th,td{{border-bottom:1px solid #e4e3df;padding:5px 6px;text-align:left;vertical-align:top}}
th{{border-bottom:1.5px solid #0b0b0b}} img{{max-width:100%;height:auto;margin:10px 0}} code{{background:#efeee9;padding:0 4px;border-radius:4px}}
@media (prefers-color-scheme: dark){{body{{background:#1a1a19;color:#fff}} th,td{{border-color:#3a3936}} th,h2{{border-color:#fff}} code{{background:#2a2a28}} img{{background:#fcfcfb;border-radius:6px}}}}
</style></head><body><main>{body}</main></body></html>"""
    with open(os.path.join(results, "report.html"), "w") as f:
        f.write(page)


def _mini_md(md):
    import html as H
    out, in_table = [], False
    for line in md.split("\n"):
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(line.replace("|", "").strip()) <= {"-"}:
                continue
            if not in_table:
                out.append("<table><tr>" + "".join(f"<th>{H.escape(c)}</th>" for c in cells) + "</tr>")
                in_table = True
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(H.escape(c))}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("# "):
            out.append(f"<h1>{H.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{H.escape(line[3:])}</h2>")
        elif line.startswith("!["):
            src = line[line.index("(") + 1:-1]
            out.append(f'<img alt="figure" src="{src}">')
        elif line.strip():
            out.append(f"<p>{_inline(H.escape(line))}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _inline(s):
    import re
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default=os.path.join(ROOT, "results"))
    a = ap.parse_args(argv)
    runs = load_runs(a.results)
    if not runs:
        print("no runs found", file=sys.stderr)
        return 1
    env = {}
    try:
        env = json.load(open(os.path.join(a.results, "environment.json")))
    except FileNotFoundError:
        pass
    global PROFILE
    prof = env.get("profile") or (env.get("testbed") or {}).get("profile")
    if isinstance(prof, dict):
        env["profile_title"] = prof.get("title")
        prof = prof.get("name")
    PROFILE = prof
    summary = summarise(runs)
    with open(os.path.join(a.results, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    figs = figures(runs, a.results)
    md = markdown(summary, env, figs, a.results)
    html(md, figs, a.results)
    print(f"wrote {a.results}/REPORT.md, report.html, summary.json and {len(figs)} figures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
