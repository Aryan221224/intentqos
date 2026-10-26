# IntentQoS as a product: who uses it, where it runs, and how the parts fit

IntentQoS is **gateway software for internet providers**. It runs on the router the ISP installs in
each home and is managed from the ISP's own systems. Three pieces of software are delivered:

| Piece | Runs on | Used by | In this repository |
|---|---|---|---|
| **Router engine + agent** | The home gateway (OpenWrt / prplOS / RDK-B class Linux router) | Nobody day to day: it shapes traffic on its own. Firmware engineers install and validate it. | `intentqos/`, `router/` (C capture helper, OpenWrt package), `deploy/` (systemd) |
| **Fleet controller** | The ISP's data centre or cloud | Support agents, NOC engineers, product/ops managers, admins; OSS/BSS systems through its API | `controller/` (server, ops console `web/ops.html`) |
| **Consumer app** | The subscriber's phone (served by the controller) | Household members | `controller/web/app.html` |
| Per-home console | The router itself (LAN), or relayed through the controller | Power users, field technicians, support (remote, audited) | `intentqos/web/console.html` |

```
 HOME                                   ISP                                       PEOPLE
 ┌──────────────────────────────┐      ┌──────────────────────────────────┐
 │ router                        │      │ controller                        │ ◄── support agent: "is it the home
 │  engine: classify, estimate, │ HTTPS│  enrolment, telemetry, alerts,   │      or the network?", live console
 │  shape (tc/nft), leases,     │ ───► │  incident correlation, rollouts, │ ◄── NOC: incidents, notify homes
 │  watchdog                    │ ◄─── │  signed config & models, audit,  │ ◄── ops: staged rollouts, reports
 │  agent: outbound only,       │ long │  consumer-app backend, NB API    │ ◄── BSS/CRM: plans, tickets (API)
 │  signed, buffered            │ poll └──────────────────────────────────┘
 └──────────────────────────────┘                     ▲
         ▲ LAN console (optional)                     │ HTTPS, phone + one-time code
         │                                            │
      household ◄──────────────────── consumer app ───┘
```

Design rules that come from where it runs:

- **The router never depends on the controller.** Shaping, classification, leases and the watchdog work
  with no controller at all; the agent buffers telemetry (2 h by default) through outages and catches up.
- **The router only makes outbound connections** (works behind NAT/CGNAT); the controller reaches it
  through the agent's long-poll, typically in about 0.1 s.
- **Nothing unsigned changes a router.** Config profiles and models are signed with the operator's
  Ed25519 release key and verified on the router; only allow-listed settings can be managed remotely.
- **Privacy is enforced at the edge.** Header-only classification; telemetry level follows the
  subscriber's consent (basic / standard / diagnostic, the last one time-limited and approved in the app);
  live views in the app and the remote console are relayed, not stored.

Documents: [DEPLOYMENT.md](DEPLOYMENT.md) (install each piece), [ROUTER_PROFILE.md](ROUTER_PROFILE.md)
(running on router hardware, measured), [AGENT_PROTOCOL.md](AGENT_PROTOCOL.md) (router ↔ controller,
TR-369 mapping), [CONTROLLER.md](CONTROLLER.md) (operator functions, research behind them),
[CONSUMER_APP.md](CONSUMER_APP.md) (subscriber features, research, privacy).
