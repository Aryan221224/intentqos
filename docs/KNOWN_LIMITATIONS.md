# Known limitations: prototype vs. production

IntentQoS is a **reference implementation**: open tools, documented interfaces,
measurable experiments. This page draws the boundary the brief asks for between
what the prototype demonstrates and what a production product would still need.

## What is emulated (labelled, as the brief requires)

| Emulated | How | What it does not capture |
|---|---|---|
| The ISP access link | `tbf` in a network namespace with a deliberately large buffer (250 ms at plan rate; the byte size stays fixed when the rate drops, as on a real modem) | Real DOCSIS/GPON/DSL scheduling, request-grant upstream, ISP-side AQM, cross-traffic from neighbours |
| Base propagation delay | `netem` when the kernel has it; otherwise `testbed/delayline.py`, a user-space TUN delay line (constant 10 ms each way, no jitter or loss) | Path jitter, reordering, random loss |
| Household devices | Python generators (`testbed/traffic.py`) that reproduce the *packet-size and timing shape* of VoIP, video calls, gaming, adaptive streaming, browsing and bulk TCP | Real codecs, real WebRTC congestion control (our video sender is constant-bitrate and does not back off), real ABR players, QUIC |
| Internet reflectors | The emulated server answers ICMP timestamp requests | Public reflectors may rate-limit or de-prioritise ICMP |
| Wi-Fi | Not modelled; the LAN is a wire | Wi-Fi airtime contention is often the real bottleneck at home (see "Scope") |

The CI sandbox used to produce `results/` lacks `sch_cake`, `sch_fq_codel`
and `sch_netem`, so **those results come from the HTB backend with short `bfifo`
leaves and the user-space delay line.** On a stock Ubuntu/Debian/OpenWrt kernel the
engine selects CAKE automatically; re-run `make eval` there to get CAKE numbers.

## Classification

- Trained and tested on emulated traffic with randomised parameters, split by session.
  Accuracy on real household traffic **will be lower**. Use `ml/record.py` on a real
  gateway with consented, labelled devices and the dashboard's correction loop
  (`data/corrections.jsonl`), then `ml/train.py --corrections ...`.
- Flow-level statistics cannot separate applications multiplexed in one tunnel:
  VPNs, QUIC connection reuse and MASQUE proxies collapse many apps into one flow.
  The engine then falls back to per-host fairness, which is still safe.
- Several published "encrypted traffic" classifiers use TLS handshake fields
  (for example the SNI in the ClientHello). IntentQoS deliberately does not, to stay
  well inside the brief's no-payload rule. A production product might allow SNI
  hints only with explicit household consent.

## Performance and hardware

- Capture runs in the C helper `iq-capture` (TPACKET_V3 ring, 96-byte kernel-side truncation,
  per-flow aggregation), with the Python capture as fallback. Every packet still crosses into user
  space once. **Production path:** eBPF at the TC hook (TCX, Linux ≥ 6.6) aggregating per-flow
  counters in a BPF map, so packets never leave the kernel.
- **No physical router was used.** The router build ran natively on x86-64 and as aarch64 + musl
  under qemu-user emulation (with the installed `.ipk` and a UCI-generated config). qemu-user lacks
  `PACKET_RX_RING` and `SO_TIMESTAMPNS`, so the emulated run used the x86-64 helper and user-space
  receive timestamps. Emulated CPU numbers do not predict real ARM performance. Next step: a
  Cortex-A53 OpenWrt router.
- The engine still needs Python (`python3-light`, about 6 MB of flash with its modules). Routers with
  16 MB of flash or 64 MB of RAM would need the control loop in C or Rust.
- The Linux shaper (HTB or CAKE) takes a global qdisc lock and is effectively
  single-threaded. Multi-gigabit links need XDP `cpumap` scale-out (as LibreQoS
  does) or hardware QoS offload in the router SoC.
- No hardware offload is used, and none is claimed.

## Scope of control

- The engine controls only the **home side** of the bottleneck. It cannot change
  ISP scheduling. Cable (Low Latency DOCSIS) and 5G (QoS flows, network slicing)
  can prioritise inside the access network; that requires ISP integration and
  standardised interfaces that a home gateway does not have.
- Download shaping works by queueing *after* the bottleneck, which costs ~5 % of
  capacity (we shape at ≤ 95 % of the plan) and is less precise than upload shaping.
- Wi-Fi: the DSCP marks are set so WMM access points can honour them, but the
  engine does not manage airtime. Many home "lag" problems are Wi-Fi problems; the
  HoA method (Sundaresan et al., PAM 2016) would be the next step to tell them apart.
- L4S (RFC 9332, DualPI2 in Linux ≥ 6.17) is not used: it needs L4S-capable
  senders that most homes do not have yet. It is a natural experimental arm.

## Security and operations

- Root privileges are needed (tc, nftables, raw sockets). A production package would
  split privileges: a small privileged enforcement helper and an unprivileged engine,
  with systemd sandboxing (`deploy/*.service` already sets `ProtectSystem`, `NoNewPrivileges`, …).
- On a router (`api.require_auth`, on in the OpenWrt package) every console and API call needs the
  router key, created 0600 on first start like the password on a router's label; browser sessions are
  HttpOnly cookies and cookie-authenticated writes must be JSON. The lab leaves the API open. There is
  no TLS on the LAN side and one key per router rather than per-person accounts, so intent budgets are
  bound to the name a person types, not to an identity.
