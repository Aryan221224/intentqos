# Running on router hardware

A home gateway has a small ARM or MIPS CPU, 128–512 MB of RAM, a few tens of MB of flash, and a
musl-based userland (OpenWrt, prplOS). The first IntentQoS prototype used NumPy/scikit-learn and a
per-packet Python loop: fine on a PC, not on a router. The router build changes three things.

| Change | Before | After |
|---|---|---|
| **Classifier runtime** | scikit-learn + NumPy + joblib (~100 MB of libraries) | `intentqos/treemodel.py`: the same random forest + isotonic calibration, exported by `ml/export_model.py` to an 847 KiB file and evaluated in pure Python. Probabilities match scikit-learn to **2.9 × 10⁻⁸** on 2,000 test rows; 32 flows classify in **7 ms** (x86). |
| **Packet capture** | Python `recvfrom` per packet | `router/iq-capture.c`: TPACKET_V3 memory-mapped ring, kernel-side truncation to 96 bytes (a one-instruction BPF filter), per-flow aggregates in C, one text line per flow every 0.5 s. **Bit-identical aggregates** to the Python capture (`tests/test_capture_helper.py`). Static binaries: aarch64 66 KB, armv7 53 KB, mipsel 80 KB, x86-64 58 KB. |
| **Dependencies and storage** | `statistics`, `uuid`, `resource`, NumPy | Only modules in OpenWrt's `python3-light` plus `email`, `urllib`, `openssl`, `logging`. Volatile files (events, usage) in `/tmp` (RAM); small state (devices, schedules, managed config, agent credentials) in `/etc/intentqos`, so flash is not worn out. |

If the helper is missing or dies, the engine falls back to the Python capture and says so in its event log.

## Measured (emulated home network, same load: video call, ISO download, backup, 3 TVs, game, browsing)

| Build | Engine CPU (one core) | Engine memory | Classification |
|---|---|---|---|
| Original (Python capture, scikit-learn) on x86 | ~29% | ~158 MB | all flows correct |
| **Router build on x86** (helper + pure-Python model) | **2.7%** (+0.1% helper) | **25 MB** (+4 MB helper) | all flows correct |
| **2.0 engine in the lab** (experience sensing, outcome control, offers; pure-Python phase-wise model; light household load: a call and browsing) | 2.0% | 29 MB | not the same workload as the row above |
| **Router build as aarch64 + musl**, under qemu-user emulation | 21–30% of the *host* core, emulation included | 54–56 MB, qemu included | all flows correct |

The aarch64 run used a static musl Python (the same C library as OpenWrt) and the **installed
`.ipk` contents** (`router/dist/intentqos_1.0.0-1_all.ipk`), with its config generated from UCI by
`intentqos-uci`. It shaped real traffic in the gateway namespace. Emulation limits, stated plainly:
qemu-user does not translate `PACKET_RX_RING`/`PACKET_VERSION` or `SO_TIMESTAMPNS`, so in that run the
capture helper was the x86-64 build and the estimator used user-space receive timestamps (it now
falls back to them automatically). Emulated CPU figures are not a prediction of real ARM performance;
a run on a physical router (e.g. a Cortex-A53 OpenWrt device) is the next validation step.

## CPU overhead: where the time went and what changed (Nov 2026)

The brief scores CPU overhead, and the full-system emulation showed the router CPU, not memory, as the limit. The
engine was profiled with `py-spy` in the lab under the full household load (video call, ISO download, cloud backup,
three TVs, a game, browsing: about 25 flows at 100/20 Mbit/s), and every process it started was counted with
`strace -e execve`.

| Where the time went (before) | Share of the engine's CPU | Change |
|---|---|---|
| Re-running the tree ensemble on every flow every 0.5 s | ~24 % | an established flow (older than 30 s) is re-predicted every 2 s and its last prediction reused in between; young flows, whose class is still being decided, every tick (`classifier.repredict_s`, `repredict_young_s`). Drift and feedback statistics count each prediction once. |
| Reading HTB counters and verifying each change by starting `tc` (twice: this `tc`'s JSON is unusable, so its text output was parsed as well) | ~20 %, and 16 processes a second | counters and rates read in-process over netlink (`RTM_GETTCLASS`), checked equal to `tc` class by class; `tc` stays the fallback. CAKE already read its counters over netlink; its two `tc qdisc change` calls per change are now one `tc -batch`. |
| ΔQ composition for each app's experience score (Monte Carlo, one random draw per sample per part) | ~18 % | one batch draw per part, summed column-wise (about 5x faster, same distribution); one sort for all percentiles |
| Heartbeat file rewritten every 200 ms tick | ~10 % of what was left | written at an eighth of the watchdog timeout (1 s on a router); watchdog trip and restore timing unchanged (2.0 s after the last heartbeat, safe state in 43 ms) |

Measured A/B on the same lab, same load, alternating the original 2.0.0 engine and this one (two rounds, three 25 s
samples each):

| | Engine CPU (one x86 core) | Processes started per second |
|---|---|---|
| 2.0.0 | 4.3–4.6 % | 16.3 |
| now | **1.9–2.3 %** | **0.1** |

Process starts matter most on a router: each fork and exec of `tc` costs milliseconds of a core that is also
forwarding packets (the emulated MT7981-class router ran at 56 % softirq under load). The emulated router's 61-94 %
engine CPU was measured before these changes; it has not been measured again, and a physical router is the real test.

## Packages

```
make router          # cross-compile iq-capture for aarch64, armv7, mipsel, x86-64 (musl, static)
make ipk             # router/dist/intentqos_1.0.0-1_all.ipk + iq-capture_1.0.0-1_<arch>.ipk
```

`intentqos_…_all.ipk` (324 KiB, 1.2 MB installed) contains the engine, watchdog, agent, console, model,
`/etc/config/intentqos` (UCI), `/etc/init.d/intentqos` (procd: engine, watchdog and agent as three
supervised instances) and `/usr/sbin/intentqos-uci`. It depends on `python3-light`, `tc-full`,
`kmod-sched-cake`, `kmod-sched-htb`, `nftables` and `iq-capture`. `router/openwrt/Makefile` is the
same package as an OpenWrt SDK feed entry (OpenWrt 25.x moves from opkg to apk; the file list is the same).

On prplOS the engine can also run as an LCM container (`router/container/Dockerfile`); it needs
`CAP_NET_ADMIN`, `CAP_NET_RAW` and the host network namespace.

## What a production port would still change

- Capture in eBPF at the TC hook is now built (`capture.backend = ebpf`, see `docs/INNOVATIONS.md`) but
  verified only in emulation on Linux 6.18; it needs testing on router kernels (5.15, 6.6) and SoCs. The
  router SoC's flow-offload counters are the other route.
- The control loop (estimator, policy) in C or Rust, as the community did for cake-autorate
  (C and Rust rewrites exist), to fit 64 MB routers without Python at all.
- Hardware QoS offload where the SoC provides it (the shaper itself is single-threaded in Linux).
