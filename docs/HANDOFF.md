# Handoff: changes made in the November 2026 review session

Written for the next Claude session (or person) picking this project up. It lists everything this session changed,
why, how it was verified, what was deliberately left alone, and what is still open. Read it before touching the
engine, the kit, CI or the docs.

- **Branch:** `claude/ecstatic-cannon-9vgzna`, five commits on top of `d473231` (the last commit on `main` and
  `device-kit`, which were identical). Diff: 49 files, +1068 / −138.
- **Not merged** into `main`; no pull request was opened.
- **Tests:** 134 unit tests, all passing (1 skipped: needs scikit-learn). CI was red on every push before this work;
  the first CI run with these changes (run 8) passed every job.

```
5ef8eeb  Fix device identity across restarts and hook rotation, stricter boost offers, CI and doc drift
5d1c0c1  Dual stack: one device per MAC across IPv4 and IPv6; IPv6 fragment and AH headers in all three parsers
2ebbae0  IPv6 reflectors for the capacity estimator; never merge devices that share a MAC
2e47478  Halve engine CPU, follow the ISP-delegated IPv6 prefix, document IPv4/IPv6 and CPU honestly
f6f447e  Rebuild router capture helpers with the IPv6 extension-header parsing; record the quality A/B
```

## Context

- Project: IntentQoS, Tata Elxsi TELIPORT Season 4, **Case Study 3** (adaptive QoS engine for mixed home broadband
  traffic). Round 1 is cleared.
- **Round 2 submission:** a 10-slide deck (`.ppt`/`.pptx`/`.pdf`, under 5 MB, named
  `LeaderFirstName_TeamName_InstituteName`) and a 3-minute MP4 video (under 50 MB) showing both the solution and the
  presenter. For Case Study 3: "build and demonstrate your solution on Linux platform". Hardware is not required;
  emulation is accepted if labelled.
- The case-study brief scores: classification and link-estimation accuracy; reduction in latency, jitter and loss;
  fairness and absence of starvation; adaptation speed and stability; **CPU overhead**, reproducibility and clarity.
  It also asks for IPv4 and, where available, **IPv6**.
- The user asked this session to apply every fix from the full review **except the login and security fixes**,
  then to research and improve IPv4/IPv6 handling and CPU overhead.

## 1. Bug fixes from the review

| Fix | Where | Detail |
|---|---|---|
| Device identities not saved after boot | `intentqos/identity.py:93`, `_save_cache` at `:150` | The flash cache was rate-limited with `time.monotonic() - 0.0 < 600`. The monotonic clock starts at boot, so nothing was written in the first 10 minutes after a power cut, the exact case the cache was added for. It also failed the unit test on fresh CI runners. `_cache_t` now starts as `None`. |
| New devices never identified after the hook trimmed its file | `intentqos/identity.py:111` (`_read_hook`) | The DHCP hook (`router/kit/files/etc/hotplug.d/dhcp/90-intentqos`) rotates its file with `tail -n 500 > tmp && mv`. The engine kept its old byte offset, read past the end forever, and never saw later devices. Now: track the inode, restart from 0 on a new inode or a shorter file, read in binary, never consume a half-written line. The engine no longer truncates the file itself (the old read-then-truncate could lose lines). |
| Wrong boost offers | `intentqos/offers.py:22-23` | An app must be seen under the same label for 20 s **and** carry real two-way traffic (`MIN_KBIT`: voip 16/16, video 100/50, gaming 8/8 kbit/s). In the full-system test 12 of 13 offers were the Test Kit's DNS probes or a ping. |
| Test Kit delay percentiles silently skipped lost probes | `kit/testkit_metrics.go` (`quality.Answered`, `Lost`), `kit/web/report.html` | Counts are now in the JSON and the report says lost probes count as loss, not delay. |
| CI red on every push | `.github/workflows/ci.yml` | numpy installed for `TestAnalysis`; emulators compiled with `-Werror`; new **kit** job (gofmt check, `go vet` of all four programs for Linux and Windows with placeholder embed files, `go test -race -tags testkit`); Go 1.24 everywhere; actions moved off Node 20 (`checkout@v5`, `setup-python@v6`, `setup-go@v6`, `upload-artifact@v5`). |
| Test skips instead of failing without numpy | `tests/test_modules.py` | `TestAnalysis` is `skipIf(np is None)`. |
| Small cleanups | various | gofmt over `kit/` and `launcher/go/` (whitespace only; the files were already unformatted); misleading-indentation warnings in `testbed/accessemu.c` and `systemsim/wifiemu.c`; unclosed files in `tests/test_router_stdlib.py`. |
| Doc and version drift | `README.md`, `pyproject.toml`, `docs/KIT.md`, `kit/build.py` | Test count (134), watchdog timeout explained (2 s in the old results, 8 s now), package version 2.0.0, Go 1.24. |

