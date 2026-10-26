# Module specifications

The brief asks for independently testable modules with defined inputs, outputs,
state transitions, failure handling and success conditions. Each module below is
one Python file under `intentqos/`, has unit tests in `tests/test_modules.py`, and
is exercised end-to-end by the scenarios in `testbed/scenarios.py`.

---

## 1. Capture & flow tracker — `capture.py`

| | |
|---|---|
| **Inputs** | LAN-side interface (`interfaces.lan`), LAN subnets |
| **Outputs** | `FlowTable` of bidirectional flows keyed `(lan_ip, remote_ip, proto, lan_port, remote_port)`: counters, first 16 packets (size, inter-arrival, direction), 0.5 s window summaries, 8 s history. Capture statistics, including kernel drops. |
| **State** | per flow: `new → active → idle (no packets > 10 s, excluded from policy) → expired (30 s idle, or FIN/RST + 3 s)` |
| **Privacy boundary** | `AF_PACKET` with a 96-byte snap length; only Ethernet/VLAN, IPv4/IPv6 (+ extension headers) and TCP/UDP header fields are parsed. A unit test changes payload bytes and checks the parse result is identical. Nothing is decrypted, stored or logged from payload. |
| **Failure handling** | socket errors → counted, thread reopens the socket with exponential back-off (0.5–5 s). Kernel drops are exposed on `/metrics` (`capture_kernel_drops_total`). While capture is down, the policy keeps the last labels and the shaper keeps running. |
| **Success condition** | per-flow byte counts match the generator's sent bytes; LAN↔LAN traffic is ignored; both directions merge into one flow (unit tests `TestCapture`). |
| **Production path** | eBPF at the TC hook (TCX, Linux ≥ 6.6) writing per-flow counters into a BPF hash map, so packets are never copied to user space. |

## 2. Traffic classifier — `classifier.py`, `features.py`, `ml/train.py`

| | |
|---|---|
| **Inputs** | active flows; 27 header-only features (`features.FEATURE_NAMES`): packet-size statistics, inter-arrival timing, direction ratio, ON/OFF burst structure, early-flow statistics, protocol number. Ports and addresses are deliberately **not** features. |
| **Outputs** | per flow: `label ∈ {voip, gaming, video, streaming, web, bulk}`, `confidence` (isotonic-calibrated), conformal prediction set, `source ∈ {user, ml, ml-hold, rules, rules-fallback}` (`ml-hold`: the model is unsure but the class the flow earned from a confident decision is still in its prediction set, so the flow keeps it), and the *committed* label that drives enforcement. |
| **Baseline** | `RuleClassifier`, qosify-style: UDP with average packet < 500 B → voice, UDP larger → video, TCP above 100 pps for 5 s → bulk, otherwise best effort. |
| **ML** | random forest (60 trees, depth 14) + isotonic calibration. Split-conformal threshold at α = 0.10: if the prediction set has more than one class the model **abstains** and the rule label is used. |
| **State** | per flow: `unlabelled → provisional → committed`. A new label must persist 2 ticks (1 s) before it moves a flow (anti-flap). User corrections commit immediately. |
| **Correction loop** | `POST /api/flows/label` or the dashboard menu → override for that flow or for the same device + protocol + remote port; applied on the next tick; the header features are appended to `data/corrections.jsonl`; `ml/train.py --corrections` folds them in (5× weight). |
| **Drift** | Page-Hinkley detector on the abstention stream. An increase raises a `drift` event telling the operator to collect corrections and retrain. |
| **Failure handling** | missing or incompatible model (feature list mismatch) → rule baseline, with an event and `model_loaded=false` on the dashboard. |
| **Success condition** | the hybrid beats the rule baseline on held-out sessions at the *scheduling-class* level, and the scenarios show the outcome improves (S3 TV bitrate, S1 call latency at download onset). See `docs/CLASSIFIER_REPORT.md`. |

## 3. Link-capacity estimator — `estimator.py`

