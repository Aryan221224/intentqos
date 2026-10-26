"""Scripted, seeded experiment scenarios (the brief's three + intent + fault injection).

Each scenario: reset environment -> start the arm (no QoS / static / adaptive) ->
inject the condition on a timeline -> collect generator records -> compute metrics.
All times are seconds relative to t0. Every random choice comes from the seed.
"""
from __future__ import annotations

import os
import random
import time

from . import analysis as A
from . import profile as PR
from . import testbed as TB
from .runner import Run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "testbed.toml")

ARMS = {
    # no engine at all: the ISP modem's bloated buffer is the only queue
    "noqos": None,
    # classic router QoS: fixed shaper at 95 % of the plan rate, fixed class shares, rule classifier
    "static": ["estimator.enabled=false", "classifier.mode=rules", "policy.adaptive_shares=false"],
    # IntentQoS with the deterministic classifier
    "adaptive_rules": ["classifier.mode=rules"],
    # IntentQoS with the calibrated ML classifier (conformal abstention -> rules)
    "adaptive_ml": ["classifier.mode=ml"],
    # the same, with the operator's "bulk-friendly" profile: bulk keeps 12 % and half of the remainder
    "adaptive_ml_bulk": ["classifier.mode=ml", "policy.floor_fraction.bulk=0.12", "policy.remainder_bulk_share=0.5"],
    "adaptive_ml_intent": ["classifier.mode=ml"],
}


def _failover_rates() -> tuple[float, float, int, int]:
    fo = PR.active()["failover_4g"]
    lim_d = int(fo["down_mbit"] * 1e6 / 8 * fo["buffer_ms"] / 1000)
    lim_u = int(fo["up_mbit"] * 1e6 / 8 * fo["buffer_ms"] / 1000)
    return fo["down_mbit"], fo["up_mbit"], lim_d, lim_u


def _prepare(run: Run, arm: str, down: float, up: float, sets_extra: list[str] | None = None,
             rnd: random.Random | None = None, on_backup_link: bool = False) -> None:
    P = PR.active()
    if P:
        # field-calibrated profile: the plan is the profile's; the thin-link scenario runs on the 4G backup link
        seed = rnd.randrange(1, 2**31) if rnd else 1
        if on_backup_link:
            down, up, ld, lu = _failover_rates()
            TB.set_isp_rate(down, up, limit_down=ld, limit_up=lu)
        else:
            down, up = P["plan"]["down_mbit"], P["plan"]["up_mbit"]
            TB.set_isp_rate(down, up)
        # the line's delays and loss are in place while the engine starts and learns its baseline ...
        PR.write_access(seed, failover=on_backup_link, epoch=0)
    else:
        TB.set_isp_rate(down, up)
    TB.reset_gateway()
    run.server()
    if ARMS.get(arm) is not None:
        run.start_engine(CONFIG, ARMS[arm] + [f"link.down_mbit={down}", f"link.up_mbit={up}"] + (sets_extra or []))
        run.wait_api(12)
        time.sleep(1.0)
    if P:
        # written last, just before the scenario clock starts, so the seeded Wi-Fi events and capacity dips
        # fall at the same offsets for every arm (common random numbers)
        # ... and the seeded event clock restarts just before the scenario clock (common random numbers)
        run.access = PR.write_access(seed, failover=on_backup_link, epoch=1)
        PR.write_capacity(seed, enabled=not on_backup_link)
        run.mark("profile", name=P["name"], **run.access)
        time.sleep(0.3)


