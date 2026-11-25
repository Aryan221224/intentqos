"""Quality Attenuation (ΔQ) for the home: delay distributions that add up along the path.

ΔQ (Broadband Forum TR-452) describes the quality a network takes away as a distribution of delay
plus a probability of loss. Its useful property is composition: the ΔQ of a path is the convolution
of the ΔQs of its parts. For a home that gives three parts the router can see separately:

    ΔQ(app) = ΔQ(LAN: Wi-Fi or cable) ⊕ ΔQ(our queue for the app's tin) ⊕ ΔQ(internet path)

and each can be split further into G (fixed: distance), S (size: serialisation) and V (variable:
queueing). That split is what tells "home, our shaper or the ISP?" apart, and composition is what
lets the controller predict an app's latency for a plan it has not applied yet (outcome.py).

Distributions are kept as bounded reservoirs of samples in milliseconds. Composition is done by
Monte-Carlo summation with a fixed seed (repeatable, cheap: a few hundred additions), not exact
convolution: the router runs pure Python.
"""
from __future__ import annotations

import math
import random

MC_SAMPLES = 400


class Dist:
    """An empirical delay distribution (ms) with a loss probability."""
    __slots__ = ("samples", "loss", "cap")

    def __init__(self, samples=None, loss: float = 0.0, cap: int = 256):
        self.cap = cap
        self.samples: list[float] = list(samples or [])[-cap:]
        self.loss = max(0.0, min(1.0, loss))

    def add(self, x: float, n: int = 1) -> None:
        for _ in range(max(1, n)):
            self.samples.append(float(x))
        if len(self.samples) > self.cap:
            del self.samples[:len(self.samples) - self.cap]

    def __len__(self) -> int:
        return len(self.samples)

    def pct(self, p: float) -> float | None:
        if not self.samples:
            return None
        s = sorted(self.samples)
        k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
        return s[k]

    def percentiles(self, ps=(50, 90, 99)) -> dict[int, float]:
        if not self.samples:
            return {}
        s, m = sorted(self.samples), len(self.samples) - 1          # one sort for all of them
        return {p: s[min(m, max(0, int(round(p / 100.0 * m))))] for p in ps}

    def gsv(self, size_ms: float = 0.0) -> dict:
        """G = the floor (minimum, less the size part), S = serialisation, V = what queueing adds above the floor."""
        if not self.samples:
            return {"g": None, "s": round(size_ms, 3), "v_p50": None, "v_p99": None, "loss": round(self.loss, 4)}
        lo = min(self.samples)
        return {"g": round(max(0.0, lo - size_ms), 2), "s": round(size_ms, 3),
                "v_p50": round(self.pct(50) - lo, 2), "v_p99": round(self.pct(99) - lo, 2), "loss": round(self.loss, 4)}

    @staticmethod
    def point(x: float, loss: float = 0.0) -> "Dist":
        return Dist([x], loss)


def compose(*parts: "Dist | None", n: int = MC_SAMPLES, seed: int = 7) -> Dist:
    """ΔQ of a path = sum of independent parts (convolution), losses combine as 1 - prod(1 - l)."""
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return Dist()
    rnd = random.Random(seed)
    # one batch draw per part, summed column-wise: ~5x faster than a choice() per sample per part (CPU profile)
    out = list(map(sum, zip(*[rnd.choices(p.samples, k=n) for p in parts])))
    keep = 1.0
    for p in parts:
        keep *= 1.0 - p.loss
    return Dist(out, 1.0 - keep, cap=n)


def queue_model(demand_kbit: float, service_kbit: float, pkt_bytes: float = 1000.0, aqm_target_ms: float = 5.0,
                n: int = 64) -> Dist:
    """Predicted queueing delay of one tin: M/D/1 waiting time below saturation, the AQM's standing-queue
    target (and drops) above it. A model, calibrated against measured tin delays in outcome.py."""
    if service_kbit <= 0:
        return Dist([1000.0] * n, 1.0)
    svc_ms = pkt_bytes * 8.0 / service_kbit             # one packet's serialisation at the service rate
    rho = demand_kbit / service_kbit
    if rho < 0.98:
        mean_wait = svc_ms * rho / (2.0 * (1.0 - rho))  # M/D/1 mean waiting time
        # exponential-ish tail around that mean: deterministic quantiles, no randomness
        qs = [(k + 0.5) / n for k in range(n)]
        return Dist([svc_ms + (-mean_wait * math.log(1.0 - q) if mean_wait > 0 else 0.0) for q in qs], 0.0, cap=n)
    loss = max(0.0, 1.0 - 1.0 / rho) if rho > 1.0 else 0.0
    qs = [(k + 0.5) / n for k in range(n)]
    return Dist([svc_ms + aqm_target_ms * (0.5 + q) for q in qs], loss, cap=n)