| | |
|---|---|
| **Inputs** | ICMP timestamp replies (one-way delay per direction) or echo replies (RTT) from configured reflectors every 50–100 ms; achieved throughput per direction from the shaper's own counters. |
| **Outputs** | shaper rate per direction, state, delay rise above baseline (ms), per-reflector baselines, confidence (reflector health). |
| **Algorithm** | (re-implemented from the documented design of cake-autorate; no code copied) Baseline per reflector falls fast and rises slowly, only while load < 40 %. Bufferbloat = the *minimum* delay rise over the last 400 ms (at least 3 samples) exceeds +15 ms -- a CoDel-style standing-queue test that ignores transient jitter. On bloat: `rate = achieved × 0.85` (or `rate × 0.85` if we carry almost no traffic and the queue is under 100 ms; a deeper queue while we push little means the line itself slowed, e.g. a failover, so we cut to what it delivered), then no further cut until the queue found has had time to drain (`delay / 0.15`, 0.75–4 s) and a 2 s increase hold. When load > 75 % and healthy: fast recovery (`+max(1.5 % of rate, 0.5 % of max)` per tick) far below the last bloat point, gentle probing (+0.3 % per tick) within 10 % of it, like CUBIC's plateau; the ceiling is forgotten once the rate passes it by 10 % without bloat. When idle: drift 2 % per tick toward base, but only if no queue formed in the last 30 s (after a failover the plan rate is no longer the line's). **Path changes** (failover to a 4G backup, a route change) raise the delay whatever we send; a full modem buffer looks flat too, so after 3 cuts with a flat delay (each within ±30 % of the first, no more than 15 s apart; a growing delay is a filling queue and starts a new count) the estimator measures directly, like BBR's ProbeRTT: it holds the shaper at its floor for 0.8 s so any queue drains and reads the delay over the last 0.3 s. Between +5 and +100 ms → the path got longer (after the hold no queue of ours is left, and a 4G path change of about +15 ms sits right at the bloat threshold, so the probe uses its own lower test): the baseline is re-learned, the rate resumes from the larger of where the probe started and what the line delivered when the flat cuts began, and an event is logged. Below → it was a queue: keep cutting. Above 100 ms → a deep upstream queue not yet drained: keep cutting, and allow another probe after 4 s. At most one probe per direction per 20 s, also at the floor. The router's own probes travel in their own HTB class outside the shaped tree, so they measure the line's queue, never ours. Clamped to [max(2 %, 1 Mbit/s), 90 %] of the plan rate for download (shaped after the bottleneck, so it needs more margin) and [max(2 %, 1 Mbit/s), 95 %] for upload; the low floor lets it shape a 4G backup that is a tenth of a symmetric fibre plan. A big queue gets a deeper cut (`f = delay / 1.5 s`, 15–50 %) so it drains within about a second or two. |
| **State** | per direction: `idle / low / high / bloat / probe / rebaseline`, plus global `blind` |
| **Failure handling** | no replies for 2 s → `blind`: never increase, cap at base rate, banner on dashboard. A reflector that never answers timestamp requests is switched to echo automatically. Raw socket unavailable → fixed rate at the configured ceiling (90 % down, 95 % up). |
| **Success condition** | after a 100 → 20 Mbit/s drop, interactive delay returns within 30 ms of baseline and stays there (S2 `recovery_s`), without oscillating (policy-change counter). |

## 4. Policy engine — `policy.py`, `daemon.py`

| | |
|---|---|
| **Inputs** | committed labels, per-flow rates, active intent leases with grants, estimator rates, last applied plan, change counter. |
| **Outputs** | plan `{down, up} × {rate, guarantee per class}` and a flow → class map. |
| **Logic** | reserve measured realtime demand × 1.3 (voice, video), intent grants in their own top class, cap realtime at 60 % of the rate, split the rest 2:1 between best effort and bulk, every class keeps a floor (voice 5 %, video 5 %, best effort 10 %, bulk 5 %). |
| **Shield** | after every decision: rate within bounds, +15 %/−50 % step limits (cutting is the safe direction), floors restored, guarantees scaled to fit, change budget (90/min; decreases always allowed). Every correction is counted (`shield_interventions_total`). |
| **State** | `last-known-good plan` ↔ `applied plan`; hysteresis skips changes under 5 %. |
| **Failure handling** | exception in a tick → logged, last-known-good plan stays in force. Starvation detector: a class with backlog and zero service for 3 s → rollback to last-known-good and an event. |
| **Success condition** | no class starves in any run (`starved_seconds` = 0 for bulk), interactive classes keep their reservation under contention (S1, S3). |

