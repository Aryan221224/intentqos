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
| **Router build as aarch64 + musl**, under qemu-user emulation | 21–30% of the *host* core, emulation included | 54–56 MB, qemu included | all flows correct |

The aarch64 run used a static musl Python (the same C library as OpenWrt) and the **installed
`.ipk` contents** (`router/dist/intentqos_1.0.0-1_all.ipk`), with its config generated from UCI by
`intentqos-uci`. It shaped real traffic in the gateway namespace. Emulation limits, stated plainly:
qemu-user does not translate `PACKET_RX_RING`/`PACKET_VERSION` or `SO_TIMESTAMPNS`, so in that run the
capture helper was the x86-64 build and the estimator used user-space receive timestamps (it now
falls back to them automatically). Emulated CPU figures are not a prediction of real ARM performance;
a run on a physical router (e.g. a Cortex-A53 OpenWrt device) is the next validation step.

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

- Classification and capture in eBPF at the TC hook (per-flow maps in the kernel), so even the
  0.5-second aggregation leaves user space; or the router SoC's flow-offload counters.
- The control loop (estimator, policy) in C or Rust, as the community did for cake-autorate
  (C and Rust rewrites exist), to fit 64 MB routers without Python at all.
- Hardware QoS offload where the SoC provides it (the shaper itself is single-threaded in Linux).
