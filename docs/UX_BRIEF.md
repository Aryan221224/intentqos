# IntentQoS Console: UX brief

## 1. What the case study asks the product to be

The brief does not ask for a consumer router app. It asks for an **adaptive QoS controller you can
operate, verify and evaluate**, with one household-facing capability: a temporary intent. Its
"Expected output and acceptance evidence" list maps directly to screens:

| Brief deliverable | Console screen | What the screen must prove |
|---|---|---|
| Traffic-class and link-capacity estimator | **Traffic**, **Link** | Every flow's class, confidence and source; correction in one action; estimated capacity vs. achieved vs. plan per direction; why the rate changed |
| Dynamic QoS policy engine and enforcement module | **Policy** | Current guarantees and floors per class, every change with its reason, shield interventions, change budget, rollback / safe mode / pause, the live `tc` and `nftables` state (inspectable) |
| Dashboard for latency, jitter, loss, throughput, queue depth and fairness | **Monitor** | All six metrics, live, with history and event markers |
| Automated baseline-vs-optimised experiments | **Experiments** | Run a scenario against baselines, watch it, read medians/IQR/p-values and figures; inject conditions by hand (link drop, ISO download, TVs, game) |
| API or user interface for temporary service intent | **Prioritise** (+ REST API) | Request → preview impact → lease with countdown and assurance; modes and schedules; budgets and bargaining visible |
| Architecture diagram (device, edge, network, data, analytics, UI) | **System** | The architecture drawn with *live* health of each component |
| Setup guide with verification steps | **System → Setup check** | Each verification step run live: pass / fail with the command it ran |
| Known-limitations section | **System → Limitations** | The prototype/production boundary, in the product itself |
| Evaluation criteria (accuracy, latency/jitter/loss reduction, fairness, adaptation speed and stability, CPU overhead, reproducibility) | **Monitor**, **Traffic → Model**, **Experiments**, **System** | Classifier accuracy vs. rules; adaptation events and change rate; engine CPU and memory; seeds and environment of every result |

Two audiences, one product:
- **Operator / evaluator** (the jury, an ISP engineer, a power user): full detail, controls and evidence.
- **Household member**: only "is it smooth?", "what is each device doing?" and "prioritise my call".
  A **Household view** toggle shows just these, using the same data.

## 2. What the market does (researched October 2026)

