# Full-system emulation (systemsim)

The whole Round 2 hardware setup on one Linux host, built to be as close to the real thing as an emulator
allows, and used to run the complete system test with everything recorded. Results:
`results-systemsim/` (report, timeline, metrics, router internals, screenshots, packet headers).

## What is real and what is emulated

| Part | In this emulation | Real hardware it stands for |
|---|---|---|
| Router firmware | **Real**: official OpenWrt 25.12.6 release image (armsr/armv8, kernel 6.12.112, musl, procd, apk, fw4/nftables, dnsmasq, odhcpd, sysntpd) | GL-MT3000's OpenWrt (same version line, same aarch64 userland) |
| Router hardware | QEMU aarch64 VM: 2 x Cortex-A53, 512 MB RAM, 104 MB disk with squashfs + overlay, NAND-like I/O throttling, no battery clock | MT7981B (2 x A53 1.3 GHz), 512 MB, 256 MB NAND |
| Router radios | Two virtual Ethernet ports bridged into br-lan (as phy0/phy1 are) | MT7976 2.4/5 GHz radios |
| IntentQoS install | **Real**: Router Setup (Linux build) over SSH, downloading the real OpenWrt packages | Router Setup on Windows |
| Operator Console, Test Kit, 4G Bridge | **Real programs** (Linux builds of the same Go code), driven through their windows by a browser robot | Windows builds |
| Laptops, phone, TVs, console, desktop | Linux network stacks in namespaces with their own MACs and DHCP identities (Windows, Android, webOS, PlayStation vendor classes; the phone uses a private MAC) | the household's devices |
| Wi-Fi | `wifiemu.c`: layer-2 air between the radio ports and stations: airtime caps (5 GHz 400, 2.4 GHz 100 Mbit/s less neighbour busy time), lognormal access delay with contention, stall episodes, background scans, phone power-save, loss | home Wi-Fi in an Indian apartment block |
| Fibre line | `testbed/accessemu.c` + tbf: plan rate, 50 ms modem FIFO, GPON upstream wait, jitter, burst loss, evening capacity dips | FTTH (Jio/Airtel/BSNL-like) |
| ISP CPE and ISP | JioFiber-style ONT/router (192.168.29.0/24, DHCP, NAT) then ISP CGNAT: the router is double-NATed, as at home | ISP router + carrier-grade NAT |
| 4G backup | LTE core with rate trace (median 20/8 Mbit/s, varying every ~2 s), 300 ms modem buffer, +15 ms delay, jitter, 0.5 % loss, CGNAT; the phone does Android USB tethering (rndis, 192.168.42.0/24) | a phone on Jio/Airtel 4G |
| Bridge laptop | 4G Bridge program with the phone seen as `rndis_host` and a `r8152` USB Ethernet port; NetworkManager's "shared" mode is reproduced by a small `nmcli` stand-in | laptop + TP-Link UE300 |
| Internet | Servers at public-style addresses: resolvers (1.1.1.1, 8.8.8.8, 9.9.9.9), NTP, a speed-test endpoint, media/game/CDN servers, and a caching mirror of downloads.openwrt.org that serves the real release packages | the internet |

## Calibration and its limits

* CoreMark on the router VM: 6,205 per core. A real Cortex-A53 at 1.2 GHz scores about 3,800 (Raspberry Pi 3),
  so about 4,100 at the MT7981's 1.3 GHz: the VM computes about 1.5 x faster than the real chip.
* Packet forwarding is the other way round. Through virtio under TCG, the VM shaped with CAKE reached 42 Mbit/s on
  a 100 Mbit/s line, 34 on 50, 24 on 30, with the router's CPU above 90 %. A real MT7981 is reported to shape
  about 200 Mbit/s with CAKE (OpenWrt forum). So the emulated line is a **30/30 Mbit/s plan** (India's entry fibre
  tier), where the VM still has headroom; the field profile's 100/100 would test the emulator, not IntentQoS.
* Process creation and page faults are far more expensive under TCG than on the chip, which magnifies anything
  that forks often. That is how the "tc seven times a second" cost was found (see the report).
* Not modelled: radio signal strength and rate adaptation, roaming, DFS, PPPoE, IPv6, hardware flow offload,
  the GL.iNet firmware's own services. The router reports its model as "QEMU QEMU Virtual Machine".

## Running it

```
sudo python3 systemsim/world.py up            # namespaces, links, emulators, servers
systemsim/vm/run_router.sh ...                # see world.py VM table; first boot settings: systemsim/firstboot.sh
systemsim/dhcp.sh                             # household devices lease addresses
python3 systemsim/capture.py start RUN        # everything recorded (see capture.py)
# kit programs: intentqos-console in w-ops, router setup (ui_router_setup.py), phone (ui_phone.py),
# Test Kit (start_testkit.sh + ui_testkit.py), 4G bridge (start_bridge.sh + ui_bridge.py), cable.sh
systemsim/restore.sh                          # rebuild everything after the host restarted
systemsim/cablesvc.sh &                       # host-side cable mover (failover); laptops ask via cablereq.sh
systemsim/failover_run.sh on|off              # Test Kit guided failover + a call and pings as ground truth
python3 systemsim/household.py RUN/household  # an evening at home, IntentQoS off then on, ground truth
python3 systemsim/resilience.py RUN/resilience
python3 systemsim/report.py CAPTURE_DIR results-systemsim   # charts, summary, evidence
```