## 2. IPv4 and IPv6 (new work)

Findings that drove this: everything in the engine was keyed by IP address, and the router's capture had only the
IPv4 subnet (the full-system evidence shows `iq-capture ... -n 192.168.8.0/24` only).

| Change | Where | Detail |
|---|---|---|
| **New module `neighbours.py`** | `intentqos/neighbours.py` | `parse_neigh` / `neigh_netlink` (`:36`, `:61`): one netlink `RTM_GETNEIGH` dump for both families, `ip neigh` as fallback. `DeviceKeys` (`:130`): ip → device key. An IPv6 address joins the IPv4 device with the same MAC **only when that MAC has exactly one IPv4 address**. IPv4 addresses are never merged with each other (Wi-Fi range extenders in repeater mode and the lab's client namespace put many devices behind one MAC). IPv6-only devices are not grouped. |
| Device key on every flow | `intentqos/capture.py` (`Flow.dev` slot), `intentqos/daemon.py:91`, `:435` | `f.dev` defaults to the LAN address and is set from `DeviceKeys` each policy step (table re-read every 5 s). |
| Everything per device | `policy.py` (lease → `f.dev`), `daemon.py` (`_devices`, `_device_measure`, console flow list `device_key`), `experience.py`, `manage.py`, `wifi.py`, `classifier.py` (`_session_key`) | A lease on the laptop's IPv4 now covers its IPv6 call. The device list has one row per device with `addresses` and `ipv6` fields; the console card shows "IPv4 + IPv6 (n addresses)". |
| Pause covers IPv6 | `daemon.py:777` (`_sync_pauses`) | A paused device's other addresses are added to `paused6` and removed when the pause ends; called on each neighbour refresh and from `pause_device` / `resume_device`. |
| Delegated prefix found at run time | `neighbours.py:87` (`lan_prefixes6`), `:110` (`merge_subnets`); `daemon.py:746` (`_follow_lan6`); `config.py` `interfaces.lan_ipv6_auto` (default true) | Global and ULA prefixes are read from `/proc/net/if_inet6` for the LAN interface at start and every 30 s. On a change, the flow table's subnets are updated, and the capture helper is restarted with the new `-n` list and re-attached to the marker (kernel-engine verdicts re-pushed). |
| IPv6 extension headers | `capture.py` (~`:173`), `router/iq-capture.c:210`, `router/iq-flow.bpf.c:170` | Fragment (44) and AH (51) headers were not walked. Fragmented or AH-protected packets became bogus "protocol 44/51" flows, losing their ports. Non-first fragments are now counted without reading payload as ports, identically in all three parsers. eBPF objects (`router/iq-flow*.bpf.o`) and static router helpers (`router/bin/iq-capture.*`, zig cc) rebuilt. |
| IPv6 reflectors | `intentqos/estimator.py` (`Prober`, `:82`, `_rx` at `:166`); defaults in `config.py`, `uciconf.py`, `router/openwrt/files/intentqos.config`, `config/example-router.toml` | `2606:4700:4700::1111` and `2001:4860:4860::8888` probed with ICMPv6 echo on a separate raw socket (round trip only; ICMPv6 has no timestamp message). Optional: with no IPv6 on the router they are reported as `unused` in `info()` and IPv4 carries on. `config/testbed.toml` is unchanged (IPv4 reflector only). |

Tests: `tests/test_dualstack.py` (12 tests: kernel-format netlink messages, grouping, extender case, DHCP-only MAC,
lease and pause over IPv6, per-device list, ICMPv6 echo-reply parsing via a socket pair, prefix discovery, prefix
change restarting the capture) and `tests/test_capture_helper.py::test_same_aggregates_ipv6` (C helper vs Python
on raw frames with every extension header; needs root).

## 3. CPU overhead (new work)

Profiled with `py-spy` in the lab under the full household load (call, ISO download, backup, three TVs, game,
browsing; about 25 flows at 100/20 Mbit/s); process starts counted with `strace -e execve` (count only `= 0` lines:
Python tries each PATH directory, so raw counts are inflated).

| Change | Where | Was |
|---|---|---|
| Established flows (≥ 30 s) re-predicted every 2 s, young ones every tick; statistics (drift, feedback, history) count each prediction once | `classifier.py:248-250`, config `classifier.repredict_s` / `repredict_young_s` | ~24 % of engine CPU |
| HTB counters and read-back rates over netlink (`RTM_GETTCLASS`), checked equal to `tc` class by class; `tc` is the fallback | `enforce.py:177` (`class_stats_netlink`), `HtbBackend.classes` | ~20 %, 16 processes/s |
| CAKE's two `tc qdisc change` per change → one `tc -batch`; HTB bfifo leaf re-sizing for both directions → one `tc -batch` | `enforce.py:486`, `:374` (`run_batch`), `daemon.py` policy step | 2 processes per change |
| ΔQ composition draws per part with `choices` (5x faster, same distribution); one sort per percentile set | `deltaq.py:69`, `:50` | ~18 % |
| Heartbeat written at timeout/8 (1 s on routers, 250 ms in the testbed) | `daemon.py:545` | every 200 ms tick |

Measured A/B (old `d473231` vs new, same lab, same load, alternating): engine **4.3–4.6 % → 1.9–2.3 %** of one x86
core; process starts **16.3/s → 0.1/s**. Capture helper 0.2 %, watchdog 0.2 %. Watchdog after `kill -9`: tripped
2.0 s after the last heartbeat, safe state in 43 ms (unchanged).

Quality check, ML arm, same lab: scenario 1 with 8 seeds per engine, call delay p99 25.7 vs 25.7 ms down
(Mann-Whitney p = 0.75), 14.4 vs 16.0 ms up (p = 0.67), ISO/backup/page load unchanged; scenario 3 with 3 seeds,
unchanged, no rebuffering or starvation. After only 3 seeds scenario 1 looked 10 ms worse; the 5 extra seeds showed
it was noise. Do not quote the 3-seed numbers.

## 4. Docs changed

- `docs/KNOWN_LIMITATIONS.md`: new **IPv4 and IPv6** section (table of what is done and how verified, plus limits);
  router CPU named as the open risk.
- `docs/ROUTER_PROFILE.md`: new section on the CPU work, the A/B, and the quality check.
- `docs/MODULES.md`: `neighbours.py` row; capture inputs (IPv6 prefixes); estimator inputs (ICMPv6, netlink
  counters); enforcement transaction (netlink read-back, one batch).
- `README.md`: new "IPv4 and IPv6 as one home" bullet; gateway CPU figures replaced with the full-load measurement.
- `docs/JUDGES.md`: router paragraph updated (CPU, IPv6 status).

## 5. Deliberately not done

The user excluded login and security fixes. These findings from the review are still open:

- Subscriber-app OTP can be brute-forced: asking for a new code resets the attempt counter
  (`controller/server.py`, `/v1/app/otp` and `/v1/app/login`).
- Kit pairing does not authenticate the console: discovery is unauthenticated UDP and pairing uses TLS with
  verification off (`kit/common.go`, `consoleClient`), so a LAN attacker can relay the PIN and substitute its own CA.
- Pairing as a Test Kit returns every router key (`controller/kit.py`, `routers(with_keys=True)`); the PIN is not
  rotated after use; the 5-wrong-PIN lockout is global (anyone on the LAN can keep pairing locked).
- Windows firewall rules use `profile=any` (`kit/console.go`); should be private.
- `kit/build.py` downloads python-build-standalone without a pinned SHA-256.
- Router key sent over plain HTTP to the router console; gateway API sessions never purged and not locked.

## 6. Still open (not security)

- **No IPv6 traffic has been shaped live.** Every lab kernel, including the one behind all committed results
  (`results*/environment.json`: `"ipv6": false`), had IPv6 disabled. The first real test is a dual-stack ISP line.
- **CAKE fairness is per address, not per device** (`dual-dsthost`/`dual-srchost`). Fix design: CAKE's host-hash
  override from a tc filter (classid major = device id) keyed by `DeviceKeys`. Not built: this lab has no CAKE.
- **Router CPU:** the emulated router's 61–94 % engine CPU was measured before the CPU work and has not been
  re-measured. Day 1 on a GL-MT3000 should check engine CPU and delivered rate at full load.
- The committed `results/` and `results-india_ftth_evening/` were produced by the old engine; they were not re-run.
- Not touched: `launcher/` (Windows WSL launchers), `controller/` behaviour, the systemsim harness.

## 7. Working in this environment (useful for the next session)

- The container kernel has **no IPv6, no CAKE, no fq_codel, no netem**. The testbed still runs (HTB backend, bfifo
  leaves, user-space delay line): `python3 testbed/testbed.py up --down 100 --up 20`, then
  `python3 -m testbed.labd --presets call,browse` (console on :8090; `POST /lab/traffic {"preset":"iso","action":"start"}`
  etc.). Tear down with `python3 testbed/testbed.py down`.
- This session installed `iproute2`, `nftables`, `iputils-ping`, `strace`, `bc` (apt) and `py-spy`, `ziglang` (pip).
  A fresh container will not have them.
- Root is available, so `tests/test_capture_helper.py` and `tests/test_kernel_engine.py` run here once
  `router/iq-capture` is built (`gcc -O2 -Wall -Wextra -o router/iq-capture router/iq-capture.c`) and `tc` exists.
- Cross-compiling helpers: `python3 -m ziglang cc -target aarch64-linux-musl -O2 -static -s -Wall -o router/bin/iq-capture.aarch64 router/iq-capture.c`
  (see the `router` target in the `Makefile` for the other three).
- `router/dist/` is git-ignored; packages are rebuilt by CI and `kit/build.py`. Commit `router/bin/*` and the
  `.bpf.o` files when the C sources change.
- When comparing engine versions, use `git worktree add /tmp/... <commit>` and run
  `python3 -m testbed.run_eval --reuse-testbed --scenarios ... --arms adaptive_ml --seeds N --out DIR` from each tree.
  Use at least 8 seeds for scenario 1: 3 seeds gave a false 10 ms difference.

## 8. Suggested next steps for Round 2

1. Decide whether to merge this branch into `main` (judges may look at the repo; CI is green here).
2. Build the 10-slide deck and the 3-minute video (Linux lab footage plus presenter). The video is fully designed
   in `docs/ROUND2_VIDEO_PLAN.md` (timeline, clip list, presenter script, layout from the organiser's sample, export). Suggested slide outline: problem;
   solution in one picture; architecture on Linux; header-only classification (99.3 % vs 66.9 % for rules); capacity
   estimation and failover; temporary intent; results before/after; real-firmware system test (8 bugs found);
   IPv4/IPv6 and CPU (this session); limits and roadmap.
3. Optional hardware: GL.iNet GL-MT3000 with upstream OpenWrt on the team's real line, plus a USB Ethernet adapter as a
   plan B (an Ubuntu laptop as the gateway, `hwtest/README.md`). Run a Waveform bufferbloat test on the line first;
   measure engine CPU and IPv6 on day 1.
4. The security items in section 5, if the user asks for them.
