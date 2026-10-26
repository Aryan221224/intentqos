"""Turn raw generator records into quality-of-experience metrics.

Realtime (VoIP / video / gaming), per direction:
  one-way delay p50/p95/p99 (exact: shared clock, kernel RX timestamps),
  RFC 3550 interarrival jitter, loss (sequence gaps), and for VoIP a MOS estimate
  from the ITU-T G.107 E-model (simplified, G.711 with PLC, 60 ms jitter buffer;
  packets later than the buffer count as lost) -- a call-quality *proxy*.
Bulk: goodput per second, mean, min, and starved seconds (< 10 kbit/s while active).
Adaptive streaming: mean selected bitrate after start-up, rebuffer seconds, switches.
"""
from __future__ import annotations

import glob
import json
import math
import os

import numpy as np

JB_MS = 60.0          # playout (jitter) buffer of a typical softphone
CODEC_MS = 20.0       # packetisation delay


def _load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _flow_hash_file(outdir, fh):
    return os.path.join(outdir, f"up_{fh}.json")


def realtime_records(outdir: str, name: str):
    """Return {'down': (tx_s, rx_s, seq), 'up': (...)} arrays in wall-clock seconds."""
    c = _load(os.path.join(outdir, f"{name}.json"))
    if c is None:
        return None
    out = {"meta": {k: c.get(k) for k in ("kind", "src", "start_wall", "duration")}}
    dn = c["down"]
    out["down"] = (np.asarray(dn["tx"], dtype=float) / 1e9, np.asarray(dn["rx"], dtype=float) / 1e9,
                   np.asarray(dn["seq"], dtype=float))
    s = _load(_flow_hash_file(outdir, c["flow_hash"]))
    if s is not None:
        out["up"] = (np.asarray(s["tx"], dtype=float) / 1e9, np.asarray(s["rx"], dtype=float) / 1e9,
                     np.asarray(s["seq"], dtype=float))
    return out


def _window(rec, a, b):
    tx, rx, seq = rec
    m = (tx >= a) & (tx < b)
    return tx[m], rx[m], seq[m]


def rfc3550_jitter(tx, rx):
    if len(tx) < 2:
        return None
    order = np.argsort(rx)
    transit = (rx - tx)[order]
    j = 0.0
    for d in np.abs(np.diff(transit)):
        j += (d - j) / 16.0
    return j * 1000.0


def loss_frac(seq):
    if len(seq) < 2:
        return None
    exp = seq.max() - seq.min() + 1
    return max(0.0, 1.0 - len(np.unique(seq)) / exp)


def emodel_mos(owd_ms, loss, base_ms):
    """Simplified ITU-T G.107 E-model for G.711 + PLC (Ie=0, Bpl=25.1)."""
    late = float(np.mean(owd_ms > base_ms + JB_MS)) if len(owd_ms) else 0.0
    ppl = 100.0 * min(1.0, (loss or 0.0) + late * (1 - (loss or 0.0)))
    d = float(np.median(owd_ms)) + JB_MS + CODEC_MS if len(owd_ms) else 0.0
    Id = 0.024 * d + (0.11 * (d - 177.3) if d > 177.3 else 0.0)
    Ie = 0.0 + (95.0 - 0.0) * ppl / (ppl + 25.1)
    R = max(0.0, min(100.0, 93.2 - Id - Ie))
    mos = 1 + 0.035 * R + 7e-6 * R * (R - 60) * (100 - R)
    return max(1.0, min(4.5, mos))


def realtime_metrics(rec, a, b, base_ms=None, mos=False):
    tx, rx, seq = _window(rec, a, b)
    if len(tx) < 5:
        return None
    owd = (rx - tx) * 1000.0
    out = {"n": int(len(owd)), "owd_p50": float(np.percentile(owd, 50)), "owd_p95": float(np.percentile(owd, 95)),
           "owd_p99": float(np.percentile(owd, 99)), "owd_max": float(owd.max()),
           "jitter_ms": rfc3550_jitter(tx, rx), "loss": loss_frac(seq)}
    if mos:
        base = base_ms if base_ms is not None else float(np.percentile(owd, 1))
        per = []
        for s in np.arange(a, b - 0.999, 1.0):
            m = (tx >= s) & (tx < s + 1)
            if m.sum() >= 5:
                per.append(emodel_mos(owd[m], loss_frac(seq[m]), base))
        out["mos_mean"] = float(np.mean(per)) if per else None
        out["poor_seconds_frac"] = float(np.mean([x < 3.6 for x in per])) if per else None
        out["mos_min"] = float(np.min(per)) if per else None
    return out


