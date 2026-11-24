#!/usr/bin/env python3
"""Capacity traces for the two access links (EMULATION), applied with `tc qdisc change` so queues are kept.

Fibre (evening): every hold (exponential, mean 30 s) the downstream rate becomes plan x factor, factor drawn
from the field profile's levels (85 % 0.9-1.0, 12 % 0.6-0.9, 3 % 0.3-0.6). The byte buffer stays as sized at
the plan rate, as in a real modem.
4G: every hold (exponential, mean 2 s) both directions become nominal x lognormal(median 1, sigma 0.35),
clipped to 0.25-2.2 x (India median 4G about 19-20 Mbit/s down, 8 up).
Every change is logged to WORK/logs/link-trace.jsonl.
"""
import json, math, os, random, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from world import PLAN, PROFILE, LTE, WORK, buffer_bytes  # noqa: E402

def tbf(ns, dev, rate, limit):
    burst = max(16000, int(rate * 1e6 / 8 / 250))
    subprocess.run(f"ip netns exec {ns} tc qdisc change dev {dev} root tbf rate {rate:.2f}mbit burst {burst} limit {limit}",
                   shell=True, capture_output=True)

rnd = random.Random(11)
log = open(os.path.join(WORK, "logs", "link-trace.jsonl"), "a")
f = PROFILE["plan"]["rate_factor"]
levels = PROFILE["evening_capacity"]["levels"]
hold = PROFILE["evening_capacity"]["hold_mean_s"]
lim_f = buffer_bytes(PLAN["down"], PROFILE["buffers"]["down_ms"])
lim_ld, lim_lu = buffer_bytes(LTE["down_mbit"], LTE["buffer_ms"]), buffer_bytes(LTE["up_mbit"], LTE["buffer_ms"])
next_f, next_l = time.time(), time.time()
while True:
    now = time.time()
    if now >= next_f:
        r = rnd.random(); acc = 0
        for share, lo, hi in levels:
            acc += share
            if r <= acc:
                break
        k = rnd.uniform(lo, hi)
        tbf("w-isp", "eth-ont", PLAN["down"] * f * k, lim_f)
        log.write(json.dumps({"t": now, "link": "fibre", "dir": "down", "mbit": round(PLAN["down"] * f * k, 2)}) + "\n")
        next_f = now + rnd.expovariate(1 / hold)
    if now >= next_l:
        k = min(2.2, max(0.25, math.exp(rnd.gauss(0, 0.35))))
        tbf("w-lte", "eth-ue", LTE["down_mbit"] * k, lim_ld)
        tbf("w-lte", "eth-inet", LTE["up_mbit"] * k, lim_lu)
        log.write(json.dumps({"t": now, "link": "4g", "down": round(LTE["down_mbit"] * k, 2),
                              "up": round(LTE["up_mbit"] * k, 2)}) + "\n")
        next_l = now + rnd.expovariate(1 / 2.0)
    log.flush()
    time.sleep(max(0.05, min(next_f, next_l) - time.time()))
