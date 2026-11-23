# What is new here, and what is borrowed

The brief asks for a technically credible reference implementation, not a reinvention.
So IntentQoS reuses proven open designs where they exist and puts its own work where the
prior art stops. This page separates the two honestly.

## Borrowed (credited, re-implemented, no code copied)

| Idea | Source | Where |
|---|---|---|
| Shape just below the bottleneck so the queue lives where you control it | bufferbloat project, CAKE/SQM | `enforce.py` |
| Latency-driven, load-gated shaper-rate control with ICMP-timestamp one-way delay | cake-autorate (design) | `estimator.py` |
| qosify's packet-length and pps thresholds as the rule baseline | OpenWrt qosify (defaults) | `classifier.RuleClassifier` |
| Shielding a controller with a safety envelope | Alshiekh et al., AAAI 2018 | `policy.shield` |
| Time-bounded, budgeted resource requests | PANE, SIGCOMM 2013 | `intents.py` |
| Nash bargaining for competing intents | Cinmere et al., EW 2023 | `intents.water_fill` |
| Jain's index, demand-aware fairness | Jain 1984; Ware et al. 2019 | `metrics.py` |
| Minimum-delay ("standing queue") test | CoDel (Nichols & Jacobson) | `estimator._bloated` |
| Passive RTT from TCP timestamps, split at the observer | ePPing / pping (Kathie Nichols; Simon Sundberg et al.) | `router/iq-capture.c`, `capture.py` |
| QUIC spin bit for passive RTT | RFC 9000 §17.4 | `iq-capture.c`, `experience.py` |
| Quality of Outcome: per-requirement interpolation between "works perfectly" and "unusable" | IETF draft-ietf-ippm-qoo | `qoo.py`, Test Kit |
| ΔQ: delay as distributions, composed; G/S/V split | ΔQSD (Broadband Forum TR-452, PNSol) | `deltaq.py`, controller `localise` |
| "Is the Wi-Fi or the line the bottleneck?" from packet-gap regularity and LAN RTT | Sundaresan et al., PAM 2016 | `wifi.py` |
| DSCP → Wi-Fi access category map | RFC 8325 | `wifi.qos_map_set`, installer |
| L4S lane (ECT(1) marked at a shallow threshold) | RFC 9330-9332; DualPI2 | `enforce.py` |
| Safe exploration of a tuning parameter | SafeOpt (Sui et al., ICML 2015) | `outcome.SafeTuner` |
| Adaptive conformal inference | Gibbs & Candès, NeurIPS 2021 | `classifier.AdaptiveConformal` |
| Per-phase classifiers by flow age | NetBeacon (Zhou et al., USENIX Security 2023) | `ml/train.py`, `treemodel.py` |
| Quality-on-Demand API shapes | CAMARA QoD v1 / QoS Profiles (Linux Foundation, GSMA Open Gateway) | `camara.py`, `controller/camara_nb.py` |
| Two-aggregator secret-shared statistics | IETF DAP (draft-ietf-ppm-dap), Prio | `privagg.py`, `controller/privagg.py` |
| Signed, expiring, versioned instructions; k-of-n release keys | The Update Framework (TUF) | `release.py` |
| Always-valid sequential test for rollouts | mSPRT (Johari, Pekelis & Walsh) | `controller/seqtest.py` |
| Responsiveness under working conditions (RPM) | IETF draft-ietf-ippm-responsiveness-09 | Test Kit |
| Load-adjusted IP capacity measurement | RFC 9097 (method only) | Test Kit, console lab endpoints |
| TR-181 vendor data model for USP | Broadband Forum TR-181 / TR-369 | `usp.py` |

## Our contributions

1. **Abstaining, calibrated, correctable classifier tied to scheduling.** Header-only
   features, isotonic calibration, and split-conformal prediction sets: the model acts only
   when its set holds a single class (90 % coverage), otherwise the deterministic rules act.
   A one-click correction applies at once and becomes training data; Page-Hinkley drift
   alarms on the abstention stream. Evaluated at the level that matters, the *scheduling
   class*, and on time-to-correct-class per flow, not just F1.