def realtime_timeseries(rec, t0, step=0.5):
    tx, rx, _ = rec
    if len(tx) == 0:
        return []
    owd = (rx - tx) * 1000.0
    rel = tx - t0
    out = []
    for s in np.arange(math.floor(rel.min()), math.ceil(rel.max()), step):
        m = (rel >= s) & (rel < s + step)
        if m.any():
            out.append([round(float(s), 2), round(float(np.percentile(owd[m], 50)), 2),
                        round(float(owd[m].max()), 2)])
    return out


def bulk_series(outdir: str, name: str):
    """Goodput Mbit/s per 0.25 s bin, absolute wall time of each bin start."""
    c = _load(os.path.join(outdir, f"{name}.json"))
    if c is None:
        return None
    if c.get("direction") == "up":
        bins = {}
        t0 = None
        for p in glob.glob(os.path.join(outdir, f"upbulk_{name}_*.json")):
            d = _load(p)
            if not d:
                continue
            t0 = d["t0_wall"] if t0 is None else min(t0, d["t0_wall"])
        for p in glob.glob(os.path.join(outdir, f"upbulk_{name}_*.json")):
            d = _load(p)
            if not d:
                continue
            off = int(round((d["t0_wall"] - t0) / 0.25))
            for k, v in d["bins"].items():
                bins[int(k) + off] = bins.get(int(k) + off, 0) + v
        start = t0 if t0 is not None else c["start_wall"]
    else:
        bins = {int(k): v for k, v in c["bins"].items()}
        start = c["start_wall"]
    if not bins:
        return {"start": start, "t": np.array([]), "mbps": np.array([])}
    n = max(bins) + 1
    arr = np.array([bins.get(i, 0) for i in range(n)], dtype=float) * 8 / 0.25 / 1e6
    return {"start": start, "t": start + np.arange(n) * 0.25, "mbps": arr}


def bulk_metrics(series, a, b):
    if series is None or len(series["t"]) == 0:
        return None
    m = (series["t"] >= a) & (series["t"] < b)
    if not m.any():
        return None
    x = series["mbps"][m]
    secs = [x[i:i + 4].mean() for i in range(0, len(x) - 3, 4)]
    # starvation = a whole second with essentially no progress (< 10 kbit/s)
    return {"goodput_mbps": float(x.mean()), "min_1s_mbps": float(min(secs)) if secs else None,
            "starved_seconds": int(sum(1 for s in secs if s < 0.01))}


def abr_metrics(outdir: str, name: str, a_rel: float = 0.0):
    c = _load(os.path.join(outdir, f"{name}.json"))
    if c is None:
        return None
    ch = [x for x in c["chunks"] if x["t"] >= a_rel]
    if not ch:
        return None
    rates = [x["mbps"] for x in ch]
    sw = sum(1 for i in range(1, len(rates)) if rates[i] != rates[i - 1])
    return {"mean_bitrate_mbps": float(np.mean(rates)), "rebuffer_s": float(c.get("rebuffer_s") or 0.0),
            "startup_s": c.get("startup_s"), "switches": sw, "chunks": len(ch),
            "mean_chunk_throughput_mbps": float(np.mean([x["thr"] for x in ch]))}


def web_metrics(outdir: str, name: str):
    c = _load(os.path.join(outdir, f"{name}.json"))
    if c is None or not c.get("loads"):
        return None
    p = [x["plt_ms"] for x in c["loads"]]
    return {"page_load_ms_p50": float(np.median(p)), "page_load_ms_p95": float(np.percentile(p, 95)), "pages": len(p)}


def jain(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    s, q = sum(xs), sum(x * x for x in xs)
    return (s * s) / (len(xs) * q) if q > 0 else 1.0
