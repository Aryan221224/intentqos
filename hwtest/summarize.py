#!/usr/bin/env python3
"""Turn hardware-trial runs into the slide's hardware.json (status "measured").

    python3 summarize.py --noqos results/noqos-*.json --intentqos results/intentqos-*.json \
        --device "Raspberry Pi 4" --line "100 Mbps fibre line, laptop on Wi-Fi" --cpu 9 --out hardware.json

Several files per arm are allowed (repeat runs); the median of each run's download-phase p99 is used.
--cpu is the engine's CPU share on the gateway during the download phase (see README, step 6).
"""
import argparse
import json
import statistics


def p99(paths, phase):
    vals = []
    for p in paths:
        run = json.load(open(p))
        for ph in run["phases"]:
            if ph["phase"] == phase and "p99_ms" in ph["ping"]:
                vals.append(ph["ping"]["p99_ms"])
    if not vals:
        raise SystemExit(f"no {phase} phase with ping results in {paths}")
    return round(statistics.median(vals), 1), len(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--noqos", nargs="+", required=True)
    ap.add_argument("--intentqos", nargs="+", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--line", required=True)
    ap.add_argument("--cpu", type=float, required=True, help="engine CPU %% of one core during the download phase")
    ap.add_argument("--phase", default="download", choices=["download", "upload"])
    ap.add_argument("--out", default="hardware.json")
    a = ap.parse_args()
    base, n0 = p99(a.noqos, a.phase)
    iq, n1 = p99(a.intentqos, a.phase)
    out = {"status": "measured", "device": a.device, "line": a.line,
           "metric": f"ping p99 during a full {a.phase}", "noqos_ms": base, "intentqos_ms": iq, "cpu_pct": a.cpu,
           "runs": {"noqos": n0, "intentqos": n1}, "sources": {"noqos": a.noqos, "intentqos": a.intentqos}}
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
