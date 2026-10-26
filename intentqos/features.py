"""Header-only flow features shared by the runtime classifier and the training pipeline.

Every feature is derived from packet sizes, packet timing, direction and the
transport protocol number. Port numbers and addresses are deliberately NOT
features (apps use random ports; this also keeps the model from memorising
the testbed). No payload byte is ever used.
"""
from __future__ import annotations

import math

from .capture import WINDOW_S, Flow, Window

FEATURE_NAMES = [
    "is_udp", "age_s",
    "pps_up", "pps_dn", "kbps_up", "kbps_dn",
    "mean_up", "mean_dn", "max_up", "max_dn", "std_up", "std_dn",
    "iat_ms_up", "iat_cv_up", "iat_ms_dn", "iat_cv_dn",
    "dn_up_ratio", "small_frac", "large_frac",
    "idle_frac_dn", "burst_cv_dn", "active_frac", "log_bytes",
    "early_mean", "early_max", "early_up_frac", "early_iat_ms",
]

RECENT_WINDOWS = 4   # features over the last 2 s


def _agg(ws: list[Window]):
    pk_up = sum(w.pk_up for w in ws)
    pk_dn = sum(w.pk_dn for w in ws)
    by_up = sum(w.by_up for w in ws)
    by_dn = sum(w.by_dn for w in ws)
    sq_up = sum(w.sq_up for w in ws)
    sq_dn = sum(w.sq_dn for w in ws)
    mx_up = max((w.mx_up for w in ws), default=0)
    mx_dn = max((w.mx_dn for w in ws), default=0)
    small = sum(w.small for w in ws)
    large = sum(w.large for w in ws)
    inu = sum(w.iat_n_up for w in ws)
    isu = sum(w.iat_s_up for w in ws)
    iqu = sum(w.iat_q_up for w in ws)
    ind = sum(w.iat_n_dn for w in ws)
    isd = sum(w.iat_s_dn for w in ws)
    iqd = sum(w.iat_q_dn for w in ws)
    return pk_up, pk_dn, by_up, by_dn, sq_up, sq_dn, mx_up, mx_dn, small, large, inu, isu, iqu, ind, isd, iqd


def _mean_std(n, s, q):
    if n <= 0:
        return 0.0, 0.0
    m = s / n
    v = max(0.0, q / n - m * m)
    return m, math.sqrt(v)


def flow_features(f: Flow, now: float) -> list[float]:
    """Compute FEATURE_NAMES for one flow from its closed windows (call after roll_windows)."""
    hist = list(f.hist)
    recent = hist[-RECENT_WINDOWS:] if hist else []
    span = max(WINDOW_S, len(recent) * WINDOW_S)
    pk_up, pk_dn, by_up, by_dn, sq_up, sq_dn, mx_up, mx_dn, small, large, inu, isu, iqu, ind, isd, iqd = _agg(recent)
    mean_up, std_up = _mean_std(pk_up, by_up, sq_up)
    mean_dn, std_dn = _mean_std(pk_dn, by_dn, sq_dn)
    iat_up, iat_sd_up = _mean_std(inu, isu, iqu)
    iat_dn, iat_sd_dn = _mean_std(ind, isd, iqd)
    tot = pk_up + pk_dn
    # ON/OFF structure over the long history (adaptive streaming = periodic bursts + idle gaps)
    bdn = [w.by_dn + w.by_up for w in hist]
    if len(bdn) >= 2:
        peak = max(bdn) or 1
        idle = sum(1 for b in bdn if b < 0.05 * peak) / len(bdn)
        m = sum(bdn) / len(bdn)
        sd = math.sqrt(sum((b - m) ** 2 for b in bdn) / len(bdn))
        burst_cv = sd / m if m > 0 else 0.0
        active = sum(1 for b in bdn if b > 0) / len(bdn)
    else:
        idle, burst_cv, active = 0.0, 0.0, 1.0
    e = f.early
    if e:
        sizes = [s for _, s, _ in e]
        e_mean = sum(sizes) / len(sizes)
        e_max = max(sizes)
        e_up = sum(d for _, _, d in e) / len(e)
        e_iat = ((e[-1][0] - e[0][0]) / (len(e) - 1) * 1000.0) if len(e) > 1 else 0.0
    else:
        e_mean = e_max = e_up = e_iat = 0.0
    return [
        1.0 if f.proto == 17 else 0.0,
        min(120.0, now - f.first),
        pk_up / span, pk_dn / span,
        by_up * 8 / span / 1000.0, by_dn * 8 / span / 1000.0,
        mean_up, mean_dn, float(mx_up), float(mx_dn), std_up, std_dn,
        iat_up * 1000.0, (iat_sd_up / iat_up) if iat_up > 0 else 0.0,
        iat_dn * 1000.0, (iat_sd_dn / iat_dn) if iat_dn > 0 else 0.0,
        math.log10((by_dn + 1.0) / (by_up + 1.0)),
        (small / tot) if tot else 0.0, (large / tot) if tot else 0.0,
        idle, min(10.0, burst_cv), active,
        math.log10(f.by_up + f.by_dn + 1.0),
        e_mean, float(e_max), e_up, min(5000.0, e_iat),
    ]


def recent_rates(f: Flow) -> tuple[float, float]:
    """(kbit/s up, kbit/s down) over the recent windows -- used for demand estimation."""
    recent = list(f.hist)[-RECENT_WINDOWS:]
    if not recent:
        return 0.0, 0.0
    span = len(recent) * WINDOW_S
    return (sum(w.by_up for w in recent) * 8 / span / 1000.0, sum(w.by_dn for w in recent) * 8 / span / 1000.0)


def recent_pps(f: Flow, windows: int) -> float:
    ws = list(f.hist)[-windows:]
    if not ws:
        return 0.0
    return sum(w.pk_up + w.pk_dn for w in ws) / (len(ws) * WINDOW_S)


def recent_avg_len(f: Flow, windows: int) -> float:
    ws = list(f.hist)[-windows:]
    pk = sum(w.pk_up + w.pk_dn for w in ws)
    by = sum(w.by_up + w.by_dn for w in ws)
    if pk == 0:
        tot = f.pk_up + f.pk_dn
        return (f.by_up + f.by_dn) / tot if tot else 0.0
    return by / pk