2. **A delay-driven estimator that does not over-cut.** After a cut, further cuts wait for
   the measured queue to drain (`delay / decrease_factor`), and probing slows near the last
   bloat point (a CUBIC-like plateau), with a CoDel-style minimum test so transient jitter
   never triggers a cut. This targets the brief's 100 → 20 Mbit/s scenario directly.
3. **Policy whose safety is by construction.** Floors first, then intent grants, then
   realtime reservations; the shield only has to catch genuine violations, and every
   intervention is counted and shown. A starvation detector rolls back, and an independent
   watchdog restores a safe state when the engine dies (measured in scenario S5).
4. **Intents as leases with assurance.** Expiry, daily budgets, admission control,
   bargaining, a dry-run preview of who gives up share, and a closed-loop "is it working"
   status, instead of a priority toggle.
5. **Evidence by construction.** Seeded, reproducible scenarios for all three brief cases plus
   intent and fault injection; four arms (no QoS, static QoS, adaptive with rules, adaptive
   with ML); medians, IQRs, bootstrap CIs and Mann-Whitney tests; one command.


## IntentQoS 2.0: ten additions, and how far each one goes

Every item below is in the code with tests. "Emulation" means measured on this repository's namespace
testbed (veth pairs, a Linux bridge, emulated access links) on an x86-64 Linux 6.18 machine, not on router
hardware. Where a part needs hardware or a third party that we did not have, it says so.

| # | Addition | Code | Status | What was measured |
|---|---|---|---|---|
| 1 | **Experience-driven control loop.** The policy's two free parameters (headroom for real-time lanes, the bulk share of what is left) are chosen by predicted Quality of Outcome of the live apps, and a plan that would push a call, game or boosted device below QoO 60 by more than 10 points is refused (rate cuts are never refused). Optional SafeOpt-style tuning of the bloat threshold. | `outcome.py`, `daemon._tick` | built, emulation | no significant effect in two scenarios (below) |
| 2 | **Experience sensing from headers.** ECN codepoints, the highest DSCP a device sends, TCP retransmissions, passive RTT from TCP timestamps split at the router into internet and in-home parts, QUIC spin-bit RTT, video-frame bursts. Per-app QoO and ΔQ. | `iq-capture.c`, `capture.py`, `experience.py`, `qoo.py`, `deltaq.py` | built, emulation | router-side RTT 44.7 ms vs the server's own `ss` 45.3 ms; QUIC spin RTT 51.3 ms for a 50 ms spin period |
| 3 | **Kernel flow engine (eBPF).** The same per-flow aggregates computed in the kernel at tc, and the engine's class decision (mark, priority, DSCP) applied in the same pass, before netfilter, so it also works with flow offload. On a LAN bridge it marks before the bridge's shaper and accounts on the member ports after it. | `router/iq-flow.bpf.c`, `iq-capture -e`, `capture.py` | built, emulation; opt-in (`capture.backend = ebpf`) | see "Kernel flow engine" below |
| 4 | **Whole-home bottleneck locator.** Per device: Wi-Fi, the line, the far end, or not enough traffic, from packet-gap regularity and the in-home round trip; RFC 8325 QoS Map Set installed on the access point. | `wifi.py`, installer | built, emulation for the method; QoS map needs an OpenWrt AP to take effect | unit tests; live verdicts in the lab |
| 5 | **L4S lane.** ECT(1) traffic gets CE marks at a 1 ms threshold (fq_codel `ce_threshold_selector`), DualPI2 where the kernel has it (6.17+), `cake_mq` on multi-queue NICs (Linux 7.0). | `enforce.py` | commands built and unit-tested; the test kernel lacks fq_codel, cake and dualpi2, so the lane reports "off" there | not measured |
| 6 | **Drift-proof classifier.** Phase-wise forests by flow age (kept per phase only if they beat the single forest on calibration data), one-sided adaptive conformal inference, weak labels from Microsoft 365's published Optimize endpoints, and a per-session vote. | `ml/train.py`, `treemodel.py`, `classifier.py`, `weaklabels.py` | built, emulation | see "Classifier under drift" below |
| 7 | **Standards-grade proof in the Test Kit.** QoO per app, ΔQ split, jitter, percentile summary next to TRAI's 2024 wireline reference values, RPM per draft-ietf-ippm-responsiveness-09, home-network capacity by the RFC 9097 method. | `kit/testkit*.go`, `kit/lab.go` | built; loopback tests | RPM and capacity run end to end on loopback; real-line runs still to do |
| 8 | **Intents that reach the ISP, and plain-language and proactive requests.** CAMARA QoD v1 on the router and at the operator (finds the home by phone number, relays to the gateway, reserves the access network), proactive boost offers, a rule-based plain-language parser (English and some Hindi) that only drafts a lease for confirmation. | `camara.py`, `controller/camara_nb.py`, `offers.py`, `nlintent.py` | built, emulation; **the access-network reservation is simulated** | a CAMARA session created at the controller reached the lab gateway's engine as a lease and returned `AVAILABLE`, with the access segment `RESERVED (simulated)` |
| 9 | **Private, safe fleet learning.** Per-home QoO histograms as two secret shares (released only for batches of 5+ homes, totals checked for plausibility), signed, expiring, gateway-bound instructions with k-of-n model signatures, always-valid sequential rollout tests, a TR-181 vendor data model, ΔQ fault localisation (home Wi-Fi / access line / OLT / upstream). | `privagg.py`, `controller/privagg.py`, `release.py`, `controller/seqtest.py`, `usp.py`, `controller/core.localise` | built, emulation | 241 gateways (240 simulated + the lab gateway) sent shares; per-OLT totals released; harmless rollouts checked 30 times each: the sequential guard falsely flagged 4 of 60, the previous fixed-sample test (Mann-Whitney at every look) 21 of 60 |
| 10 | **Hardware-assisted scale.** `cake_mq`, bulk-only flow offload with marks set before netfilter (by the kernel engine). | `enforce.py`, `iq-flow.bpf.c` | research: commands built; not tested on offload hardware | not measured |

