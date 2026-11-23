"""Private fleet statistics: each home's report is split into two secret shares (IETF DAP data flow).

The ISP wants "how many calls were poor on OLT 3 between 7 and 9 pm", not which home had a poor call.
The IETF Distributed Aggregation Protocol (draft-ietf-ppm-dap) gets there by having each client split its
measurement into shares sent to two aggregators run by different parties; neither learns anything from
its share alone, and only the sum over a batch of many clients is ever revealed.

This module does the client side and the aggregation arithmetic for histograms (additive secret sharing
modulo a prime). Each home reports, per telemetry interval, a histogram of its live apps' Quality of
Outcome by class (6 classes x 5 QoO bins). What is NOT here, and why it matters:

* Prio3's zero-knowledge validity proofs: without them a dishonest client can skew a total (it cannot
  learn anyone's data). Totals are therefore also checked for plausibility by the controller.
* Two independent operators: in the home kit both aggregator roles run inside the Operator Console, which
  demonstrates the data flow but gives no privacy against that console. In deployment the helper share goes
  to a separately operated helper (agent setting `helper_url`).
"""
from __future__ import annotations

import os

PRIME = 2 ** 61 - 1
CLASSES = ("voip", "video", "gaming", "streaming", "web", "bulk")
BINS = (20, 40, 60, 80, 101)            # QoO 0-19, 20-39, 40-59, 60-79, 80-100
MIN_BATCH = 5                            # never release a total over fewer homes than this


def qoo_histogram(apps: list) -> list[int]:
    h = [0] * (len(CLASSES) * len(BINS))
    for a in apps or []:
        q, c = a.get("qoo"), a.get("label")
        if q is None or c not in CLASSES:
            continue
        b = next(i for i, edge in enumerate(BINS) if q < edge)
        h[CLASSES.index(c) * len(BINS) + b] += 1
    return h


def split(vec: list[int], rnd=None) -> tuple[list[int], list[int]]:
    """Additive shares: leader + helper = vec (mod PRIME); each share alone is uniformly random."""
    rb = rnd or (lambda: int.from_bytes(os.urandom(8), "big") % PRIME)
    leader = [rb() for _ in vec]
    helper = [(v - r) % PRIME for v, r in zip(vec, leader)]
    return leader, helper


def add(acc: list[int] | None, share: list[int]) -> list[int]:
    if acc is None:
        return [x % PRIME for x in share]
    return [(a + b) % PRIME for a, b in zip(acc, share)]


def combine(leader_sum: list[int], helper_sum: list[int]) -> list[int]:
    return [(a + b) % PRIME for a, b in zip(leader_sum, helper_sum)]


def labelled(total: list[int]) -> dict:
    out = {}
    for ci, c in enumerate(CLASSES):
        row = total[ci * len(BINS):(ci + 1) * len(BINS)]
        if any(row):
            out[c] = {"0-19": row[0], "20-39": row[1], "40-59": row[2], "60-79": row[3], "80-100": row[4]}
    return out
