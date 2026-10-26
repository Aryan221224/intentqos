# What the evidence shows (and what it does not)

Source: `results/REPORT.md`. There are 160 seeded runs: 10 seeds × 5 scenarios × each scenario's arms.
The numbers are medians, with interquartile ranges and Mann-Whitney p-values in the report. All
network conditions are emulated. This page reads the numbers plainly, including the ones that
do not favour IntentQoS.

## Where IntentQoS clearly helps

| Finding | Evidence (median) |
|---|---|
| **The link drop is handled.** Once the link has dropped from 100 to 20 Mbit/s and settled, call delay p99 is half that of static QoS and a fifth of no QoS. Recovery takes about 5 s instead of about 15 s, and call quality stays good. | S2: p99 47 ms vs 93 (static, p = 0.003) vs 219 (none, p < 0.001). Recovery 4.9 s vs 15.4 s (p < 0.001). MOS 3.90 vs 2.76 / 2.24 |
| **Upload bufferbloat is removed** while a cloud backup runs. | S1: call audio p99 upload 17 ms vs 113 ms with no QoS (p < 0.001). All shaped arms are similar here. |
| **Browsing stays fast during a big download** when the ML classifier is used. | S1: web page load p95 76 ms vs 135–142 ms for every other arm (p < 0.001) |
| **Recognising adaptive streaming pays off.** The rule baseline lumps TV streams in with downloads. The ML labels them as streaming and serves them above bulk. | S2: TV rebuffering 3.1 s vs 11–18 s (p ≤ 0.006). S3: mean TV bitrate 12.4 vs 7.4 Mbit/s with rules (+67 %, p = 0.001) |
| **The lease does what it promises.** The chosen work call's video loss falls from 13.6 % to 0 %. | S4 (p < 0.001) |
| **It fails safe.** After `kill -9` of the engine, the watchdog installed the safe state in 1.8 s. The call stayed under 30 ms p99 and the download never stalled. With reflectors black-holed, blind mode held the rates. | S5, all 10 runs |
| **Bulk keeps making progress** in S1–S3: zero seconds with no progress in every arm and run. | S1, S3 |

## Where it does not help, or costs something

| Finding | Evidence | Why, and what we would do next |
|---|---|---|
| **Prioritising streaming takes bandwidth from bulk.** | S3 update goodput: 25 Mbit/s (ML) vs 54–60. S2 download after the drop: 3.9 vs 9–13 Mbit/s | This is the default ordering (streaming above bulk), and it is now an operator setting: `policy.remainder_bulk_share`, pushed per home in the signed *bulk-friendly* profile. Measured in S3 with that profile (10 more seeds): update **37.6 Mbit/s** (+51 %), TV bitrate **10.4 Mbit/s** (still +41 % over rules, no rebuffering), game download p99 **40.5 ms** (better than the default's 45.4). The cost moves to upload: game upload p99 18.8 ms vs 11.8 ms, because bulk's larger floor also applies upstream. |
| **ML is worse than rules for game delay in S3.** | Game p99 download: 45 ms (ML) vs 27 ms (rules). Both beat static (60) and none (147). | The TVs now sit in a higher class and their bursty downloads overshoot the bottleneck upstream of the gateway. Download shaping happens after the bottleneck, so it cannot stop every overshoot. CAKE's `ingress` mode on a stock kernel is the expected fix; it was not available in our test kernel. |
| **ML is not better than rules on upload call delay in S1 and S2.** | S1 upload p99 17 ms vs 15. S2 upload after the drop 325 ms vs 120 ms (high variance, p = 0.27) | Not significant in S1. The S2 upload gap is not yet diagnosed; we report it rather than tune it away. |
| **The first seconds after a link drop are still bad for everyone.** | S2 peak after the drop: 364 ms (ML) vs 322–450 ms | The modem's buffer fills before any controller can see it. The estimator reacts in under a second, but the queue that is already in the modem has to drain. |
| **The lease costs the other call and the backup.** | S4: the other call's video p99 rises from 37 to 166 ms. Backup goodput falls from 0.67 to 0.13 Mbit/s, with about 1 s of no progress. Work-call audio p99 rises from 13 to 33 ms, though MOS stays 4.36. | On a 3 Mbit/s uplink two video calls cannot both fit, and the user chose which one wins. The backup keeps its 5 % floor on average, but TCP stalls in short gaps. Next steps: a minimum absolute floor for bulk, and smaller HTB bursts at low rates. |
| **Download-direction call delay at the full 100 Mbit/s barely improves on no QoS.** | S1 download p99: 24 ms (ML) vs 33 ms (none, p = 0.52) vs 48 ms (static) | Our emulated modem builds little download queue at 100 Mbit/s, because the receivers' TCP windows limit it. The real benefit shows in S2. |
| **More jitter, still very small** | S1 jitter 0.5 ms vs 0.15–0.27 ms | It comes from more frequent policy changes. It is far below anything audible, and MOS is identical. |

## Classifier (held-out sessions, `docs/CLASSIFIER_REPORT.md`)

- Scheduling-class accuracy: hybrid (ML when confident, rules otherwise) 0.987 vs the rule baseline 0.703.
- Bulk flows reach the right class in a median of 0.46 s, instead of 6.3 s with the rules' 5-second bulk timer.
  Streaming flows are in the right class 99 % of their lifetime, vs 40 % with rules.
- Conformal coverage is 0.945 for a 0.90 target; the abstain rate is 4.4 %.
- Caveat: training and testing both use emulated traffic. Expect lower accuracy on real homes.