What is honestly not here: Prio3 validity proofs (a dishonest gateway can skew a private total, though it
cannot learn anyone's data); two independently operated aggregators in the home kit (both roles run on the
Operator Console there, and the page says so); registration of the TR-181 model with a USP agent (the model
and its read-out exist); an interoperable RFC 9946 (UDPST) implementation (the capacity test follows the
RFC 9097 method with our own messages); a real OLT/BNG adapter for CAMARA (the adapter is a simulated ledger
with the interface a RADIUS CoA or NETCONF adapter would implement).

### Outcome control ablation (emulation)

The same engine with the outcome controller on (default) and off (`outcome.enabled = false`), 4 seeds each,
measured at the endpoints by the traffic generators, on the testbed's emulated 100/20 Mbit/s line:

| Scenario | Measure | on (median) | off (median) | Mann-Whitney p |
|---|---|---|---|---|
| ISO download during a video call | call audio delay p99 | 30.1 ms | 35.5 ms | 0.25 |
| | call video delay p99 | 30.4 ms | 35.9 ms | 0.25 |
| | call MOS | 4.36 | 4.36 | – |
| | ISO download / backup goodput | 78.8 / 14.0 Mbit/s | 78.1 / 14.0 Mbit/s | 0.77 / 0.56 |
| TVs and a game | game delay p99 | 44.5 ms | 44.7 ms | 0.77 |
| | game jitter | 1.4 ms | 0.5 ms | 0.39 |
| | TV bitrate, rebuffering | 14.4 Mbit/s, 0 s | 13.8 Mbit/s, 0 s | 0.56 |