- No secrets are in the repository. The example token in the docs is fictitious.
- Packaging for OpenWrt (`.ipk`, procd, UCI) and signed model/config updates exist; firmware-image
  integration and certification (regulatory, carrier acceptance testing) were not attempted.
- Scale testing beyond one household (tens of devices, a few hundred flows) was not done on the engine.
- The Windows launchers run everything inside WSL 2, so the gateway lab uses WSL's Linux kernel. Its
  stock build has HTB, TBF and netem but not CAKE, so the engine selects the HTB backend there, as in
  `results/`, with fq_codel leaves (WSL's kernel has fq_codel; the CI kernel does not). The launchers
  are unsigned programs; Windows SmartScreen may ask before the first start.
- Found while testing the launchers on Windows: Ubuntu 24.04's `tc` (iproute2 6.1) returned output for
  `tc -j class show` that is not JSON, so every policy change failed its read-back and the engine kept
  its last policy. The enforcer now cuts the JSON out of mixed output and otherwise parses tc's text
  output (`tests/test_modules.py`), and a failing control loop is shown in the console instead of a
  frozen view.

## Fleet controller and consumer app

- **The fleet is 1 real gateway + 240 simulated homes.** Simulated homes (`controller/simfleet.py`) use
  the real agent protocol (enrolment, HMAC signing, telemetry, commands, signed model installs), but
  their telemetry comes from a statistical model, not from engines. They are labelled "simulated" in
  every view. Incident and rollout results for them show the controller logic works, not that real
  homes behave that way.
- One Python process and SQLite (WAL): about 16 telemetry records/s in the lab, which covers a few
  thousand homes at a 15 s interval. An operator fleet needs a time-series store, a stream processor
  for alerts and several agent front-ends (see "Scaling path" in CONTROLLER.md).
- The router–controller channel is a custom HTTPS protocol modelled on USP, not a certified TR-369
  agent. Its mapping to USP/TR-181 is documented, not implemented.
- Lab PKI and keys are generated locally (`controller/pki.py`); the release signing key is a file.
  Production keeps it in an HSM/KMS and uses the operator's CA. Staff sign-in has no SSO or 2FA; five
  failed passwords lock the account (and the address) for 15 minutes.
- The lab's quick sign-in (role buttons, `lab_quick_login`) and the plain-HTTP console listener
  (`ui_listen`, loopback only) exist for the demo. Both are off by default in the code; quick sign-in is
  accepted only from the same computer even when switched on.
- Subscriber sign-in uses a one-time code that, in the lab, is **shown on screen** (`demo_otp = true`):
  there is no SMS gateway. This must be off outside the lab.
- The app has no Wi-Fi name/password, guest network, parental controls or bill payment. Plan changes
  are requests that open a ticket; the subscriber is called to confirm.
- DPDP handling (consent, access log, export, erasure and grievance requests with due dates) is
  implemented as product features; it has not been reviewed by a lawyer.
- The TRAI report is a proxy: it reports delay *added* at the home's access link, not the
  end-to-end latency to a TRAI test server.
- The speed test is a single HTTP download from the operator's test server, not TR-471 / UDPST.
  Household traffic during the test is measured and reported next to it, so a busy home is not
  shown as a slow line.

## Classifier behaviour found in the lab

- **Streams first seen mid-flow look like downloads.** Several features describe the first packets of
  a flow. After an engine restart (watchdog recovery or upgrade), flows that were already running are
  seen from the middle, and long video streams were then classified as bulk. **Fixed for restarts:** the
  engine saves each live flow's committed class every 5 s (to RAM on a router) and a restarted engine
  keeps that class for the same 5-tuple (`tests/test_modules.py`). We also tried retraining with
  mid-stream copies of every flow: it cut streams-taken-for-downloads on a mid-stream test set from 89 %
  to 45 % but lowered accuracy on normal flows (98.7 → 96.7 %), so the shipped model is unchanged. A flow
  that started before the router itself booted is still classified from the middle.
- A starved video stream downloads continuously and briefly looks like a bulk transfer (seen during a
  speed test). Demoting it would starve it further. The classifier therefore needs `demote_ticks`
  (default 20 ticks, about 10 s) of consistent evidence before it moves a flow to a lower-priority
  class, while promotions still take 2 ticks (`tests/test_modules.py`).

## Measured trade-offs (from `results/`)

- Serving adaptive streaming above bulk cuts bulk goodput sharply when TVs are busy (S3: 25 vs 54–60 Mbit/s
  by default; 37.6 Mbit/s with the bulk-friendly profile, which gives the TVs less: 10.4 instead of 12.4 Mbit/s).
- With the ML classifier, game delay in S3 was higher than with rules (45 vs 27 ms p99), although still
  better than static QoS and no QoS. Download shaping after the bottleneck cannot stop every overshoot of
  bursty streams; CAKE's `ingress` mode (not in our test kernel) is the expected remedy.
- On a 3 Mbit/s uplink, an active lease leaves the backup only its 5 % floor, and TCP shows short stalls.
- The first seconds after a sudden capacity drop are dominated by the modem's already-full buffer.
- See `docs/FINDINGS.md` for the full list with numbers.

## Statistics

- Results are medians over a modest number of seeded runs on one machine with
  2 vCPUs. CPU contention between the emulator, generators and engine adds noise;
  the reported IQRs and Mann-Whitney p-values make that visible.
- Our MOS is a simplified ITU-T G.107 E-model proxy, not a perceptual test.
