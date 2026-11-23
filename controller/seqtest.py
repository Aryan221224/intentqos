"""Always-valid sequential tests for staged rollouts.

The rollout guard looks at canary and control homes again and again while a stage runs. A fixed-sample
test (Mann-Whitney) checked at every look loses its error guarantee: with enough looks a harmless update
eventually "fails" by chance, and a harmful one may be noticed late. The mixture sequential probability
ratio test (mSPRT; Johari, Pekelis and Walsh, "Always Valid Inference", 2015-2017) gives a p-value that
stays valid however often it is checked, so the guard can stop a bad stage at the first real evidence.

For the difference in means Z between canary and control, with plug-in variance V of Z and a normal
mixing distribution of variance tau^2 over the effect:

    Lambda_n = sqrt(V / (V + tau^2)) * exp( tau^2 Z^2 / (2 V (V + tau^2)) ),   p_n = min(p_{n-1}, 1 / Lambda_n)

tau is set to the size of effect the operator cares about (the guardrail's millisecond threshold).
"""
from __future__ import annotations

import math


def _mean_var(v: list[float]) -> tuple[float, float]:
    n = len(v)
    m = sum(v) / n
    var = sum((x - m) ** 2 for x in v) / (n - 1) if n > 1 else 0.0
    return m, var


def msprt_p(canary: list[float], control: list[float], tau: float) -> float:
    """One look's 1/Lambda (two-sided, effect = mean(canary) - mean(control)); 1.0 when there is too little data."""
    a = [x for x in canary if x is not None]
    b = [x for x in control if x is not None]
    if len(a) < 3 or len(b) < 3 or tau <= 0:
        return 1.0
    ma, va = _mean_var(a)
    mb, vb = _mean_var(b)
    V = max(1e-9, va / len(a) + vb / len(b))
    z = ma - mb
    t2 = tau * tau
    log_lam = 0.5 * math.log(V / (V + t2)) + t2 * z * z / (2.0 * V * (V + t2))
    return 1.0 if log_lam <= 0 else max(0.0, min(1.0, math.exp(-log_lam)))


class SequentialGuard:
    """Keeps the running minimum p-value per (rollout, stage): that minimum is the always-valid p."""

    def __init__(self):
        self.p: dict[tuple, float] = {}
        self.looks: dict[tuple, int] = {}

    def update(self, key: tuple, canary: list[float], control: list[float], tau: float) -> float:
        p = min(self.p.get(key, 1.0), msprt_p(canary, control, tau))
        self.p[key] = p
        self.looks[key] = self.looks.get(key, 0) + 1
        return p
