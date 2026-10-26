# Setup guide

Two ways to run IntentQoS: the **testbed** (fully reproducible, one Linux machine or VM,
no extra hardware) and a **real gateway** (a Linux box routing between your modem and LAN).

## Prerequisites (exact versions validated)

| Component | Version validated | Notes |
|---|---|---|
| Linux kernel | 6.18.44 (also expected to work on ≥ 5.15) | needs `sch_htb`, `sch_tbf`; uses `sch_cake`, `sch_fq_codel`, `sch_netem` when present |
| Distribution | Ubuntu 24.04.4 LTS | any distro with the tools below |
| iproute2 (`ip`, `tc`) | 7.2.0 | `tc -j` JSON output is required |
| nftables | 1.0.9 | verdict maps with `flags timeout` and `th` expressions |
| Python | 3.11.15 | 3.11+ (uses `tomllib`) |
| numpy | 2.4.4 | |
| scikit-learn | 1.8.0 | training only; the engine runs the exported `models/flowclf.iqm` in pure Python |
| scipy | 1.17.1 | report statistics |
| matplotlib | 3.10.9 | report figures |
| joblib | 1.5.3 | training-time model bundle (fallback if no `.iqm`) |
| Root access | — | network namespaces, tc, nftables, raw ICMP sockets |

```bash
sudo apt install iproute2 nftables python3 python3-pip
pip install -r requirements.txt          # add --break-system-packages on Debian/Ubuntu system Python
```

## A. Testbed (recommended for evaluation)

One command:

```bash
sudo make lab                # testbed + engine + watchdog + lab server
#   console: http://localhost:8090/   (Operator and Household views, lab controls, experiments)
```

`make lab` runs `testbed/labd.py` in the root namespace. It creates the four namespaces if they are
missing (clients, gateway, ISP modem at 100/20 Mbit/s with a bloated buffer, server), starts the engine
and the watchdog in the gateway, serves the console, and proxies `/api/*` to the engine. Its own
`/lab/*` endpoints let the console start traffic presets, change the access link, crash or restart
the engine, and run scenarios (each run stops live traffic and the live engine, then restarts it).

Step by step instead:

```bash
sudo make testbed            # 4 namespaces: clients, gateway, ISP modem (100/20 Mbit/s, bloated buffer), server
sudo make engine             # start IntentQoS inside the gateway namespace (Ctrl-C to stop)
#   console: http://10.255.0.2:8080/   (from the host; the classic dashboard is at /classic)
sudo make watchdog           # second terminal
sudo make demo               # third terminal: scripted 2-minute live demo (see docs/DEMO_SCRIPT.md)
```

To record a new replay for people without Linux: with the lab running, `make replay` records 150 s
of a scripted evening and writes `docs/console-replay.html`, a single file that opens in any browser.

Full reproduction of the evidence report:

```bash
sudo make dataset            # ~25 min: labelled header-only flow dataset  -> ml/data/dataset.jsonl
make train                   # ~1 min : model + docs/CLASSIFIER_REPORT.md   -> models/flowclf.joblib
sudo make eval SEEDS=10      # ~2.5 h : all scenarios x arms x seeds       -> results/runs/
make report                  #          statistics + figures               -> results/REPORT.md, report.html
make test                    #          unit tests (no root needed)
sudo make clean-testbed
```

### Verification steps

The console's **System → Setup check** runs steps 2 to 4 below against the live gateway and shows
each result with the command it used (`GET /api/setup-check`). By hand:

1. `sudo python3 testbed/testbed.py status` shows the four namespaces and whether
   CAKE / fq_codel / netem are available on your kernel.
2. With the engine running: `sudo ip netns exec iq-gw tc -s qdisc show dev gw-lan`
   shows a `cake` (or `htb`) root; `sudo ip netns exec iq-gw nft list table inet intentqos`
   shows the class maps filling as traffic starts.
3. `curl -s http://10.255.0.2:8080/api/healthz` returns `{"ok": true, ...}`;
   `curl -s http://10.255.0.2:8080/metrics | head` shows Prometheus gauges.
4. Kill the engine with `kill -9`: with `make watchdog` running, the event log shows
   "restored safe ..." within ~2.5 s and `tc` shows the safe tree.
5. `make test` prints `OK`.

## B. Real gateway

**OpenWrt / prplOS routers:** install the packages from `make router ipk` with `opkg` and configure
through UCI; see [DEPLOYMENT.md](DEPLOYMENT.md#1-router). The steps below are for a generic Linux box.
For the copy below, also install the capture helper: `sudo cp router/bin/iq-capture.x86_64 /usr/sbin/iq-capture`
(or set `[capture] helper_path`).

Topology: modem/ONT — **(wan) Linux box (lan)** — home switch / Wi-Fi AP.
The box must already route (or bridge) between the two NICs.

```bash
sudo cp config/example-router.toml /etc/intentqos.toml
sudoedit /etc/intentqos.toml          # set interfaces, LAN subnets, plan rates, reflectors, nat = true
sudo install -d /opt/intentqos && sudo cp -r intentqos models /opt/intentqos/
sudo cp deploy/intentqos.service deploy/intentqos-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now intentqos intentqos-watchdog
```

- Set `[link] down_mbit / up_mbit` to your plan. The estimator never shapes above 95 %
  of these and never below 10 %.
- Reflectors: use 3+ hosts that answer ICMP (the defaults are public anycast resolvers).
  Hosts that ignore ICMP timestamp requests are switched to echo (RTT) automatically.
- If the box does NAT, set `[enforce] nat = true` so CAKE's per-host fairness sees
  internal addresses.
- Optional API token: `sudo systemctl edit intentqos` and add
  `Environment=INTENTQOS_TOKEN=<your own random string>`. Never commit it.
- To uninstall: `sudo systemctl disable --now intentqos intentqos-watchdog`; the engine
  leaves the configured exit state (`[watchdog] exit_state = "passthrough"` removes all shaping).
