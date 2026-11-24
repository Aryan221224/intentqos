"""Module 4 -- Policy engine: decide the shaping plan, then pass it through a safety shield.

decide(): flows+labels, intents and the estimated capacity -> a plan
  { "down": {"rate": kbit, "guarantee": {tin: kbit}}, "up": {...} } and a
  flow -> tin map. Realtime demand is reserved with headroom, intents get their
  bargained grants in a dedicated top class, best-effort and bulk share the
  rest, and EVERY class keeps a floor (no starvation by construction).

Shield (safety envelope, after Alshiekh et al., AAAI 2018 "Safe RL via
shielding" -- here it wraps a deterministic controller and any future learned
one alike): the proposed plan is corrected if and only if it violates
  1. rate bounds [min, max] and per-tick step limits (decreases are always allowed
     faster than increases -- cutting rate is the safe direction),
  2. per-class floors and sum(guarantees) <= rate,
  3. the change budget (max tc changes per minute; hysteresis below 5 %).
Every correction is counted and logged.

Inputs : decisions, flows, active intents, estimator rates, last applied plan.
Outputs: Plan + flow->tin map + shield report.
Failure: any exception in decide() -> the daemon keeps the last-known-good plan.
Success: no class starves (> 0 service whenever backlogged), interactive classes
         keep their reservation under contention, changes stay rate-limited.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import LABEL_TO_TIN, TIN_BE, TIN_BULK, TIN_INTENT, TIN_NAMES, TIN_VIDEO, TIN_VOICE  # noqa: F401
from .features import recent_rates

REALTIME_LABELS = {"voip", "gaming", "video"}


@dataclass
class ShieldReport:
    interventions: list = field(default_factory=list)
    held: bool = False


def flow_tins(flows, decisions, intents) -> dict:
    """flow key -> tin, applying intent leases on top of classifier labels."""
    targets = {}
    for it in intents:
        targets[it.target] = it
    out = {}
    for f in flows:
        d = decisions.get(f.id())
        label = d.committed if d else None
        tin = LABEL_TO_TIN.get(label, TIN_BE)
        it = targets.get(f.dev)        # every address of the device, IPv6 included
        if it is not None:
            # voice-like flows already sit in the top-priority voice class; moving them into the intent
            # class would make them queue behind the same device's video bursts. Everything else the
            # lease covers (video, interactive web, or all flows for a throughput goal) goes to INTENT.
            if label in ("voip", "gaming") and it.goal != "throughput":
                tin = TIN_VOICE
            elif it.goal == "throughput" or label in REALTIME_LABELS or label == "web":
                tin = TIN_INTENT
        out[f.key] = tin
    return out


def demand_by_tin(flows, tins: dict) -> dict:
    dem = {"down": {t: 0.0 for t in TIN_NAMES}, "up": {t: 0.0 for t in TIN_NAMES}}
    for f in flows:
        t = tins.get(f.key, TIN_BE)
        up, dn = recent_rates(f)
        dem["up"][t] += up
        dem["down"][t] += dn
    return dem


def decide(cfg: dict, rates: dict, demand: dict, intents) -> dict:
    """Compute guaranteed rates per tin for both directions.

    Built so that sum(guarantees) == rate and every floor holds by construction:
    floors first, then intent grants, then measured realtime demand (capped),
    then the remainder split between best effort and bulk (2:1 by default, `remainder_bulk_share`)."""
    P = cfg["policy"]
    fl = P["floor_fraction"]
    plan = {}
    for d in ("down", "up"):
        R = rates[d]
        g = {TIN_INTENT: 8.0, TIN_VOICE: fl["voice"] * R, TIN_VIDEO: fl["video"] * R,
             TIN_BE: fl["besteffort"] * R, TIN_BULK: fl["bulk"] * R}
        avail = max(0.0, R - sum(g.values()))
        rt_cap = P["max_realtime_fraction"] * R
        grant = sum((it.granted_down_kbit if d == "down" else it.granted_up_kbit) for it in intents)
        extra = min(max(0.0, grant - 8.0), cfg["intents"]["max_boost_fraction"] * R, avail)
        g[TIN_INTENT] += extra
        avail -= extra
        if P["adaptive_shares"]:
            h = P["realtime_headroom"]
            want_v = max(0.0, demand[d][TIN_VOICE] * h - g[TIN_VOICE])
            want_vi = max(0.0, demand[d][TIN_VIDEO] * h - g[TIN_VIDEO])
        else:
            want_v = max(0.0, 0.10 * R - g[TIN_VOICE])
            want_vi = max(0.0, 0.30 * R - g[TIN_VIDEO])
        room = max(0.0, min(avail, rt_cap - (g[TIN_INTENT] + g[TIN_VOICE] + g[TIN_VIDEO])))
        want = want_v + want_vi
        if want > 0:
            k = min(1.0, room / want)
            g[TIN_VOICE] += want_v * k
            g[TIN_VIDEO] += want_vi * k
            avail -= want * k
        bs = min(0.9, max(0.1, float(P.get("remainder_bulk_share", 1.0 / 3.0))))
        g[TIN_BE] += avail * (1.0 - bs)
        g[TIN_BULK] += avail * bs
        plan[d] = {"rate": R, "guarantee": g}
    return plan


def shield(cfg: dict, plan: dict, last: dict | None, est_bounds: dict, changes_last_minute: int) -> ShieldReport:
    """Correct `plan` in place so that it satisfies the safety envelope."""
    P = cfg["policy"]
    fl = P["floor_fraction"]
    rep = ShieldReport()
    for d in ("down", "up"):
        p = plan[d]
        lo, hi = est_bounds[d]
        if p["rate"] < lo or p["rate"] > hi:
            rep.interventions.append(f"{d}: rate {p['rate']:.0f} clamped to [{lo:.0f},{hi:.0f}]")
            p["rate"] = min(hi, max(lo, p["rate"]))
        if last:
            prev = last[d]["rate"]
            up_lim = prev * (1 + P["max_rate_increase_per_tick"])
            dn_lim = prev * (1 - P["max_rate_decrease_per_tick"])
            if p["rate"] > up_lim:
                rep.interventions.append(f"{d}: increase limited to +{P['max_rate_increase_per_tick'] * 100:.0f}%")
                p["rate"] = up_lim
            elif p["rate"] < dn_lim:
                rep.interventions.append(f"{d}: decrease limited to -{P['max_rate_decrease_per_tick'] * 100:.0f}%")
                p["rate"] = dn_lim
        R = p["rate"]
        g = p["guarantee"]
        floors = {TIN_VOICE: fl["voice"], TIN_VIDEO: fl["video"], TIN_BE: fl["besteffort"], TIN_BULK: fl["bulk"]}
        for t, f in floors.items():
            if g[t] < f * R * 0.999:
                rep.interventions.append(f"{d}: {TIN_NAMES[t]} raised to its floor")
                g[t] = f * R
        tot = sum(g.values())
        if tot > R:
            # scale the part above the floors so the sum fits; floors are untouchable
            fsum = sum(f * R for f in floors.values()) + 8.0
            above = tot - fsum
            k = max(0.0, (R - fsum) / above) if above > 0 else 0.0
            for t in g:
                base = floors.get(t, 0.0) * R if t != TIN_INTENT else 8.0
                g[t] = base + (g[t] - base) * k
            rep.interventions.append(f"{d}: guarantees scaled to fit the rate")
    if last and changes_last_minute >= P["max_changes_per_minute"]:
        decreasing = any(plan[d]["rate"] < last[d]["rate"] * 0.98 for d in ("down", "up"))
        if not decreasing:
            rep.held = True
            rep.interventions.append("change budget exhausted: holding the current policy")
    return rep


def significant(plan: dict, last: dict | None, threshold: float) -> bool:
    """Hysteresis: apply decreases of >= 2 % at once (the safe direction), increases only
    once they add up to `threshold`, and guarantee moves only when they are material."""
    if last is None:
        return True
    for d in ("down", "up"):
        a, b = plan[d]["rate"], last[d]["rate"]
        if b <= 0 or (a < b and (b - a) / b >= 0.02) or (a > b and (a - b) / b >= threshold):
            return True
        for t, v in plan[d]["guarantee"].items():
            w = last[d]["guarantee"].get(t, 0.0)
            if abs(v - w) > max(2 * threshold * w, 0.03 * a):
                return True
    return False