# ---------------------------------------------------------------- S1
def s1_iso_during_call(run: Run, arm: str, rnd: random.Random) -> dict:
    """A large ISO download (and a cloud backup) start during a video conference."""
    D = 45.0
    _prepare(run, arm, 100, 20, rnd=rnd)
    vk, vdk = rnd.randint(1500, 2000), rnd.randint(2000, 2600)
    bulk_t = rnd.uniform(9.0, 11.0)
    conns = rnd.randint(4, 8)
    run.start_clock()
    run.flow(0.0, "voip", "call_audio", "laptop", D - 1)
    run.flow(0.2, "video", "call_video", "laptop", D - 1.2, kbps=vk, down_kbps=vdk)
    run.flow(0.5, "web", "browse", "phone", D - 2, seed=rnd.randint(0, 999))
    run.flow(bulk_t, "bulk", "iso_download", "desktop", D - bulk_t - 1, conns=conns, direction="down")
    run.flow(bulk_t + 0.3, "bulk", "cloud_backup", "nas", D - bulk_t - 1.3, conns=2, direction="up")
    time.sleep(D + 3.5)
    t0 = run.t0
    out = {"params": {"video_kbps": vk, "video_down_kbps": vdk, "bulk_t": bulk_t, "conns": conns}}
    a = A.realtime_records(run.outdir, "call_audio")
    v = A.realtime_records(run.outdir, "call_video")
    base = None
    if a:
        pre = A.realtime_metrics(a["down"], t0 + 3, t0 + bulk_t)
        base = pre["owd_p50"] if pre else None
        for d in ("down", "up"):
            if d in a:
                out[f"audio_{d}"] = A.realtime_metrics(a[d], t0 + bulk_t + 2, t0 + D - 1, mos=True)
                out[f"audio_{d}_onset"] = A.realtime_metrics(a[d], t0 + bulk_t, t0 + bulk_t + 6, mos=True)
                out[f"audio_{d}_pre"] = A.realtime_metrics(a[d], t0 + 3, t0 + bulk_t)
        out["ts_audio_down"] = A.realtime_timeseries(a["down"], t0)
        out["ts_audio_up"] = A.realtime_timeseries(a["up"], t0) if "up" in a else []
    if v:
        for d in ("down", "up"):
            if d in v:
                out[f"video_{d}"] = A.realtime_metrics(v[d], t0 + bulk_t + 2, t0 + D - 1)
    out["iso_download"] = A.bulk_metrics(A.bulk_series(run.outdir, "iso_download"), t0 + bulk_t + 2, t0 + D - 1)
    out["cloud_backup"] = A.bulk_metrics(A.bulk_series(run.outdir, "cloud_backup"), t0 + bulk_t + 2, t0 + D - 1)
    out["browse"] = A.web_metrics(run.outdir, "browse")
    out["base_owd_ms"] = base
    return out


# ---------------------------------------------------------------- S2
def s2_wan_drop(run: Run, arm: str, rnd: random.Random) -> dict:
    """WAN capacity drops from 100 to 20 Mbit/s (upload 20 -> 5) in the middle of a busy evening."""
    D = 50.0
    drop_t = 15.0 + rnd.uniform(-1, 1)
    _prepare(run, arm, 100, 20, rnd=rnd)
    run.start_clock()
    run.flow(0.0, "voip", "call_audio", "laptop", D - 1)
    run.flow(0.2, "video", "call_video", "laptop", D - 1.2, kbps=rnd.randint(1200, 1800),
             down_kbps=rnd.randint(1800, 2400))
    run.flow(0.4, "abr", "tv_stream", "tv1", D - 1.5, ladder="1.5,3,6,10,16", seed=rnd.randint(0, 999))
    run.flow(3.0, "bulk", "download", "desktop", D - 4, conns=rnd.randint(3, 6), direction="down")
    run.flow(3.2, "bulk", "backup", "nas", D - 4.2, conns=1, direction="up")

    def drop():
        if PR.active():                 # realistic cause of 100 -> 20 Mbit/s: failover to the 4G backup link
            d, u, ld, lu = _failover_rates()
            TB.set_isp_rate(d, u, limit_down=ld, limit_up=lu)
            PR.write_access(run.access["seed"], failover=True, epoch=1)
            PR.write_capacity(run.access["seed"], enabled=False)   # the evening FTTH dips do not apply to 4G
            run.mark("wan_drop", down=d, up=u, kind="4g_failover")
        else:
            TB.set_isp_rate(20, 5)
            run.mark("wan_drop", down=20, up=5)
    run.at(drop_t, drop)
    time.sleep(D + 3.5)
    t0 = run.t0
    out = {"params": {"drop_t": drop_t}}
    a = A.realtime_records(run.outdir, "call_audio")
    if a:
        pre = A.realtime_metrics(a["down"], t0 + 5, t0 + drop_t)
        base = pre["owd_p50"] if pre else None
        out["base_owd_ms"] = base
        for d in ("down", "up"):
            if d in a:
                out[f"audio_{d}_pre"] = pre if d == "down" else A.realtime_metrics(a[d], t0 + 5, t0 + drop_t)
                out[f"audio_{d}_post"] = A.realtime_metrics(a[d], t0 + drop_t, t0 + D - 1, mos=True)
                out[f"audio_{d}_settled"] = A.realtime_metrics(a[d], t0 + drop_t + 10, t0 + D - 1, mos=True)
        # recovery: first time after the drop from which 3 s of packets all stay under base + 30 ms
        tx, rx, _ = a["down"]
        owd = (rx - tx) * 1000.0
        thr = (base or 10.0) + 30.0
        m = tx >= t0 + drop_t
        txp, owp = tx[m], owd[m]
        rec_s = None
        for i in range(len(txp)):
            j = txp <= txp[i] + 3.0
            sel = (txp >= txp[i]) & j
            if sel.sum() > 10 and (owp[sel] < thr).all():
                rec_s = float(txp[i] - (t0 + drop_t))
                break
        out["recovery_s"] = rec_s if rec_s is not None else float(D - drop_t)
        pk = (tx >= t0 + drop_t) & (tx < t0 + drop_t + 15)
        out["peak_owd_after_drop_ms"] = float(owd[pk].max()) if pk.any() else None
        out["ts_audio_down"] = A.realtime_timeseries(a["down"], t0)
    out["download_post"] = A.bulk_metrics(A.bulk_series(run.outdir, "download"), t0 + drop_t + 5, t0 + D - 2)
    out["download_pre"] = A.bulk_metrics(A.bulk_series(run.outdir, "download"), t0 + 5, t0 + drop_t)
    out["backup_post"] = A.bulk_metrics(A.bulk_series(run.outdir, "backup"), t0 + drop_t, t0 + D - 2)
    out["tv_stream"] = A.abr_metrics(run.outdir, "tv_stream")
    return out


