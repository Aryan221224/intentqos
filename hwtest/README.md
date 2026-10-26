# Hardware trial: IntentQoS on a real gateway and a real broadband line

Goal: replace the slide's *projected* "real line + hardware" line with a *measured* one. One evening, two
people. The result is latency under load (ping p99 while the line is full), with and without IntentQoS, on the
same hardware and the same line.

## What you need

- **Gateway:** a Raspberry Pi 4 or 5 (2 GB is enough) with a USB 3 Gigabit Ethernet adapter, or any Linux
  laptop/PC with two Ethernet ports (one can be USB). An OpenWrt router works too (see `docs/DEPLOYMENT.md`).
- Two Ethernet cables, the home broadband router, and a test laptop (Windows, macOS or Linux, Python 3).
- Optional: a spare Wi-Fi router set to access-point mode, to test with the laptop on Wi-Fi.

```
ISP router / ONT ──(LAN port)── eth0 [ Pi: IntentQoS ] eth1 ──── test laptop (cable, or Wi-Fi AP)
```

The Pi becomes a small router behind the home router (double NAT is fine for this test).

## Steps

1. **Install** Raspberry Pi OS Lite 64-bit (Bookworm, Python 3.11). Then:
   `sudo apt update && sudo apt install -y nftables iproute2 dnsmasq git` and copy this repository to the Pi.
2. **Plain router first (this is the "no QoS" arm).** Give eth1 `192.168.50.1/24`, enable forwarding
   (`sysctl -w net.ipv4.ip_forward=1`), masquerade eth0 with nftables, and hand out addresses on eth1 with
   dnsmasq (`interface=eth1`, `dhcp-range=192.168.50.20,192.168.50.100,12h`). Connect the laptop and check
   that it browses.
3. **Measure your line once** at an idle hour (fast.com or speedtest.net from the laptop) and note the
   download and upload rates.
4. **No QoS run.** On the laptop, in `hwtest/`: `python3 latency_test.py --label noqos --note "plan, wired/Wi-Fi"`.
   Repeat 3 times. Nobody else should use the line during the test.
5. **IntentQoS run.** On the Pi copy `config/example-router.toml` to `/etc/intentqos.toml` and set
   `lan = "eth1"`, `wan = "eth0"`, `lan_subnets = ["192.168.50.0/24"]`, `down_mbit` / `up_mbit` to the rates
   from step 3, `nat = false` (step 2 already masquerades) and `listen = "192.168.50.1:8080"`. Start it with
   `sudo python3 -m intentqos.daemon --config /etc/intentqos.toml` (leave it running) and check the console
   at http://192.168.50.1:8080. Then on the laptop: `python3 latency_test.py --label intentqos`, 3 times.
6. **CPU on the gateway** during an IntentQoS download phase:
   `top -b -d 5 -n 6 -p $(pgrep -f intentqos.daemon | head -1)`; note the typical %CPU.
7. **Stop the engine** (Ctrl-C) and confirm the plain router still works (the watchdog restores a safe state).
8. **Summarise** on any machine:
   `python3 summarize.py --noqos results/noqos-*.json --intentqos results/intentqos-*.json --device "Raspberry Pi 4" --line "100 Mbps fibre, laptop wired" --cpu 9 --out hardware.json`
   and send `hardware.json` plus the `results/` folder. The slide rebuilds from it and then says
   "Measured on a Raspberry Pi 4 …" with your numbers.

## Notes

- Windows pings once a second, so a 60 s phase gives about 60 samples: p99 is then close to the maximum.
  Linux and macOS ping 5 times a second (macOS may need `sudo` for that).
- The load comes from Cloudflare's public speed-test endpoints. If they are blocked on your network, run
  any large download (an OS update ISO) and a cloud upload by hand during the load phases.
- Keep the runs honest: same time window, same laptop position, three repeats per arm, and keep every
  result file. Report what you measured, including a run where IntentQoS does not help.
- IntentQoS shapes the wired path; Wi-Fi airtime is not managed. Test wired first, then Wi-Fi if you can.
