"""Outcome control on vs off: per-scenario medians over seeds, endpoint-measured (emulation).

    sudo python3 -m testbed.run_eval --seeds 4 --scenarios s1_iso_during_call,s3_tvs_and_gaming \
        --arms adaptive_ml,adaptive_ml_no_outcome --out results-ablation
    python3 testbed/ablation_outcome.py results-ablation > results-ablation/outcome_on_vs_off.json
"""
import glob
import json
import os
import statistics as st
import sys

BASE = sys.argv[1]
METRICS = {
    "s1_iso_during_call": [("call audio down, delay p99 (ms)", lambda m: m["audio_down"]["owd_p99"]),
                           ("call audio down, MOS", lambda m: m["audio_down"]["mos_mean"]),
                           ("call video down, delay p99 (ms)", lambda m: m["video_down"]["owd_p99"]),
                           ("ISO download goodput (Mbit/s)", lambda m: m["iso_download"]["goodput_mbps"]),
                           ("cloud backup goodput (Mbit/s)", lambda m: m["cloud_backup"]["goodput_mbps"])],
    "s3_tvs_and_gaming": [("game down, delay p99 (ms)", lambda m: m["game_down"]["owd_p99"]),
                          ("game down, jitter (ms)", lambda m: m["game_down"]["jitter_ms"]),
                          ("TV mean bitrate (Mbit/s)", lambda m: m["tv_mean_bitrate_mbps"]),
                          ("TV rebuffering (s, total)", lambda m: m["tv_rebuffer_s_total"])],
}


def mw(a, b):
    """Two-sided Mann-Whitney U p-value (normal approximation; small n, indicative only)."""
    import math
    n1, n2 = len(a), len(b)
    allv = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks = {}
    i = 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        for k in range(i, j):
            ranks.setdefault(k, (i + j + 1) / 2)
        i = j
    r1 = sum(ranks[k] for k, (_, g) in enumerate(allv) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    mu, sd = n1 * n2 / 2, math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    z = (u - mu) / sd if sd else 0
    return math.erfc(abs(z) / math.sqrt(2))


out = {}
for sc, ms in METRICS.items():
    out[sc] = {}
    for name, f in ms:
        row = {}
        for arm in ("adaptive_ml", "adaptive_ml_no_outcome"):
            vals = []
            for p in sorted(glob.glob(os.path.join(BASE, "runs", sc, arm, "seed*", "metrics.json"))):
                try:
                    vals.append(f(json.load(open(p))))
                except (KeyError, TypeError):
                    pass
            row[arm] = vals
        a, b = row["adaptive_ml"], row["adaptive_ml_no_outcome"]
        out[sc][name] = {"outcome_on_median": round(st.median(a), 2) if a else None,
                         "outcome_off_median": round(st.median(b), 2) if b else None,
                         "n": [len(a), len(b)], "p_mw": round(mw(a, b), 3) if a and b else None,
                         "on": [round(x, 2) for x in a], "off": [round(x, 2) for x in b]}
print(json.dumps(out, indent=1))