# ---------------------------------------------------------------- S3
def s3_tvs_and_gaming(run: Run, arm: str, rnd: random.Random) -> dict:
    """Three TVs stream adaptive video while a console games; a game update downloads in the background."""
    D = 60.0
    _prepare(run, arm, 100, 20, rnd=rnd)
    upd_t = rnd.uniform(10.0, 13.0)
    run.start_clock()
    run.flow(0.0, "gaming", "game", "console", D - 1)
    for i, tv in enumerate(("tv1", "tv2", "tv3")):
        run.flow(0.5 + 2 * i + rnd.uniform(0, 0.5), "abr", f"{tv}_stream", tv, D - 2 - 2 * i,
                 ladder="1.5,3,6,10,16", seed=rnd.randint(0, 999))
    run.flow(upd_t, "bulk", "game_update", "desktop", D - upd_t - 1, conns=rnd.randint(4, 8), direction="down")
    time.sleep(D + 3.5)
    t0 = run.t0
    out = {"params": {"update_t": upd_t}}
    g = A.realtime_records(run.outdir, "game")
    if g:
        for d in ("down", "up"):
            if d in g:
                out[f"game_{d}"] = A.realtime_metrics(g[d], t0 + upd_t + 3, t0 + D - 1)
        out["ts_game_down"] = A.realtime_timeseries(g["down"], t0)
    tvs = {}
    for tv in ("tv1", "tv2", "tv3"):
        tvs[tv] = A.abr_metrics(run.outdir, f"{tv}_stream", a_rel=upd_t + 3)
    out["tvs"] = tvs
    rates = [x["mean_bitrate_mbps"] for x in tvs.values() if x]
    out["tv_mean_bitrate_mbps"] = sum(rates) / len(rates) if rates else None
    out["tv_rebuffer_s_total"] = sum((x["rebuffer_s"] for x in tvs.values() if x), 0.0)
    out["tv_jain_bitrate"] = A.jain(rates)
    out["game_update"] = A.bulk_metrics(A.bulk_series(run.outdir, "game_update"), t0 + upd_t + 3, t0 + D - 1)
    return out


# ---------------------------------------------------------------- S4
def s4_intent(run: Run, arm: str, rnd: random.Random) -> dict:
    """Two video calls compete on a thin link; the user declares 'prioritise my work call' (laptop)."""
    D = 45.0
    _prepare(run, arm, 10, 3, rnd=rnd, on_backup_link=True)
    run.start_clock()
    for dev in ("laptop", "phone"):
        run.flow(0.0, "voip", f"{dev}_audio", dev, D - 1)
        run.flow(0.2, "video", f"{dev}_video", dev, D - 1.2, kbps=rnd.randint(1400, 1700),
                 down_kbps=rnd.randint(2000, 2300))
    run.flow(2.0, "bulk", "download", "desktop", D - 3, conns=4, direction="down")
    run.flow(2.2, "bulk", "backup", "nas", D - 3.2, conns=2, direction="up")
    res = {}
    if arm == "adaptive_ml_intent":
        def intent():
            r = run.api("POST", "/api/intents", {"target": "laptop", "duration_s": 600, "goal": "low_latency",
                                                  "priority": 3, "requester": "asha", "label": "work call",
                                                  "demand_down_kbit": 2600, "demand_up_kbit": 1900})
            res["intent_response"] = {k: r.get(k) for k in ("ok", "reason", "preview")}
            run.mark("intent", ok=r.get("ok"))
        run.at(8.0, intent)
    time.sleep(D + 3.5)
    t0 = run.t0
    out = {"intent": res.get("intent_response")}
    for dev in ("laptop", "phone"):
        a = A.realtime_records(run.outdir, f"{dev}_audio")
        v = A.realtime_records(run.outdir, f"{dev}_video")
        for d in ("down", "up"):
            if a and d in a:
                out[f"{dev}_audio_{d}"] = A.realtime_metrics(a[d], t0 + 12, t0 + D - 1, mos=True)
            if v and d in v:
                out[f"{dev}_video_{d}"] = A.realtime_metrics(v[d], t0 + 12, t0 + D - 1)
    out["download"] = A.bulk_metrics(A.bulk_series(run.outdir, "download"), t0 + 12, t0 + D - 2)
    out["backup"] = A.bulk_metrics(A.bulk_series(run.outdir, "backup"), t0 + 12, t0 + D - 2)
    return out