No difference here is statistically significant at 4 seeds, and none costs throughput. In these two scenarios
the classic policy already keeps the call and the game in their lanes, so the outcome controller has little to
add; its case rests on situations its parameters change (several real-time apps competing for the reserved
headroom, a line near its limit), which these runs do not show. Raw results: `results-ablation/`, script
`testbed/ablation_outcome.py`.

### Kernel flow engine (emulation)

Both capture modes ran at once on the same LAN bridge of a three-namespace network (device, router, server)
with a download, an upload and QUIC-style UDP with a spin bit:

| | packet ring (`iq-capture`) | kernel engine (`iq-capture -e`) |
|---|---|---|
| download flow: internet RTT (TCP timestamps) | 12.09 ms | 12.08 ms |
| download flow: in-home RTT | 0.07 ms | 0.06 ms |
| upload flow: internet / in-home RTT | 12.74 / 0.26 ms | 12.73 / 0.25 ms |
| QUIC spin-bit RTT (50 ms spin period) | 51.28 ms | 51.28 ms |
| packets counted (≈6 s) | 8,567 | 8,611 |

Verdicts: after the engine sent "voice class" for the two TCP flows, 24.7 MB of the download went to the
voice class of the bridge's shaper (1.0 MB stayed in the default class during the switch-over), every upload
byte after the verdict went to the voice class on the WAN shaper, and the device received the download with
DSCP 46. Cost, x86-64, kernel BPF statistics: **176 ns per packet in the BPF program** (237 ns before the
counters stopped using atomic adds); the packet-ring helper spent 93 ns of user CPU per packet, and its
kernel-side copy into the ring is not in that number; the kernel-mode helper's own CPU was below the
measurement resolution. A first version stamped download packets before the shaper, so the in-home round
trip absorbed the router's own queue (4.36 ms against 0.08 ms); attaching the accounting to the bridge's
member ports fixed it, and on a non-bridged LAN the engine says that its in-home round trips include that
queue. Verified on Linux 6.18 only; the programs are written for 5.3+ (bounded loops) and need tc with
libbpf.

### Classifier under drift (emulation)

Trained on dataset A (24 sessions), then replayed in time order on dataset B, generated later with another
seed (12 sessions, 6,917 rows), and on B with a synthetic shift (packet sizes ×1.3, gaps ×1.5, rates to match).

| | accuracy, phase-wise / single | first 2 s, phase-wise / single | coverage (target 0.90), fixed / adaptive | abstentions, fixed / adaptive |
|---|---|---|---|---|
| A, held-out sessions | macro F1 0.967 / 0.967 | 0.923 / 0.878 (246 rows) | 0.962 / – | 1.8 % / – |
| B, new sessions | 0.970 / 0.964 | 0.949 / 0.928 (432 rows) | 0.963 / 0.969 | 1.8 % / 1.5 % |
| B, synthetic shift | 0.782 / 0.757 | 0.701 / 0.708 | 0.719 / 0.835 | 12.6 % / 12.9 % |
| B, shift, feedback on 10 % of decisions | same | same | 0.719 / 0.807 | 12.6 % / 12.2 % |

Plain (two-sided) ACI brought coverage on B down from 0.96 to exactly 0.90 and raised abstentions from 1.8 %
to 8.3 %; on a gateway an abstention means a rules fallback, so the shipped ACI only widens sets. Under the
synthetic shift ACI recovers about half of the coverage gap; it does not reach the target. Feedback came from
ground-truth labels here; on a gateway it comes from household corrections, weak labels and flows whose label
held to the end, which is sparser. Source: `ml/eval_drift.py`, `docs/drift_eval.json`.

## Deliberately not done

- No reinforcement learning in the control path: it would be hard to bound and to reproduce. The outcome
  controller picks from a small discrete grid by a model prediction, and the only exploration (SafeTuner) is
  opt-in, bounded to a safe set and shielded.
- No language model in the control path: plain-language requests are parsed by rules on the router into the
  same typed lease, shown for confirmation, and checked by the same schema, budget and admission control. A
  model can be plugged in as a drafter only behind that schema check.
- No TLS SNI or DNS-based classification: stays well inside the no-payload rule.
