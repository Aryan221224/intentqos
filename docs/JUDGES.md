# IntentQoS in five minutes (for reviewers)

**The problem.** In a busy home, one download or backup fills the modem's oversized buffer and every
call, game and video stalls. Home routers either do nothing or apply fixed rules that cannot tell a
TV stream from a download and do not notice when the line slows down.

**What we built.** A QoS engine that runs on the home router and keeps calls and games responsive
while downloads still progress. It recognises traffic from packet sizes and timing only (no payload,
no domain names), measures the real line capacity from delay, reshapes every half second inside a
safety envelope, and lets a person say "prioritise my work call for an hour" as a time-limited lease.
Around it sit the two things an ISP needs to ship it: a fleet controller for its network and support
teams, and a branded app for its subscribers.

| Part | Who uses it | Look at |
|---|---|---|
| **Gateway** (router package: engine, watchdog, agent) | nobody directly; it just works. Engineers use its console | `IntentQoS Gateway.exe` or http://localhost:8090/ |
| **Fleet controller** | the ISP's network operations and support desk | `IntentQoS Operator Console.exe` or http://localhost:8088/ops/ |
| **Subscriber app** | the household, in English or Hindi | `SampleNet Home App.exe` or http://localhost:8088/app/ |

## See it running

- **Windows 10/11 with WSL 2:** double-click `dist/IntentQoS Demo (all three).exe`. The first start
  installs the release into WSL (about a minute) and opens three windows. `Stop IntentQoS.exe` stops
  everything. The gateway is the real engine shaping real traffic in an emulated home network inside WSL.
- **Linux:** `sudo scripts/stack.sh up`, then the three addresses above.
- **No setup at all:** open `docs/console-replay.html` (a recorded session of the gateway console,
  labelled as a replay) and the screenshots in `docs/img/`.

## A two-minute path through it

1. **Gateway, Monitor.** Five channel strips per direction, one per traffic class. A video call, three
   TVs, an ISO download, a backup and a game are running; delay stays low and the grade stays A.
   *Link → drop to 20/5 Mbit/s*: the delay spikes, the estimator cuts the rate within a second, the
   delay returns to flat, and **Policy** says why.
2. **Subscriber app, Boost.** Pick *Asha's laptop*, *Calls and games*, *1 hour*. The preview says how
   much is reserved and who will share what is left. The boost appears on Home with a countdown and a
   live "working" check, and on the gateway as an intent lease.
3. **Operator console, Fleet overview.** *Simulate a network problem* on one OLT. Within about two
   minutes its row of homes turns colour, one incident opens instead of 30 alerts, and each affected
   home's diagnosis says "area problem, not the home". The subscriber app shows the outage notice.
4. **Operator console, a home.** The diagnosis ("is it the home or the network?"), live devices,
   a line test from the router, and *Open live console*, which is the gateway console relayed through
   the router's agent, audited.
5. **Rollouts.** A regressing classifier model was halted 33 s into its first stage and rolled back
   on 20 homes automatically; a good one reached all homes.

## Evidence

160 seeded runs on an emulated access link (10 seeds per arm, medians, Mann-Whitney p-values; method
and every number in `results/REPORT.md`).

| Scenario (brief) | No QoS | Static QoS | IntentQoS |
|---|---|---|---|
| S1 download + backup during a call: call delay p99, upload | 113 ms | 19 ms | **17 ms** |
| S1: web page load p95 | 135 ms | 139 ms | **76 ms** |
| S2 link drops 100 → 20 Mbit/s: call delay p99 once settled | 219 ms | 93 ms | **47 ms** |
| S2: recovery time | 16 s | 15 s | **4.9 s** |
| S2: TV rebuffering | 11 s | 13 s | **3.1 s** |
| S3 three TVs + game + update: game delay p99 | 147 ms | 60 ms | **45 ms** |
| S5 engine killed: safe state restored by the watchdog | | | **1.8 s** |

**Does the machine learning help?** Only where the class matters, and that is measured, not assumed.
The model recognises adaptive video, which rules lump in with downloads: TV rebuffering falls from
18 s (rules) to 3 s after a link drop, and TV bitrate rises 67 % in S3. The price is bulk throughput:
by default, best effort (streaming, browsing) gets twice bulk's share of what is left, so the game
update drops from 54 to 25 Mbit/s in S3. That ratio is an operator setting
(`policy.remainder_bulk_share`, pushed per home as a signed profile). With the *bulk-friendly*
profile the same scenario gives an update at 37.6 Mbit/s (+51 %), TVs at 10.4 Mbit/s (still 41 % above
rules, no rebuffering) and a slightly lower game delay, at the cost of more upload delay for the game. The rule arm's game delay is lower in S3 (27 vs 45 ms);
`docs/FINDINGS.md` lists this and the other results that do not favour IntentQoS, with causes.

## Can it run on a real router?

Yes, as an OpenWrt package (`router/dist/*.ipk`, `make ipk`): a 324 KiB package, a static C capture
helper per CPU (54-82 KB), a pure-Python model runtime (no NumPy), procd services, UCI config, and a
router key for the console. Measured: 2.7 % of one x86 core and 25 MB. The aarch64 build ran under
emulation only; a physical router is the next step (`docs/ROUTER_PROFILE.md`).

## Where to look next

| If you want | Read |
|---|---|
| The product and who uses which part | `docs/PRODUCT.md` |
| Architecture (three tiers, the control loop) | `docs/ARCHITECTURE.md` |
| Every module's inputs, outputs, states and failure handling | `docs/MODULES.md` |
| What is emulated and what a production version still needs | `docs/KNOWN_LIMITATIONS.md` |
| The router ↔ controller protocol and its TR-369/USP mapping | `docs/AGENT_PROTOCOL.md` |
| Installation of each part | `docs/DEPLOYMENT.md`, `launcher/README.md` |