# ---------------------------------------------------------------- S5
def s5_faults(run: Run, arm: str, rnd: random.Random) -> dict:
    """Fault injection: SIGKILL the engine, restart it, then black-hole the delay reflectors."""
    D = 52.0
    _prepare(run, "adaptive_ml", 100, 20, rnd=rnd)
    run.start_watchdog(CONFIG)
    run.start_clock()
    run.flow(0.0, "voip", "call_audio", "laptop", D - 1)
    run.flow(0.2, "video", "call_video", "laptop", D - 1.2)
    run.flow(2.0, "bulk", "download", "desktop", D - 3, conns=5, direction="down")
    kill_t, restart_t, bh_t, bh_end = 12.0, 24.0, 36.0, 44.0
    run.at(kill_t, run.kill_engine)

    def restart():
        run.start_engine(CONFIG, ARMS["adaptive_ml"] + ["link.down_mbit=100", "link.up_mbit=20"], "timeseries2.jsonl")
    run.at(restart_t, restart)

    def blackhole(on):
        if on:
            TB.nsx(TB.NS_SRV, "nft add table inet bh")
            TB.nsx(TB.NS_SRV, "nft 'add chain inet bh in { type filter hook input priority 0 ; }'")
            TB.nsx(TB.NS_SRV, "nft add rule inet bh in meta l4proto icmp drop")
            run.mark("reflector_blackhole_on")
        else:
            TB.nsx(TB.NS_SRV, "nft delete table inet bh", check=False)
            run.mark("reflector_blackhole_off")
    run.at(bh_t, blackhole, True)
    run.at(bh_end, blackhole, False)
    time.sleep(D + 3.5)
    TB.nsx(TB.NS_SRV, "nft delete table inet bh", check=False)
    t0 = run.t0
    out = {"timeline": {"kill": kill_t, "restart": restart_t, "blackhole": [bh_t, bh_end]}}
    ev = []
    try:
        with open(os.path.join(run.outdir, "engine-data", "events.jsonl")) as f:
            ev = [__import__("json").loads(x) for x in f]
    except FileNotFoundError:
        pass
    wd = [e for e in ev if e["kind"] == "watchdog"]
    out["watchdog_events"] = [{"t_rel": round(e["t"] - t0, 3), "msg": e["msg"], "restore_ms": e.get("restore_ms")}
                              for e in wd]
    out["watchdog_reaction_s"] = round(wd[0]["t"] - (t0 + kill_t), 3) if wd else None
    blind = [e for e in ev if e["kind"] == "estimator" and "blind" in e["msg"]]
    out["blind_mode_events"] = [{"t_rel": round(e["t"] - t0, 3), "msg": e["msg"]} for e in blind]
    a = A.realtime_records(run.outdir, "call_audio")
    if a:
        for nm, (x, y) in {"before_kill": (4, kill_t), "engine_dead_safe_state": (kill_t, restart_t),
                           "after_restart": (restart_t + 3, bh_t), "reflectors_blackholed": (bh_t, bh_end),
                           "recovered": (bh_end + 1, D - 1)}.items():
            out[f"audio_down_{nm}"] = A.realtime_metrics(a["down"], t0 + x, t0 + y, mos=True)
        out["ts_audio_down"] = A.realtime_timeseries(a["down"], t0)
    s = A.bulk_series(run.outdir, "download")
    out["download_whole"] = A.bulk_metrics(s, t0 + 4, t0 + D - 2)
    out["download_engine_dead"] = A.bulk_metrics(s, t0 + kill_t, t0 + restart_t)
    return out


SCENARIOS = {
    "s1_iso_during_call": (s1_iso_during_call, ["noqos", "static", "adaptive_rules", "adaptive_ml"]),
    "s2_wan_drop": (s2_wan_drop, ["noqos", "static", "adaptive_rules", "adaptive_ml"]),
    "s3_tvs_and_gaming": (s3_tvs_and_gaming, ["noqos", "static", "adaptive_rules", "adaptive_ml", "adaptive_ml_bulk"]),
    "s4_intent": (s4_intent, ["adaptive_ml", "adaptive_ml_intent"]),
    "s5_faults": (s5_faults, ["adaptive_ml"]),
}
