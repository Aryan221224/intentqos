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

## Deliberately not done

- No reinforcement learning in the control path: it would be hard to bound and to reproduce.
  The shield and the discrete plan structure leave room for a contextual bandit later.
- No LLM / chat interface: the brief discounts conversational add-ons; the intent API is typed.
- No TLS SNI or DNS-based classification: stays well inside the no-payload rule.