## 5. Enforcement — `enforce.py`

| | |
|---|---|
| **Inputs** | plan, flow → class map |
| **Outputs** | Linux `tc` tree on the LAN egress (download) and WAN egress (upload); nftables verdict maps `cls4`/`cls6` (5-tuple → mark, element timeout 60 s, refreshed every 20 s); mark → `skb->priority` (class) and DSCP (EF/AF41/CS0/CS1, so Wi-Fi WMM honours it). |
| **Backends** | **CAKE** (`diffserv4`, `dual-dsthost`/`dual-srchost`, optional `nat`, `ack-filter` upstream) on stock kernels; **HTB** with 5 classes, guaranteed floors, borrowing by priority, `fq_codel` leaves (short `bfifo` if the kernel lacks it). Chosen automatically. |
| **Transaction** | apply in one `tc -batch` → read back with `tc -j` and compare rates → commit as last-known-good, or roll back (in-place `change` first, full rebuild if the tree is damaged). |
| **Failure handling** | nft batch failure (an element expired underneath) → per-element retry; marks expire by themselves if the engine dies; the watchdog deletes the whole table. |
| **Success condition** | read-back matches within 5 %; `apply_failures` stays 0 in all runs. |

## 6. Temporary intent API — `intents.py`, `api.py`

| | |
|---|---|
| **Inputs** | `POST /api/intents {target, duration_s, goal, priority, requester, label, demand_*}`; `?dry_run=1` for a preview. |
| **Outputs** | a lease with grant (kbit/s), expiry, assurance status; or a rejection with a reason and the earliest time it could be granted. The preview lists which devices give up guaranteed share. |
| **Effect on traffic** | the leased device's video, interactive web (or, for a throughput goal, all its flows) move to the top *intent* class with the bargained guarantee; its voice/game flows stay in the voice class so they do not queue behind its own video. |
| **Rules** | duration ≤ 8 h; per-requester budget of boost-minutes per day; all leases together ≤ 60 % of capacity (best effort and bulk keep their floors); conflicts = overlapping time window *and* insufficient shared capacity; resolved by weighted Nash bargaining (`water_fill`, weights = priority). |
| **State** | `scheduled → active → expired | cancelled`; assurance `pending → satisfied | degraded | infeasible`, re-checked every 0.5 s against estimated added delay and measured throughput. |
| **Failure handling** | leases persist across restarts (`data/intents.json`). If the engine dies, the watchdog removes all boosts. |
| **Success condition** | S4: the chosen call keeps low delay while the other call and the backup keep working. |

## 7. Metrics & console — `metrics.py`, `console.py`, `web/console.html`, `/metrics`

| | |
|---|---|
| **Inputs** | engine snapshot every 0.5 s; `GET /api/system`, `/api/setup-check`, `/api/inspect`, `/api/model`; lab server `/lab/*` when present |
| **Outputs** | the console: seven operator views (Monitor, Traffic, Link, Policy, Prioritise, Experiments, System) and a Household view (Home, Devices, Prioritise); Prometheus exposition at `/metrics`; per-tick JSONL time series. |
| **Console services (`console.py`)** | grade from the 95th percentile of added delay over 60 s, Waveform thresholds (A+ < 5 ms … F ≥ 400 ms); device registry (names and types, `PUT /api/devices/<ip>`, persisted in `data/devices.json`); modes (`POST /api/modes/<id>/activate`, each an ordinary lease); schedules (`POST/DELETE /api/schedules`, a day-and-time window that starts one lease per day); engine CPU and memory; the setup guide's checks run live; verbatim `tc -s` and `nft` output. |
| **Operator controls** | `POST /api/automation {paused}` holds the safe baseline and stops adaptation; `POST /api/rollback` returns to the last verified policy; `POST /api/safe`. Each is logged in the change log with its reason, as is every adaptive change (`apply_log` in the snapshot). |
| **Fairness** | Jain's index over busy devices, normalised by max-min fair share (Ware et al. 2019 motivate demand awareness); the harness also reports Ware's harm-style comparisons via per-arm deltas. |
| **Failure handling** | the console shows "Engine not reachable" and retries every second; an in-page token dialog on HTTP 401; with a recording embedded it falls back to a clearly labelled replay. |
| **Success condition** | every number in the evidence report can be read live in the console during a demo, and every brief deliverable has a screen that proves it (`docs/UX_BRIEF.md`). |

