#!/usr/bin/env python3
"""Record everything while the system runs: packets, the router's internals, the engine, the controller, the
emulators and the host. One directory per run; every record carries a wall-clock timestamp.

    capture.py start RUN_DIR [--key ROUTER_KEY]
    capture.py stop RUN_DIR

Streams (all under RUN_DIR):
  pcap/*.pcap           headers (first 128 bytes) on the router's WAN port, wired LAN port, both radio ports
                        and the bridge laptop's Ethernet port, 50 MB rings
  router/sys.jsonl      every 3 s from the router (forks are costly on the emulated router): CPU (total and per core: user/sys/softirq/idle), memory,
                        load, interface counters, top processes every 10 s
  router/tc.jsonl       every 10 s: `tc -s -j qdisc` (the shaper's queues, drops, marks)
  router/syslog.log     logread -f (procd, dnsmasq, netifd, IntentQoS)
  router/events.jsonl   the engine's own event log (/tmp/intentqos/events.jsonl), followed
  engine/*.jsonl        the engine's local API: /api/state and /api/experience every 2 s, /metrics every 10 s
  host/qemu.jsonl       host CPU used by the router VM, every second (to separate emulation cost)
The emulator logs (Wi-Fi events and stats, link traces, DNS queries, package mirror) stay in WORK/logs and
are copied in at stop.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

WORK = os.environ.get("SYSSIM_WORK", "/home/claude/syssim")
SSH = (f"ip netns exec w-ops ssh -i {WORK}/ssh/id_ed25519 -o StrictHostKeyChecking=no "
       f"-o UserKnownHostsFile={WORK}/ssh/known_hosts -o ServerAliveInterval=5 -o ConnectTimeout=5 root@192.168.8.1")

ROUTER_SYS = r'''
# one awk per second (forks are expensive on the router: keep the observer cheap)
i=0
while true; do
  awk -v T="$(date +%s)" -v I=$i '
    FILENAME=="/proc/stat" && /^cpu/ {c=c $0 ";"}
    FILENAME=="/proc/meminfo" && /^(MemTotal|MemFree|MemAvailable|Buffers|Cached|Shmem|SUnreclaim):/ {m=m $1 $2 ";"}
    FILENAME=="/proc/loadavg" {l=$0}
    FILENAME=="/proc/net/dev" && /eth[0-9]|br-lan|ifb/ {n=n $0 ";"}
    FILENAME=="/proc/net/softnet_stat" {s=s $1 " " $2 ";"}
    END {printf "%s|%s|%s|%s|%s|%s|\n", T, c, m, l, n, s}' /proc/stat /proc/meminfo /proc/loadavg /proc/net/dev /proc/net/softnet_stat
  if [ $((i % 6)) = 0 ]; then echo "#top $(date +%s) $(top -b -n1 2>/dev/null | head -14 | tail -8 | tr -s ' ' | tr '\n' ';')"; fi
  i=$((i+1)); sleep 3
done'''
ROUTER_TC = r'''while true; do t=$(date +%s); printf '%s|' "$t"; tc -s -j qdisc 2>/dev/null || tc -s qdisc | tr '\n' ' '; echo; sleep 10; done'''

PROCS: list[subprocess.Popen] = []
STOP = threading.Event()


def spawn(cmd: str, out: str) -> subprocess.Popen:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    p = subprocess.Popen(cmd, shell=True, stdout=open(out, "ab"), stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    PROCS.append(p)
    return p


def router_stream(name: str, script: str, out: str) -> None:
    """Keep a stream from the router running across reboots and outages (reconnects, marks gaps)."""
    while not STOP.is_set():
        with open(out, "a") as f:
            f.write(f"#connect {time.time():.3f}\n")
        p = subprocess.Popen(f"{SSH} sh -s", shell=True, stdout=open(out, "ab"),
                             stderr=subprocess.DEVNULL, stdin=subprocess.PIPE, start_new_session=True)
        PROCS.append(p)
        p.stdin.write(script.encode() + b"\n")
        p.stdin.flush()
        while p.poll() is None and not STOP.is_set():
            time.sleep(0.5)
        if not STOP.is_set():
            with open(out, "a") as f:
                f.write(f"#disconnect {time.time():.3f}\n")
            time.sleep(2)


def parse_sys(run: str) -> None:
    """Convert the raw router lines into JSON records with CPU percentages."""
    src, dst = os.path.join(run, "router", "sys.raw"), os.path.join(run, "router", "sys.jsonl")
    prev = None
    pos = 0
    while not STOP.is_set():
        time.sleep(1)
        try:
            with open(src) as f:
                f.seek(pos)
                lines = f.readlines()
                pos = f.tell()
        except OSError:
            continue
        with open(dst, "a") as o:
            for ln in lines:
                if ln.startswith("#top"):
                    o.write(json.dumps({"t": time.time(), "top": ln[5:].strip()}) + "\n")
                    continue
                if ln.startswith("#") or ln.count("|") < 6:
                    o.write(json.dumps({"t": time.time(), "mark": ln.strip()}) + "\n")
                    prev = None
                    continue
                t, c, m, l, n, s, p = ln.rstrip("\n").split("|")[:7]
                cpus = {}
                for row in c.strip(";").split(";"):
                    f_ = row.split()
                    if f_:
                        cpus[f_[0]] = [int(x) for x in f_[1:9]]
                rec = {"t": float(t), "load": l.split()[:3], "mem_kb": {}, "cpu": {}}
                for kv in m.strip(";").split(";"):
                    if ":" in kv:
                        k, v = kv.split(":", 1)
                        rec["mem_kb"][k.strip()] = int(v.split()[0])
                if prev:
                    for k, v in cpus.items():
                        if k in prev:
                            d = [a - b for a, b in zip(v, prev[k])]
                            tot = sum(d) or 1
                            # user nice system idle iowait irq softirq steal
                            rec["cpu"][k] = {"busy": round(100 * (tot - d[3] - d[4]) / tot, 1),
                                             "user": round(100 * (d[0] + d[1]) / tot, 1),
                                             "sys": round(100 * d[2] / tot, 1),
                                             "softirq": round(100 * (d[5] + d[6]) / tot, 1)}
                prev = cpus
                rec["net"] = n
                if p:
                    rec["top"] = p
                o.write(json.dumps(rec) + "\n")


def engine_poll(run: str, key: str) -> None:
    hdr = {"X-IntentQoS-Key": key, "Authorization": f"Bearer {key}"}
    base = "http://192.168.8.1:8080"
    i = 0
    while not STOP.is_set():
        for path, every in (("/api/state", 1), ("/api/experience", 1), ("/metrics", 5), ("/api/devices", 5)):
            if i % every:
                continue
            cmd = (f"ip netns exec w-ops curl -s -m 3 -H 'X-IntentQoS-Key: {key}' -H 'Authorization: Bearer {key}' "
                   f"{base}{path}")
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            name = path.strip("/").replace("/", "_")
            with open(os.path.join(run, "engine", f"{name}.jsonl"), "a") as f:
                f.write(json.dumps({"t": time.time(), "rc": r.returncode, "body": r.stdout[:200000]}) + "\n")
        i += 1
        time.sleep(2)


def qemu_cpu(run: str) -> None:
    tck = os.sysconf("SC_CLK_TCK")
    prev = {}
    while not STOP.is_set():
        out = {}
        for pidf in ("mt3000.pid", "cudy.pid"):
            try:
                pid = int(open(os.path.join(WORK, "vm", pidf)).read())
                f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                cpu = (int(f[11]) + int(f[12])) / tck
                if pidf in prev:
                    out[pidf.split(".")[0]] = round(100 * (cpu - prev[pidf][0]) / (time.time() - prev[pidf][1]), 1)
                prev[pidf] = (cpu, time.time())
            except (OSError, ValueError):
                pass
        la = open("/proc/loadavg").read().split()[:3]
        with open(os.path.join(run, "host", "qemu.jsonl"), "a") as f:
            f.write(json.dumps({"t": time.time(), "vm_cpu_pct_of_one_host_core": out, "host_load": la}) + "\n")
        time.sleep(1)


def start(run: str, key: str | None) -> None:
    for d in ("pcap", "router", "engine", "host", "console"):
        os.makedirs(os.path.join(run, d), exist_ok=True)
    for dev in ("t3-wan", "t3-lan", "ap5", "ap24", "v-brg"):
        spawn(f"tcpdump -Z root -i {dev} -s 128 -U -C 50 -W 8 -w {run}/pcap/{dev}.pcap", f"{run}/pcap/{dev}.tcpdump.log")
    threads = [
        threading.Thread(target=router_stream, args=("sys", ROUTER_SYS, os.path.join(run, "router", "sys.raw"))),
        threading.Thread(target=router_stream, args=("tc", ROUTER_TC, os.path.join(run, "router", "tc.raw"))),
        threading.Thread(target=router_stream, args=("syslog", "logread -f", os.path.join(run, "router", "syslog.log"))),
        threading.Thread(target=router_stream, args=("events", "mkdir -p /tmp/intentqos; touch /tmp/intentqos/events.jsonl; "
                                                     "tail -F /tmp/intentqos/events.jsonl 2>/dev/null",
                                                     os.path.join(run, "router", "events.jsonl"))),
        threading.Thread(target=parse_sys, args=(run,)),
        threading.Thread(target=qemu_cpu, args=(run,)),
    ]
    if key:
        threads.append(threading.Thread(target=engine_poll, args=(run, key)))
    for t in threads:
        t.daemon = True
        t.start()
    open(os.path.join(run, "capture.pid"), "w").write(str(os.getpid()))
    with open(os.path.join(run, "timeline.jsonl"), "a") as f:
        f.write(json.dumps({"t": time.time(), "event": "capture_start", "key": bool(key)}) + "\n")

    def bye(*_):
        STOP.set()
        for p in PROCS:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    while True:
        time.sleep(1)


def stop(run: str) -> None:
    try:
        os.kill(int(open(os.path.join(run, "capture.pid")).read()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    time.sleep(2)
    os.makedirs(os.path.join(run, "emulators"), exist_ok=True)
    for f in os.listdir(os.path.join(WORK, "logs")):
        shutil.copy(os.path.join(WORK, "logs", f), os.path.join(run, "emulators", f))
    for f in ("wifiemu.conf.events.jsonl", "wifiemu.conf.stats.jsonl", "ont.leases", "phone4g.leases"):
        if os.path.exists(os.path.join(WORK, f)):
            shutil.copy(os.path.join(WORK, f), os.path.join(run, "emulators", f))
    with open(os.path.join(run, "timeline.jsonl"), "a") as f:
        f.write(json.dumps({"t": time.time(), "event": "capture_stop"}) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "engine":          # engine API poller only (once the router key exists)
        os.makedirs(os.path.join(sys.argv[2], "engine"), exist_ok=True)
        open(os.path.join(sys.argv[2], "engine.pid"), "w").write(str(os.getpid()))
        engine_poll(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "start":
        k = sys.argv[sys.argv.index("--key") + 1] if "--key" in sys.argv else None
        start(sys.argv[2], k)
    else:
        stop(sys.argv[2])
