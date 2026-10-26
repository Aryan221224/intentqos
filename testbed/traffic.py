#!/usr/bin/env python3
"""Traffic generators and sinks for the IntentQoS testbed.

Every realtime packet carries a sequence number and a send timestamp, and the
receiver uses *kernel* receive timestamps (SO_TIMESTAMPNS), so one-way delay,
jitter and loss are measured exactly (all namespaces share one clock). Nothing
here is used by the QoS engine itself -- the engine never sees these labels or
timestamps; they are ground truth for evaluation only.

Flow kinds
  voip      UDP CBR, 50 pps x 172 B each way (G.711, 20 ms packetisation)
  video     UDP video-conference: 30 fps frames, ~1.2-2.5 Mbit/s each way
  gaming    UDP, ~60 pps small packets up, ~30 pps medium packets down
  bulk      TCP, N parallel connections (ISO download / cloud backup upload)
  abr       TCP adaptive-bitrate streaming player (chunked, ON/OFF, buffer model)
  web       TCP short request/response bursts (interactive browsing)

Server:  python3 -m testbed.traffic server --out DIR
Client:  python3 -m testbed.traffic voip --src 192.168.50.10 --dst 203.0.113.10 ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import socket
import socketserver
import struct
import sys
import threading
import time

UDP_PORT = 9003
TCP_PORT = 9001
MAGIC = 0x1A51
HDR = struct.Struct("!HIIQHHHHHH")  # magic, flowhash, seq, send_ns, kind, up_rate, up_size, dur, dn_rate, dn_size
KIND = {"voip": 1, "video": 2, "gaming": 3}
KIND_NAME = {v: k for k, v in KIND.items()}
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
SCM_TIMESTAMPNS = SO_TIMESTAMPNS
MAX_UDP_PAYLOAD = 1200
HAVE_V6 = os.path.exists("/proc/net/if_inet6")


def flow_hash(name: str) -> int:
    return int.from_bytes(hashlib.sha1(name.encode()).digest()[:4], "big")


def _family(addr: str) -> int:
    return socket.AF_INET6 if ":" in addr else socket.AF_INET


def _enable_rx_timestamps(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)


def _recv_ts(sock: socket.socket, bufsize: int = 2048):
    """recvmsg returning (data, addr, kernel_rx_ns)."""
    data, anc, _flags, addr = sock.recvmsg(bufsize, socket.CMSG_SPACE(16))
    rx = None
    for level, typ, cdata in anc:
        if level == socket.SOL_SOCKET and typ == SCM_TIMESTAMPNS and len(cdata) >= 16:
            sec, nsec = struct.unpack("qq", cdata[:16])
            rx = sec * 1_000_000_000 + nsec
    if rx is None:
        rx = time.time_ns()
    return data, addr, rx


def _sleep_until(t: float) -> None:
    d = t - time.perf_counter()
    if d > 0:
        time.sleep(d)


def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Realtime packet scheduling (shared by client upstream and server downstream)
# ----------------------------------------------------------------------------
def realtime_schedule(kind: int, rate: int, size: int):
    """Yield (offset_seconds, packet_sizes) bursts for a realtime flow.

    voip/gaming: `rate` = packets/s, `size` = bytes per packet.
    video:       `rate` = frames/s, `size` = bytes per frame (split into <=1200 B).
    """
    if kind == KIND["video"]:
        period = 1.0 / max(1, rate)
        i = 0
        while True:
            # frame-size variation: I-frame every 2 s is ~3x a P-frame
            f = size * (3.0 if i % (rate * 2) == 0 else 0.9)
            pkts = []
            rem = int(f)
            while rem > 0:
                p = min(MAX_UDP_PAYLOAD, rem)
                pkts.append(max(HDR.size, p))
                rem -= p
            yield i * period, pkts
            i += 1
    else:
        period = 1.0 / max(1, rate)
        i = 0
        while True:
            yield i * period, [max(HDR.size, size)]
            i += 1


def send_realtime(sock, dst, fh, kind, rate, size, duration, dn_rate, dn_size, stop=None):
    seq = 0
    t0 = time.perf_counter()
    pad = bytes(MAX_UDP_PAYLOAD)
    sent = 0
    for off, burst in realtime_schedule(kind, rate, size):
        if off >= duration or (stop is not None and stop.is_set()):
            break
        _sleep_until(t0 + off)
        for psize in burst:
            hdr = HDR.pack(MAGIC, fh, seq, time.time_ns(), kind, rate, size, int(duration), dn_rate, dn_size)
            try:
                if dst is None:
                    sock.send(hdr + pad[: psize - HDR.size])
                else:
                    sock.sendto(hdr + pad[: psize - HDR.size], dst)
                sent += 1
            except OSError:
                pass
            seq += 1
    return sent


# ----------------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------------
class UdpServer:
    """Records upstream realtime packets and generates the matching downstream."""

    def __init__(self, outdir: str):
        self.outdir = outdir
        if HAVE_V6:
            self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            self.sock.bind(("::", UDP_PORT))
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", UDP_PORT))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        _enable_rx_timestamps(self.sock)
        self.flows: dict[tuple, dict] = {}
        self.lock = threading.Lock()

    def serve(self):
        while True:
            data, addr, rx = _recv_ts(self.sock)
            if len(data) < HDR.size:
                continue
            magic, fh, seq, sent_ns, kind, rate, size, dur, dn_rate, dn_size = HDR.unpack_from(data)
            if magic != MAGIC:
                continue
            key = (addr[0], addr[1], fh)
            with self.lock:
                f = self.flows.get(key)
                if f is None:
                    f = {"fh": fh, "kind": KIND_NAME.get(kind, "?"), "addr": addr[0], "seq": [], "tx": [],
                         "rx": [], "size": []}
                    self.flows[key] = f
                    threading.Thread(target=self._downstream, args=(key, addr, fh, kind, dn_rate, dn_size, dur),
                                     daemon=True).start()
                f["seq"].append(seq)
                f["tx"].append(sent_ns)
                f["rx"].append(rx)
                f["size"].append(len(data))

    def _downstream(self, key, addr, fh, kind, dn_rate, dn_size, dur):
        if dn_rate > 0:
            send_realtime(self.sock, addr, fh, kind, dn_rate, dn_size, dur, 0, 0)
        time.sleep(2.5)  # drain late upstream packets
        with self.lock:
            f = self.flows.pop(key, None)
        if f is not None:
            _write_json(os.path.join(self.outdir, f"up_{fh:08x}.json"), f)


class TcpHandler(socketserver.BaseRequestHandler):
    outdir = "."

    def handle(self):
        s: socket.socket = self.request
        f = s.makefile("rb")
        line = f.readline()
        if not line:
            return
        try:
            req = json.loads(line)
        except ValueError:
            return
        mode = req.get("mode")
        if mode == "down":
            buf = memoryview(bytes(256 * 1024))
            end = time.monotonic() + float(req.get("seconds", 30))
            try:
                while time.monotonic() < end:
                    s.sendall(buf)
            except OSError:
                pass
        elif mode == "up":
            # measure goodput of an upload at the receiver, in 0.25 s bins
            t0 = time.monotonic()
            bins: dict[int, int] = {}
            buf = bytearray(256 * 1024)
            while True:
                try:
                    n = s.recv_into(buf)
                except OSError:
                    break
                if n <= 0:
                    break
                b = int((time.monotonic() - t0) / 0.25)
                bins[b] = bins.get(b, 0) + n
            _write_json(os.path.join(self.outdir, f"upbulk_{req.get('id', 'x')}_{req.get('conn', 0)}.json"),
                        {"t0_wall": time.time() - (time.monotonic() - t0), "bin": 0.25, "bins": bins})
        elif mode == "chunks":
            buf = memoryview(bytes(256 * 1024))
            while True:
                line = f.readline()
                if not line:
                    break
                n = int(line.strip() or 0)
                try:
                    while n > 0:
                        k = min(n, len(buf))
                        s.sendall(buf[:k])
                        n -= k
                except OSError:
                    break
        elif mode == "web":
            while True:
                line = f.readline()
                if not line:
                    break
                n = int(line.strip() or 0)
                try:
                    s.sendall(bytes(n))
                except OSError:
                    break


class DualStackTCPServer(socketserver.ThreadingTCPServer):
    address_family = socket.AF_INET6 if HAVE_V6 else socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        if HAVE_V6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def run_server(outdir: str) -> None:
    os.makedirs(outdir, exist_ok=True)
    TcpHandler.outdir = outdir
    udp = UdpServer(outdir)
    threading.Thread(target=udp.serve, daemon=True).start()
    srv = DualStackTCPServer(("::" if HAVE_V6 else "0.0.0.0", TCP_PORT), TcpHandler)
    print(f"traffic server ready (udp {UDP_PORT}, tcp {TCP_PORT}) -> {outdir}", flush=True)
    srv.serve_forever()


# ----------------------------------------------------------------------------
# Clients
# ----------------------------------------------------------------------------
def run_realtime(kind: str, name: str, src: str, dst: str, duration: float, outdir: str,
                 up_rate: int, up_size: int, dn_rate: int, dn_size: int) -> None:
    fam = _family(dst)
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.bind((src, 0))
    s.connect((dst, UDP_PORT))
    _enable_rx_timestamps(s)
    fh = flow_hash(name)
    rec = {"seq": [], "tx": [], "rx": [], "size": []}
    stop = threading.Event()

    def rx_loop():
        s.settimeout(0.5)
        while not stop.is_set():
            try:
                data, _addr, rx = _recv_ts(s)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if len(data) < HDR.size:
                continue
            magic, h, seq, sent_ns, *_ = HDR.unpack_from(data)
            if magic != MAGIC or h != fh:
                continue
            rec["seq"].append(seq)
            rec["tx"].append(sent_ns)
            rec["rx"].append(rx)
            rec["size"].append(len(data))

    th = threading.Thread(target=rx_loop, daemon=True)
    th.start()
    start_wall = time.time()
    lport = s.getsockname()[1]
    sent = send_realtime(s, None, fh, KIND[kind], up_rate, up_size, duration, dn_rate, dn_size)
    time.sleep(2.0)
    stop.set()
    th.join(timeout=2)
    _write_json(os.path.join(outdir, f"{name}.json"), {
        "name": name, "kind": kind, "src": src, "dst": dst, "sport": lport, "dport": UDP_PORT, "proto": "udp",
        "flow_hash": f"{fh:08x}", "start_wall": start_wall, "duration": duration, "sent_up": sent,
        "up_rate": up_rate, "up_size": up_size, "dn_rate": dn_rate, "dn_size": dn_size, "down": rec})


def run_bulk(name: str, src: str, dst: str, duration: float, outdir: str, conns: int, direction: str) -> None:
    fam = _family(dst)
    bins_all: list[dict] = []
    ports: list[int] = []
    start_wall = time.time()
    t0 = time.monotonic()

    def worker(i: int):
        bins: dict[int, int] = {}
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)  # kernel autotuning, like real hosts
            s.bind((src, 0))
            s.connect((dst, TCP_PORT))
            ports.append(s.getsockname()[1])
            if direction == "down":
                s.sendall((json.dumps({"mode": "down", "seconds": duration}) + "\n").encode())
                buf = bytearray(256 * 1024)
                s.settimeout(0.5)
                stop = t0 + duration + 2.0     # data still queued when the run ends is not waited for
                while time.monotonic() < stop:
                    try:
                        n = s.recv_into(buf)
                    except (socket.timeout, TimeoutError):
                        continue
                    if n <= 0:
                        break
                    b = int((time.monotonic() - t0) / 0.25)
                    bins[b] = bins.get(b, 0) + n
            else:
                s.sendall((json.dumps({"mode": "up", "id": name, "conn": i}) + "\n").encode())
                buf = memoryview(bytes(16 * 1024))      # small writes so the flow stops on time even when starved
                end = t0 + duration
                s.settimeout(0.5)
                while time.monotonic() < end:
                    try:
                        s.send(buf)
                    except (socket.timeout, TimeoutError):
                        continue
                s.settimeout(5.0)
                s.shutdown(socket.SHUT_WR)
                time.sleep(1.0)
            s.close()
        except OSError:
            pass
        bins_all.append(bins)

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(conns)]
    for t in ths:
        t.start()
    terminated = False
    try:
        for t in ths:
            t.join(timeout=duration + 8)
    except SystemExit:
        terminated = True            # stopped by the harness: keep what was measured
    total: dict[int, int] = {}
    for b in bins_all:
        for k, v in b.items():
            total[k] = total.get(k, 0) + v
    _write_json(os.path.join(outdir, f"{name}.json"), {
        "name": name, "kind": "bulk", "direction": direction, "src": src, "dst": dst, "proto": "tcp",
        "sports": ports, "dport": TCP_PORT, "conns": conns, "start_wall": start_wall, "duration": duration,
        "bin": 0.25, "bins": {str(k): v for k, v in sorted(total.items())}, **({"terminated": True} if terminated else {})})


def run_abr(name: str, src: str, dst: str, duration: float, outdir: str, ladder: list[float],
            chunk_s: float, buffer_target: float, seed: int) -> None:
    """A minimal adaptive-bitrate player (throughput-based, like dash.js 'throughput' rule)."""
    rnd = random.Random(seed)
    fam = _family(dst)
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.bind((src, 0))
    s.connect((dst, TCP_PORT))
    sport = s.getsockname()[1]
    s.sendall((json.dumps({"mode": "chunks"}) + "\n").encode())
    s.settimeout(0.25)
    start_wall = time.time()
    t0 = time.monotonic()
    end = t0 + duration
    timed_out = False
    buffer_s = 0.0
    playing = False
    last = t0
    rebuffer = 0.0
    startup = None
    est = None
    chunks = []
    level = 0
    buf = bytearray(256 * 1024)
    terminated = False
    print(f"abr {name} start", file=sys.stderr, flush=True)
    try:
        while time.monotonic() - t0 < duration:
            now = time.monotonic()
            # playback drains the buffer
            if playing:
                buffer_s -= (now - last)
                if buffer_s < 0:
                    rebuffer += -buffer_s
                    buffer_s = 0.0
                    playing = False
            last = now
            if buffer_s >= buffer_target:
                time.sleep(min(0.25, buffer_s - buffer_target + 0.05))
                continue
            # choose bitrate: highest rung <= 0.8 x throughput estimate
            if est is not None:
                level = 0
                for i, r in enumerate(ladder):
                    if r <= 0.8 * est:
                        level = i
            rate = ladder[level]
            nbytes = int(rate * 1e6 / 8 * chunk_s * rnd.uniform(0.9, 1.1))
            ts = time.monotonic()
            s.sendall(f"{nbytes}\n".encode())
            got = 0
            while got < nbytes:
                if time.monotonic() >= end:      # checked every read: a starved stream still trickles in
                    timed_out = True
                    break
                try:
                    n = s.recv_into(buf, min(len(buf), nbytes - got))
                except (socket.timeout, TimeoutError):
                    continue
                if n <= 0:
                    break
                got += n
            if timed_out:
                break                               # the run ended while this segment was still downloading
            dt = max(1e-3, time.monotonic() - ts)
            thr = got * 8 / dt / 1e6
            est = thr if est is None else 0.7 * est + 0.3 * thr
            now = time.monotonic()
            if playing:
                buffer_s -= (now - last)
                if buffer_s < 0:
                    rebuffer += -buffer_s
                    buffer_s = 0.0
                    playing = False
            else:
                # waiting for data while not playing counts as stall after startup
                if startup is not None:
                    rebuffer += (now - last)
            last = now
            buffer_s += chunk_s
            if not playing and buffer_s >= min(4.0, buffer_target):
                playing = True
                if startup is None:
                    startup = now - t0
            chunks.append({"t": round(ts - t0, 3), "mbps": rate, "thr": round(thr, 3), "dl_s": round(dt, 3)})
    except SystemExit:
        terminated = True        # stopped by the harness: keep what was measured
    # account for the time between the last event and the end of the run (a stuck segment is stall time)
    now = min(time.monotonic(), end)
    if playing:
        buffer_s -= (now - last)
        if buffer_s < 0:
            rebuffer += -buffer_s
    elif startup is not None:
        rebuffer += max(0.0, now - last)
    s.close()
    _write_json(os.path.join(outdir, f"{name}.json"), {
        "name": name, "kind": "abr", "src": src, "dst": dst, "proto": "tcp", "sport": sport, "dport": TCP_PORT,
        "start_wall": start_wall, "duration": duration, "ladder": ladder, "chunks": chunks,
        "rebuffer_s": rebuffer, "startup_s": startup, **({"terminated": True} if terminated else {})})
    print(f"abr {name} done (rebuffer {rebuffer:.1f} s{', terminated' if terminated else ''})", file=sys.stderr, flush=True)


def run_web(name: str, src: str, dst: str, duration: float, outdir: str, seed: int) -> None:
    """Interactive browsing: request a page (20-300 KB) every 1-4 s; record page load time."""
    rnd = random.Random(seed)
    fam = _family(dst)
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.bind((src, 0))
    s.connect((dst, TCP_PORT))
    sport = s.getsockname()[1]
    s.sendall((json.dumps({"mode": "web"}) + "\n").encode())
    start_wall = time.time()
    t0 = time.monotonic()
    loads = []
    buf = bytearray(65536)
    s.settimeout(0.25)
    end = t0 + duration
    terminated = False
    try:
        while time.monotonic() < end:
            n = rnd.randint(20_000, 300_000)
            ts = time.monotonic()
            s.sendall(f"{n}\n".encode())
            got, cut = 0, False
            while got < n:
                if time.monotonic() >= end:
                    cut = True
                    break
                try:
                    k = s.recv_into(buf, min(len(buf), n - got))
                except (socket.timeout, TimeoutError):
                    continue
                if k <= 0:
                    break
                got += k
            loads.append({"t": round(ts - t0, 3), "bytes": n, "plt_ms": round((time.monotonic() - ts) * 1000, 2),
                          **({"unfinished": True} if cut else {})})
            if cut:
                break
            time.sleep(rnd.uniform(1.0, 4.0))
    except SystemExit:
        terminated = True
    s.close()
    _write_json(os.path.join(outdir, f"{name}.json"), {
        "name": name, "kind": "web", "src": src, "dst": dst, "proto": "tcp", "sport": sport, "dport": TCP_PORT,
        "start_wall": start_wall, "duration": duration, "loads": loads, **({"terminated": True} if terminated else {})})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("server")
    p.add_argument("--out", required=True)
    for k in ("voip", "video", "gaming", "bulk", "abr", "web"):
        p = sub.add_parser(k)
        p.add_argument("--name", required=True)
        p.add_argument("--src", required=True)
        p.add_argument("--dst", required=True)
        p.add_argument("--duration", type=float, default=30)
        p.add_argument("--out", required=True)
        p.add_argument("--seed", type=int, default=0)
        if k == "voip":
            p.add_argument("--pps", type=int, default=50)
            p.add_argument("--size", type=int, default=172)
        if k == "video":
            p.add_argument("--fps", type=int, default=30)
            p.add_argument("--kbps", type=int, default=1800)
            p.add_argument("--down-kbps", type=int, default=2200)
        if k == "gaming":
            p.add_argument("--pps-up", type=int, default=60)
            p.add_argument("--size-up", type=int, default=110)
            p.add_argument("--pps-down", type=int, default=30)
            p.add_argument("--size-down", type=int, default=320)
        if k == "bulk":
            p.add_argument("--conns", type=int, default=4)
            p.add_argument("--direction", choices=["down", "up"], default="down")
        if k == "abr":
            p.add_argument("--ladder", default="1.5,3,6,10")
            p.add_argument("--chunk", type=float, default=2.0)
            p.add_argument("--buffer-target", type=float, default=12.0)
    a = ap.parse_args(argv)
    import signal

    def _term(signum, frame):
        raise SystemExit(143)
    if a.cmd != "server":
        signal.signal(signal.SIGTERM, _term)
    if a.cmd == "server":
        run_server(a.out)
        return 0
    os.makedirs(a.out, exist_ok=True)
    if a.cmd == "voip":
        run_realtime("voip", a.name, a.src, a.dst, a.duration, a.out, a.pps, a.size, a.pps, a.size)
    elif a.cmd == "video":
        fr_up = int(a.kbps * 1000 / 8 / a.fps)
        fr_dn = int(a.down_kbps * 1000 / 8 / a.fps)
        run_realtime("video", a.name, a.src, a.dst, a.duration, a.out, a.fps, min(fr_up, 65535), a.fps,
                     min(fr_dn, 65535))
    elif a.cmd == "gaming":
        run_realtime("gaming", a.name, a.src, a.dst, a.duration, a.out, a.pps_up, a.size_up, a.pps_down,
                     a.size_down)
    elif a.cmd == "bulk":
        run_bulk(a.name, a.src, a.dst, a.duration, a.out, a.conns, a.direction)
    elif a.cmd == "abr":
        run_abr(a.name, a.src, a.dst, a.duration, a.out, [float(x) for x in a.ladder.split(",")], a.chunk,
                a.buffer_target, a.seed)
    elif a.cmd == "web":
        run_web(a.name, a.src, a.dst, a.duration, a.out, a.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