## 8. Experiment harness — `testbed/`

| | |
|---|---|
| **Inputs** | number of seeds, scenario list, arm list |
| **Outputs** | `results/runs/<scenario>/<arm>/seedNN/metrics.json`, engine time series and events, `results/REPORT.md`, `results/report.html`, figures, `summary.json` |
| **Procedure** | per run: reset ISP rate and gateway → start the arm → seeded timeline of flows and conditions → metrics → cleanup. Arm order shuffled per seed; every arm gets the same seeded parameters. |
| **Failure handling** | a failing run is recorded with its error and excluded; `--resume` continues an interrupted campaign. |
| **Lab server (`labd.py`)** | serves the console, proxies the engine API, and runs scenarios on request (`POST /lab/experiments/run`), reporting progress and serving the resulting summary and figures. Also: traffic presets, link conditions, engine kill/restart. |
| **Success condition** | one command reproduces the whole report: `make testbed eval report`. |

## 9. Router runtime — `router/iq-capture.c`, `treemodel.py`, `manage.py`, `uciconf.py`

| | |
|---|---|
| **Inputs** | packets on the LAN interface (helper); `models/flowclf.iqm`; managed settings from the controller; `/etc/config/intentqos` (UCI) |
| **Outputs** | per-flow aggregates every 0.5 s (`W`/`X` lines on stdout); class probabilities and conformal sets; the live config overlay (`state_dir/managed.json`); per-device usage |
| **States** | helper running / exited (engine falls back to Python capture); model loaded / swapped (previous kept for rollback); managed version |
| **Failure handling** | helper death → Python capture and an event; model file that fails signature, hash or size checks is not loaded; a managed setting outside the allow-list or its bounds is rejected whole; floors summing above 60 % are rejected |
| **Success condition** | aggregates bit-identical to the Python capture (`tests/test_capture_helper.py`); probabilities within 3 × 10⁻⁸ of scikit-learn; engine 2.7 % CPU and 25 MB on x86 |

## 10. Router agent — `agent.py`, `ed25519.py`

| | |
|---|---|
| **Inputs** | controller URL, operator CA, serial and claim code, release public key; the local engine API |
| **Outputs** | enrolment; telemetry every 15 s at the consented level; command results |
| **States** | unenrolled → enrolled; online / offline (telemetry buffered, up to 480 records); telemetry level basic / standard / diagnostic (≤ 60 min) |
| **Failure handling** | the agent is a separate process: if it fails, shaping continues. Unknown device → re-enrol with the claim code. Unsigned or badly signed config/model → refused. Relay calls outside the allow-list → refused on the router |
| **Success condition** | zero-touch enrolment, signed config applied, relay round trip ≈ 0.1 s in the lab (`tests/test_router_and_controller.py`) |

## 11. Fleet controller — `controller/`

| | |
|---|---|
| **Inputs** | agent telemetry, events and results; staff actions; subscriber actions; BSS calls (northbound API) |
| **Outputs** | alerts, incidents, diagnoses, signed configs and models, rollout decisions, reports (CSV), notifications, webhooks, audit log |
| **States** | per home: online / offline, alert set, config and model versions; per rollout: stage n of N → completed / halted / rolled back; per incident: open → investigating → identified → resolved |
| **Failure handling** | rollout guard halts and rolls back on a significant delay regression (Mann-Whitney) or apply failures; incident correlation needs ≥ 25 % and ≥ 3 homes on one OLT; replayed or stale agent requests rejected (nonce cache, 5-minute window) |
| **Success condition** | lab: OLT incident opened for 30 of 31 homes; bad model halted 33 s into stage 1 with 20 homes rolled back; good model reached 100 % |