| Product | Prioritisation UX | Health / quality UX | Gaps relevant to us |
|---|---|---|---|
| Google Home (Nest Wifi) | Device → "Prioritize device" → duration at the bottom → Save. **One device at a time**, gets **70 %** of bandwidth; fixed durations up to 8 h | Speed tests | No preview of who loses; no conflict handling; no proof it worked ([Google Help](https://support.google.com/googlenest/answer/6246483?hl=en), [9to5Google](https://9to5google.com/2021/10/19/google-home-wifi-priority/)) |
| TP-Link Deco | "Scene acceleration" (Gaming, Video conferencing, custom scenes from app lists) or "Client acceleration"; each entry has an **"Always" / 1 h…** duration dropdown; user types total bandwidth | — | Manual bandwidth entry; drag-to-order lists ([TP-Link FAQ 1601](https://www.tp-link.com/us/support/faq/1601/)) |
| ASUS Adaptive QoS | One-click **modes** (Games, Media streaming, VoIP, Web surfing, Work-from-home) and drag-and-drop priority of app categories | Bandwidth monitor | "Only one QoS function at a time" ([ASUS FAQ](https://www.asus.com/support/faq/1010935/)) |
| eero | A single toggle, "Optimize for conferencing and gaming" (SQM) | Activity tab: live usage every 5 s, weekly speed tests, per-device usage ([eero Activity](https://eero.com/support/articles/what-can-i-see-in-the-activity-tab)) | Users cannot tell whether it is working ([eero community](https://community.eero.com/t/35hg7l5/sqm-or-optimize-for-conference-and-gaming)) |
| Firewalla | Smart Queue → queue type CAKE (beta); rules engine; flows with exclude filters | Live charts, Wi-Fi test ([Firewalla 1.56](https://help.firewalla.com/hc/en-us/articles/18872957627539-Firewalla-App-Release-1-56-Wi-Fi-Test-CAKE-and-more)) | Expert-only wording |
| UniFi | Smart Queues = two rate fields; warns against > 300 Mbit/s | Traffic identification by category ([UniFi Smart Queues](https://help.ui.com/hc/en-us/articles/12648661321367-UniFi-Gateway-Smart-Queues)) | Static rates; no adaptation |
| Plume HomePass | Bottom navigation (Home, People, More); customisable home cards (Guard, Control, Sense, Adapt) | Auto speed tests ([Allwest](https://www.allwest.com/blog/plume-homepass-app-update/)) | — |
| Starlink | — | Latency graph, **outage timeline with causes**, 12-hour history ([guide](https://satspeedcheck.com/blog/starlink-debug-data-guide/)) | — |
| Waveform bufferbloat test | — | Letter grade from *added* delay under load: A+ < 5 ms, A < 30, B < 60, C < 200, D < 400, F ≥ 400 ([summary](https://bufferspeed.com/compare/waveform)) | A one-off test, not continuous |

## 3. Patterns we adopt, and where we go further

Adopted:
- **Duration chips** for priority (Nest, Deco), capped at 8 h (Nest's cap).
- **Modes** as one-tap presets (ASUS, Deco scenes): Work call, Gaming, Movie night.
- **A grade from added delay under load** (Waveform), shown continuously instead of as a one-off test.
- **A timeline of events with causes** (Starlink outages), written in plain language.
- **Live per-device usage** (eero) and a **customisable, card-based home** (Plume), simplified.

Going further (the case study's own requirements, which no product above meets):
1. **Preview before commit**: who gives up share, what is reserved, what fraction of the request fits.
2. **Several concurrent leases**, resolved by bargaining, instead of "one device at a time".
3. **Assurance**: "Working / Not meeting its goal / Link too slow", checked continuously.
4. **Confidence and correction** on every classification, with "model unsure" shown honestly.
5. **Every automatic change explained and reversible**: reason, shield corrections, rollback, pause.
6. **Evidence in the product**: experiments against baselines, with statistics.
7. **Privacy by design, stated in the UI**: "classified from packet sizes and timing only".

## 4. Information architecture

```
Console
├── Monitor        six brief metrics, grade, class lanes, live charts, event timeline
├── Traffic        devices → flows; class, confidence, set, source; correct; model report
├── Link           capacity estimator per direction; delay rise; reflectors; decisions
├── Policy         guarantees & floors; change log with reasons; shield; budget; rollback /
│                  safe mode / pause automation; live tc + nft
├── Prioritise     request (device, goal, duration, importance) → preview → lease;
│                  modes; schedules; active leases with assurance; budgets
├── Experiments    scenario catalogue → run vs baselines → progress → results + figures;
│                  manual condition injection (link rate, traffic presets, reset)
└── System         live architecture; setup check; kernel capabilities; engine CPU/memory;
                   watchdog; limitations
Household view: Home (status + boosts + modes), Devices, Prioritise
```

## 5. Design principles

- One signature element: the **class lanes** ("the road"), carried over from the first dashboard.
- Numbers come with their meaning: "23 ms added delay, grade A: smooth for calls".
- Plain verbs on buttons ("Prioritise", "End now", "Roll back", "Run scenario").
- Destructive or global actions (safe mode, pause, rollback, reset lab) ask for confirmation and are logged.
- Works on a phone (household) and a laptop (operator); light and dark; keyboard accessible.
- Runs **live** against the engine, or as a **replay** of a recorded testbed session for anyone
  without a Linux machine (clearly labelled "Replay of a recorded session").

## 6. Where it is

- The console: `intentqos/web/console.html` (one file, no build step, no framework), served by the
  engine at `/` and by the lab server at `http://localhost:8090/`. The first dashboard stays at `/classic`.
- The replay: `docs/console-replay.html`, the same file with a recorded 150-second session embedded
  (`make replay` re-records it).
- Screenshots: `docs/img/gateway-monitor.png`, `gateway-traffic.png`, `gateway-link.png`,
  `gateway-policy.png`, `gateway-experiments.png`, `gateway-household.png`; the operator console as
  `ops-*.png` and the subscriber app as `app-*.png` (`app-home-hi.png` in Hindi).
