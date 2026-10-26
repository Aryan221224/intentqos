# Router ↔ controller protocol

The router agent (`intentqos/agent.py`) is the only thing on the gateway that talks to the operator.
It is a separate process from the engine: if it fails, shaping is unaffected.

## Transport and identity

| Aspect | Implementation |
|---|---|
| Direction | Router → controller only (HTTPS). Works behind NAT/CGNAT; no open port on the router. |
| Server identity | TLS 1.2+; the agent trusts only the operator CA (`ca_file`). |
| Device identity | Zero-touch enrolment: serial + one-time **claim code** (on the router label / provisioned by the factory or through `POST /v1/nb/gateways`) → device id + 256-bit device secret, stored 0600. Optional mTLS client certificate (`client_cert`). |
| Request authentication | Every request carries `X-IQ-Device`, `X-IQ-Time`, `X-IQ-Nonce` and `X-IQ-Signature = HMAC-SHA256(secret, method ∥ path ∥ time ∥ nonce ∥ SHA-256(body))`. The controller rejects signatures older than 5 minutes and any repeated nonce. |
| Re-enrolment | If the controller no longer knows the device (database restored, router replaced), the agent re-enrols with its claim code automatically. |
| Payload authenticity | Config profiles and models are **signed with the operator's Ed25519 release key** (`intentqos/ed25519.py`, RFC 8032). The router verifies before applying, independently of TLS. |
| Proxies | The agent ignores proxy environment variables: a router connects directly. |

## Messages

| Endpoint | Purpose |
|---|---|
| `POST /v1/agent/enroll` | serial, claim code, hardware, versions → device id, secret, release public key, interval |
| `POST /v1/agent/sync` (every 15 s) | telemetry records (backlog first after an outage), results of executed commands, current config/model version and telemetry level → pending commands, interval, telemetry level from the subscriber's consent |
| `GET /v1/agent/wait?timeout=25` | long-poll: returns as soon as a command is queued (remote console, support actions) |
| `POST /v1/agent/result` | command results, sent immediately after executing |
| `GET /v1/agent/blob/<sha256>` | model download; checked against the signed hash and size |

Telemetry record (standard level): grade, p95 added delay down/up, mean and max added delay, jitter,
shaper loss, shaping rate vs achieved vs plan, deepest queue, Jain fairness, flows, active devices,
leases, class mix, estimator state, safety counters, capture backend and drops, CPU and memory, versions,
the events since the last record, and (standard and above) the device list with activity. Diagnostic
level adds the top 20 flows; it lasts at most 60 minutes and needs the subscriber's approval in the app.
Basic level sends aggregates only.

Commands: `apply_config` (signed), `install_model` / `rollback_model` (signed), `set_level`,
`line_test` (download through the access line while sampling delay), `snapshot`, `relay` (an
allow-listed call to the local engine API, used by the remote console and the consumer app).

The relay allow-list (agent side, so a compromised controller cannot do more): read state, history,
system, setup check, kernel state, model, modes, schedules, devices, usage, config; write intents,
flow corrections, classifier mode, safe, rollback, automation, modes, schedules, device names and pauses.
Per-flow detail in relayed state is removed unless a diagnostic session is active.

## Mapping to TR-369 (USP) and TR-181

The same functions map onto Broadband Forum USP for operators that run a USP controller; the agent
would then be a USP agent (for example obuspa on prplOS) exposing a vendor data model:

| IntentQoS | USP / TR-181 |
|---|---|
| enrolment with claim code | USP onboarding (`OnBoardRequest`), controller trust (`Device.LocalAgent.ControllerTrust.`) |
| sync telemetry | Bulk Data Collection (`Device.BulkData.Profile.{i}.`) of `Device.X_INTENTQOS_QoS.Stats.` |
| events | `Notify` (Event, ValueChange) |
| `apply_config` | `Set` on `Device.X_INTENTQOS_QoS.Config.` (allow-listed parameters, e.g. `DownstreamRate`, `FloorVoice`, `ClassifierMode`) |
| `install_model` | `Device.SoftwareModules.` / LCM deployment unit, or `Operate` `Device.X_INTENTQOS_QoS.InstallModel()` |
| `line_test` | `Device.IP.Diagnostics.IPLayerCapacityMetrics()` (TR-471) or `UDPEchoDiagnostics()` |
| leases | `Device.X_INTENTQOS_QoS.Intent.{i}.` (Add/Delete), mirroring `Device.QoS.Classification.{i}.` |
| `relay` reads | `Get` on the vendor object tree |

`X_INTENTQOS_` stands for the vendor prefix (the operator's or vendor's OUI/domain) required by TR-106.
The HTTPS transport here plays the role of USP's WebSocket MTP: one outbound connection per router.
