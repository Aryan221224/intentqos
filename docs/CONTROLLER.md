# The fleet controller

`controller/` is the ISP-side software: one Python service (stdlib + SQLite), an operator console
(`/ops/`), the consumer app (`/app/`), and APIs for routers, the app and the ISP's other systems.

```
python3 -m controller.pki ...      # lab CA and server certificate
python3 -m controller.seed --sim 240   # plans, staff, profiles, signed models, 1 real + 240 simulated homes
python3 -m controller.server --config controller/controller.toml
python3 -m controller.simfleet --story # simulated homes (labelled "simulated" everywhere)
sudo scripts/stack.sh up            # all of the above + testbed + engine + agent
```

Listeners: HTTPS on 8443 for routers, staff and the app; with `ui_listen = "127.0.0.1:8088"` (the lab
config) also plain HTTP on the loopback address only, so a browser on the same computer has no
certificate warning. In the lab, **Fleet overview → Simulate a network problem** makes the simulated
homes on one OLT congested or offline; the controller then has to detect and correlate it by itself.
Staff sign-in locks a user name (and the client address) for 15 minutes after five wrong passwords.

## What operators need, and where it came from

Researched from the platforms ISPs use for home networks and QoE (sources at the end).

| Need | Seen in | Here |
|---|---|---|
| Support agent sees a home's live state, history and devices, and gets a suggested fix | Plume Haystack Frontline (real-time data, 7-day history, recommendation engine); Calix Support Cloud | **Homes → home view**: diagnosis card "Is it the home or the network?" with evidence and recommended actions; 2-hour chart; devices; events; commands; notes; tickets |
| Tell a home problem from a network problem | Calix Support Cloud ("subscribers, systems and ports impacted by an outage"); Domos "Network X-ray" (home vs access vs core) | **Incident correlation**: when ≥ 25% (and ≥ 3) of an OLT's homes raise capacity, delay or connectivity alerts together, one network incident opens and the home alerts link to it; the diagnosis then says "area problem, not the home" |
| Proactive outage communication | Calix (status notifications through the CommandIQ app) | **Notify affected subscribers** (automatic for outages); a message appears in their app, and again when resolved; TRAI's 4-hour outage rebate rule is mentioned |
| Remote actions without a truck roll | Plume Frontline, Calix | Line test, live console relayed to the router, re-send config, roll back, safe baseline, pause automation, ask the subscriber for a diagnostic session |
| Subscriber lookup by name, IP, device | Preseem ("instant subscriber lookup") | Search by account, name, phone, serial, area, OLT |
| Plan and experience side by side | Preseem ("each subscriber's plan, usage and experience") | Shaping rate vs plan vs achieved on every home; plan rates pushed to routers from billing |
| Network topology / per-element view | Preseem (topology), Calix Operations Cloud (geo map) | Region → PoP → OLT tiles with affected share |
| Safe change management | (operator practice; USP software-module management) | **Staged rollouts** of signed models and config profiles (e.g. 5% → 25% → 100%) with an automatic guard: Mann-Whitney comparison of updated homes vs the rest; halt and roll back on regression or apply failures |
| Reporting against regulation | TRAI QoS regulations 2024: wireline latency < 50 ms, jitter < 40 ms | QoE report by region, PoP, OLT or plan with those two thresholds (as a proxy: the router measures added delay at the access link), CSV export |
| Least privilege and accountability | DPDP Rules 2025 (access control, logging, 1-year logs) | Roles viewer / support / noc / admin; every view of a home and every action in the audit log, visible to the subscriber in "See my data" |
| Integrations | Calix/Plume APIs | Northbound API (API key): create/update subscribers and plans (a plan change re-configures the router), provision gateways and claim codes, incidents, fleet QoE; signed webhooks (alert, incident, ticket, rollout events) |

## Alert rules (per home, from telemetry)

| Rule | Opens when | Severity |
|---|---|---|
| High delay under load | grade C or worse for 4 records (~1 min) | minor; major at D/F |
| Line short of plan | busy, and shaping rate < 60% of plan for 4 records | major |
| Reflectors not answering | blind mode for 2 records | minor |
| Engine down | agent reports the engine is not answering | major |
| Watchdog trip, shaper apply failures, capture drops | as reported | major / minor |
| Classifier drift | Page-Hinkley alarm on the router | info |
| High CPU | > 70% for 4 records | minor |
| Priority not met | a lease is degraded or infeasible | minor |
| Offline | no report for 4 intervals (60 s) | major |

Alerts close after 4 clean records. Every rule is visible to support with the measured value.

## Measured in the lab (241 homes: 1 real gateway + 240 simulated)

- Telemetry ingest ≈ 16 records/s on one Python process (SQLite WAL).
- OLT congestion (simulated, 31 homes): incident opened about 1.5 minutes after the congestion began
  (4-record alert delay plus a 15-second correlation pass), "29 of 31 homes affected".
- Rollout of a regressing model: guard halted it 33 s after stage 1 began (updated homes 23 ms vs
  5.3 ms delay added, p = 8 × 10⁻⁸) and rolled back all 20 updated homes; a good model advanced
  through 10% → 50% → 100%.
- Relay round trip to the real gateway (remote console) ≈ 0.1 s; line test across the emulated
  access link 94 Mbit/s on a 100 Mbit/s plan.

## Scaling path

SQLite and one process are for the reference build. For an operator fleet: telemetry in a time-series
store (TimescaleDB/ClickHouse) or Kafka → stream processing for alerts; the agent channel behind a
load balancer with sticky long-polls (or USP over WebSocket/MQTT, see AGENT_PROTOCOL.md); the
release key in an HSM/KMS; SSO for staff; multi-tenant for wholesale brands.

## Sources

- Plume Haystack Frontline datasheet: https://static.plume.production.k3.m1.brightspot.cloud/c8/2b/cf1e547942c5a38c419996d500ea/frontline-tier-1-support-datasheet-us.PDF
- Calix Support Cloud network insights: https://www.calix.com/press-release/2023/08/calix-support-cloud-extends-visibility-beyond-subscribers-home-wifi.html
- Calix Operations Cloud: https://www.calix.com/products/platform/cloud/operations.html
- Preseem features: https://preseem.com/features/
- Airties and Domos (latency management, Network X-ray): https://www.lightreading.com/broadband/airties-domos-team-to-improve-latency-for-broadband-operators
- USP overview (QA Cafe): https://www.qacafe.com/resources/user-services-platform-tr-369-overview/
- USP WebSocket vs MQTT (Axiros): https://www.axiros.com/blog/2026/websocket-vs-mqtt
- TR-181 / TR-471 (IP capacity), OB-UDPST: https://github.com/BroadbandForum/obudpst
- TRAI 2024 broadband QoS norms (MediaNama): https://www.medianama.com/2024/08/223-trai-tightens-quality-norms-for-broadband-networks/
- DPDP Rules 2025 summary (Tsaaro): https://tsaaro.com/blogs/dpdp-rules-2025-explained-full-overview-and-practical-summary
- prpl Life Cycle Management: https://prplfoundation.org/prplos/
