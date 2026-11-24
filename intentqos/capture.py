"""Module 1 -- Traffic capture & flow tracker (header-only).

Privacy boundary
  * The packet socket copies at most SNAPLEN bytes per packet into userspace and
    the parser reads ONLY Ethernet/VLAN, IPv4/IPv6 and TCP/UDP header fields.
    Payload bytes are never parsed, stored, hashed or logged.
  * Nothing is decrypted. The original packet length comes from MSG_TRUNC.

What it produces
  A table of bidirectional flows keyed by (lan_ip, remote_ip, proto, lan_port,
  remote_port) with counters, the first N packets' size/IAT/direction ("early
  flow" features) and per-0.5 s window summaries used by the classifier.

Inputs : a LAN-side interface name and the LAN subnets.
Outputs: FlowTable.snapshot() -> list[Flow]; capture statistics (incl. kernel drops).
Failure: socket errors are counted and the capture thread restarts with backoff;
         if capture is unavailable the engine keeps shaping with the last labels.
Success: per-flow counters match `tc`/interface counters within a few percent
         (checked in tests/test_capture.py with synthetic frames).
"""
from __future__ import annotations

import collections
import ipaddress
import os
import socket
import struct
import threading
import time

DSCP_CODES = {"ef": 46, "af41": 34, "af31": 26, "af21": 18, "af11": 10, "cs0": 0, "cs1": 8, "cs2": 16, "cs3": 24,
              "cs4": 32, "cs5": 40, "cs6": 48, "le": 1}

ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_STATISTICS = 6
SNAPLEN = 96          # enough for Eth+VLAN+IPv6+TCP base header; payload is never parsed
EARLY_N = 16          # per-flow "first packets" kept for early classification
WINDOW_S = 0.5        # window length for rolling statistics
HISTORY_WINDOWS = 16  # 8 s of per-window history (ON/OFF pattern detection)

PROTO_TCP, PROTO_UDP = 6, 17
VID_MIN = 200         # bytes: packets this large can carry video
VID_GAP = 0.004       # s: a gap this long between video-size packets starts a new frame
TS_MAX_AGE = 3.0      # s: an unanswered TCP timestamp sample is dropped after this
TS_SLOTS = 16         # outstanding timestamp samples kept per direction
TS_GAP_MIN = 0.002    # s: samples are spaced at least this far apart (ePPing-style rate limit) ...
TS_GAP_MAX = 0.064    # ... and at most this: 16 slots then cover a 1 s round trip

# Per-window experience fields (header-only; same names and semantics in router/iq-capture.c):
#   ect0/ect1/ce   ECN codepoints seen (ECT(1) = L4S),   dscp_up  highest DSCP the LAN device sent (255 = none)
#   retx_up/dn     TCP segments that carried no new data (retransmission or reordering)
#   rw_*           internet RTT samples router->server->router from TCP timestamps (ms: n, sum, min, max)
#   rl_*           LAN RTT samples router->device->router (Wi-Fi / cable)
#   fr_up/dn       bursts of video-size packets (frames)    sp_n/sp_s  QUIC spin-bit RTT samples (ms)
XFIELDS = ("ect0", "ect1", "ce", "dscp_up", "retx_up", "retx_dn", "rw_n", "rw_s", "rw_min", "rw_max",
           "rl_n", "rl_s", "rl_min", "rl_max", "fr_up", "fr_dn", "sp_n", "sp_s")


class Window:
    __slots__ = ("pk_up", "by_up", "sq_up", "mx_up", "pk_dn", "by_dn", "sq_dn", "mx_dn",
                 "small", "large", "iat_n_up", "iat_s_up", "iat_q_up", "iat_n_dn", "iat_s_dn", "iat_q_dn") + XFIELDS

    def __init__(self):
        self.pk_up = self.by_up = self.sq_up = self.mx_up = 0
        self.pk_dn = self.by_dn = self.sq_dn = self.mx_dn = 0
        self.small = self.large = 0
        self.iat_n_up = 0
        self.iat_s_up = self.iat_q_up = 0.0
        self.iat_n_dn = 0
        self.iat_s_dn = self.iat_q_dn = 0.0
        self.ect0 = self.ect1 = self.ce = self.retx_up = self.retx_dn = 0
        self.rw_n = self.rl_n = self.fr_up = self.fr_dn = self.sp_n = 0
        self.rw_s = self.rw_min = self.rw_max = self.rl_s = self.rl_min = self.rl_max = self.sp_s = 0.0
        self.dscp_up = 255


class Flow:
    __slots__ = ("key", "family", "first", "last", "pk_up", "pk_dn", "by_up", "by_dn", "early", "cur",
                 "hist", "last_up", "last_dn", "fin", "syn", "fid", "label", "meta",
                 "seq_hi_up", "seq_hi_dn", "tsu", "tsd", "last_tsv_up", "last_tsv_dn",
                 "spin_last", "spin_t", "vid_up", "vid_dn", "ts_next_up", "ts_next_dn", "ts_gap_up", "ts_gap_dn", "dev")

    def __init__(self, key, family, now):
        self.key = key
        self.family = family
        self.dev = key[0]           # device key: the device's IPv4 address for its IPv6 flows (neighbours.py)
        self.first = now
        self.last = now
        self.pk_up = self.pk_dn = self.by_up = self.by_dn = 0
        self.early: list[tuple[float, int, int]] = []    # (t - first, size, dir 1=up 0=down)
        self.cur = Window()
        self.hist: collections.deque = collections.deque(maxlen=HISTORY_WINDOWS)
        self.last_up = self.last_dn = 0.0
        self.fin = False
        self.syn = False
        self.fid = ""
        self.label = None
        self.meta: dict = {}
        self.seq_hi_up = self.seq_hi_dn = None     # highest TCP sequence end seen per direction
        self.tsu: list = []                        # outstanding timestamp samples [(tsval, t)] (up / down)
        self.tsd: list = []
        self.last_tsv_up = self.last_tsv_dn = 0
        self.spin_last = None
        self.spin_t = 0.0
        self.vid_up = self.vid_dn = 0.0
        self.ts_next_up = self.ts_next_dn = 0.0    # sample spacing: about a sixteenth of the RTT
        self.ts_gap_up = self.ts_gap_dn = TS_GAP_MIN

    # key = (lan_ip, remote_ip, proto, lan_port, remote_port)
    @property
    def lan_ip(self) -> str:
        return self.key[0]

    @property
    def remote_ip(self) -> str:
        return self.key[1]

    @property
    def proto(self) -> int:
        return self.key[2]

    def id(self) -> str:
        if not self.fid:
            l, r, p, lp, rp = self.key
            pn = {6: "tcp", 17: "udp"}.get(p, str(p))
            self.fid = f"{pn}:{l}:{lp}-{r}:{rp}"
        return self.fid


def parse_frame(buf, n_captured: int, wire_len: int):
    """Parse L2/L3/L4 headers. Returns (family, src, dst, proto, sport, dport, ip_len, tcp_flags, extra) or None,
    extra = (tos, tcp_seq, tcp_payload_len, tsval, tsecr, quic_spin) with -1 / 0 where absent.

    Only header fields are read (IP TOS byte, TCP sequence number and options, the QUIC public flags
    byte of UDP/443 packets). `buf` may contain a few payload bytes after the headers because of
    SNAPLEN; other than QUIC's public flags (which RFC 9000 exposes to the network) they are never touched.
    """
    if n_captured < 14:
        return None
    off = 12
    etype = (buf[off] << 8) | buf[off + 1]
    off = 14
    while etype in (0x8100, 0x88A8) and n_captured >= off + 4:   # VLAN / QinQ
        etype = (buf[off + 2] << 8) | buf[off + 3]
        off += 4
    if etype == 0x0800:
        if n_captured < off + 20:
            return None
        ihl = (buf[off] & 0x0F) * 4
        tos = buf[off + 1]
        ip_len = (buf[off + 2] << 8) | buf[off + 3]
        frag = ((buf[off + 6] & 0x1F) << 8) | buf[off + 7]
        proto = buf[off + 9]
        src = socket.inet_ntop(socket.AF_INET, bytes(buf[off + 12:off + 16]))
        dst = socket.inet_ntop(socket.AF_INET, bytes(buf[off + 16:off + 20]))
        l4 = off + ihl
        fam = 4
        if frag != 0:   # non-first fragment: no L4 header
            return (fam, src, dst, proto, 0, 0, ip_len, 0, (tos, 0, -1, 0, 0, -1))
    elif etype == 0x86DD:
        if n_captured < off + 40:
            return None
        ip_len = ((buf[off + 4] << 8) | buf[off + 5]) + 40
        tos = ((buf[off] & 0x0F) << 4) | (buf[off + 1] >> 4)
        proto = buf[off + 6]
        src = socket.inet_ntop(socket.AF_INET6, bytes(buf[off + 8:off + 24]))
        dst = socket.inet_ntop(socket.AF_INET6, bytes(buf[off + 24:off + 40]))
        l4 = off + 40
        fam = 6
        # extension headers: hop-by-hop (0), routing (43), fragment (44), AH (51), destination options (60).
        # A non-first fragment carries no L4 header: count it, but do not read payload bytes as ports.
        hops = frag6 = 0
        while proto in (0, 43, 44, 51, 60) and n_captured >= l4 + 8 and hops < 4:
            nxt = buf[l4]
            if proto == 44:
                frag6 = ((buf[l4 + 2] << 8) | buf[l4 + 3]) & 0xFFF8
                l4 += 8
            elif proto == 51:
                l4 += (buf[l4 + 1] + 2) * 4
            else:
                l4 += (buf[l4 + 1] + 1) * 8
            proto = nxt
            hops += 1
        if frag6:
            return (fam, src, dst, proto, 0, 0, ip_len, 0, (tos, 0, -1, 0, 0, -1))
    else:
        return None
    sport = dport = flags = 0
    seq = tsval = tsecr = 0
    paylen = spin = -1
    if proto in (PROTO_TCP, PROTO_UDP) and n_captured >= l4 + 4:
        sport = (buf[l4] << 8) | buf[l4 + 1]
        dport = (buf[l4 + 2] << 8) | buf[l4 + 3]
        if proto == PROTO_TCP and n_captured >= l4 + 14:
            flags = buf[l4 + 13]
        if proto == PROTO_TCP and n_captured >= l4 + 13:
            doff = (buf[l4 + 12] >> 4) * 4
            seq = (buf[l4 + 4] << 24) | (buf[l4 + 5] << 16) | (buf[l4 + 6] << 8) | buf[l4 + 7]
            paylen = ip_len - (l4 - off) - doff
            i, end = l4 + 20, min(l4 + doff, n_captured)
            while doff >= 20 and i < end:                 # TCP options: the timestamp option is kind 8
                kind = buf[i]
                if kind == 0:
                    break
                if kind == 1:
                    i += 1
                    continue
                if i + 1 >= end:
                    break
                ln = buf[i + 1]
                if ln < 2:
                    break
                if kind == 8 and ln == 10 and i + 10 <= end:
                    tsval = int.from_bytes(bytes(buf[i + 2:i + 6]), "big")
                    tsecr = int.from_bytes(bytes(buf[i + 6:i + 10]), "big")
                    break
                i += ln
        if proto == PROTO_UDP and 443 in (sport, dport) and n_captured >= l4 + 9:
            q = buf[l4 + 8]                               # QUIC public flags: short header + fixed bit
            if not q & 0x80 and q & 0x40:
                spin = (q >> 5) & 1
    return (fam, src, dst, proto, sport, dport, ip_len, flags, (tos, seq, paylen, tsval, tsecr, spin))


def _experience(f: Flow, w: Window, up: bool, size: int, now: float, extra: tuple) -> None:
    """Per-packet experience accounting; mirrors account() in router/iq-capture.c line for line."""
    tos, seq, paylen, tsval, tsecr, spin = extra
    ecn = tos & 3
    if ecn == 2:
        w.ect0 += 1
    elif ecn == 1:
        w.ect1 += 1
    elif ecn == 3:
        w.ce += 1
    if up and (w.dscp_up == 255 or tos >> 2 > w.dscp_up):  # the highest DSCP the device sent in the window
        w.dscp_up = tos >> 2
    if size >= VID_MIN:
        last = f.vid_up if up else f.vid_dn
        if last == 0 or now - last > VID_GAP:
            if up:
                w.fr_up += 1
            else:
                w.fr_dn += 1
        if up:
            f.vid_up = now
        else:
            f.vid_dn = now
    if paylen > 0:
        e = (seq + paylen) & 0xFFFFFFFF
        hi = f.seq_hi_up if up else f.seq_hi_dn
        if hi is not None and ((e - hi) & 0xFFFFFFFF) - (0x100000000 if (e - hi) & 0x80000000 else 0) <= 0:
            if up:
                w.retx_up += 1
            else:
                w.retx_dn += 1
        elif up:
            f.seq_hi_up = e
        else:
            f.seq_hi_dn = e
    if tsval:                                             # passive RTT from TCP timestamps (ePPing)
        mine, theirs = (f.tsu, f.tsd) if up else (f.tsd, f.tsu)
        theirs[:] = [(v, t) for v, t in theirs if now - t <= TS_MAX_AGE]
        for v, t0 in theirs:                              # does this packet echo the other side's stamp?
            if v == tsecr:
                ms = (now - t0) * 1000.0
                if up:                                    # the device echoed the server: LAN RTT
                    w.rl_n += 1
                    w.rl_s += ms
                    w.rl_min = ms if w.rl_n == 1 or ms < w.rl_min else w.rl_min
                    w.rl_max = max(w.rl_max, ms)
                else:                                     # the server echoed the device: internet RTT
                    w.rw_n += 1
                    w.rw_s += ms
                    w.rw_min = ms if w.rw_n == 1 or ms < w.rw_min else w.rw_min
                    w.rw_max = max(w.rw_max, ms)
                theirs[:] = [(x, t) for x, t in theirs if t > t0]
                g = min(TS_GAP_MAX, max(TS_GAP_MIN, ms / 1000.0 / TS_SLOTS))   # slots span two round trips
                if up:
                    f.ts_gap_dn = g
                else:
                    f.ts_gap_up = g
                break
        last = f.last_tsv_up if up else f.last_tsv_dn
        nxt = f.ts_next_up if up else f.ts_next_dn
        if tsval != last and now >= nxt:                  # the first packet with a new stamp, one per gap
            gap = f.ts_gap_up if up else f.ts_gap_dn
            if len(mine) >= TS_SLOTS:                     # all slots busy: the RTT is longer than they span
                mine.remove(min(mine, key=lambda x: x[1]))
                gap = min(TS_GAP_MAX, gap * 2)
            mine.append((tsval, now))
            if up:
                f.last_tsv_up, f.ts_next_up, f.ts_gap_up = tsval, now + gap, gap
            else:
                f.last_tsv_dn, f.ts_next_dn, f.ts_gap_dn = tsval, now + gap, gap
    if spin >= 0 and not up:                              # QUIC spin-bit edges, server -> device
        if f.spin_last is not None and spin != f.spin_last:
            ms = (now - f.spin_t) * 1000.0
            if 1.0 <= ms <= 2000.0:
                w.sp_n += 1
                w.sp_s += ms
        if f.spin_last is None or spin != f.spin_last:
            f.spin_t, f.spin_last = now, spin


class FlowTable:
    def __init__(self, lan_subnets: list[str], idle_timeout: float = 30.0):
        self.nets = [ipaddress.ip_network(s, strict=False) for s in lan_subnets]
        self.flows: dict[tuple, Flow] = {}
        self.lock = threading.Lock()
        self.idle_timeout = idle_timeout
        self.packets = 0
        self.bytes = 0
        self._lan_cache: dict[str, bool] = {}
        self.source = None              # HelperCapture when a native helper does the accounting

    def is_lan(self, ip: str) -> bool:
        v = self._lan_cache.get(ip)
        if v is None:
            try:
                a = ipaddress.ip_address(ip)
                v = any(a in n for n in self.nets)
            except ValueError:
                v = False
            if len(self._lan_cache) > 65536:
                self._lan_cache.clear()
            self._lan_cache[ip] = v
        return v

    def add(self, parsed, now: float) -> None:
        fam, src, dst, proto, sport, dport, size, flags = parsed[:8]
        extra = parsed[8] if len(parsed) > 8 else None
        src_lan, dst_lan = self.is_lan(src), self.is_lan(dst)
        if src_lan == dst_lan:
            return                      # LAN<->LAN or transit we do not shape
        up = src_lan
        key = (src, dst, proto, sport, dport) if up else (dst, src, proto, dport, sport)
        with self.lock:
            self.packets += 1
            self.bytes += size
            f = self.flows.get(key)
            if f is None:
                f = Flow(key, fam, now)
                self.flows[key] = f
            f.last = now
            w = f.cur
            if up:
                f.pk_up += 1
                f.by_up += size
                w.pk_up += 1
                w.by_up += size
                w.sq_up += size * size
                if size > w.mx_up:
                    w.mx_up = size
                if f.last_up:
                    d = now - f.last_up
                    w.iat_n_up += 1
                    w.iat_s_up += d
                    w.iat_q_up += d * d
                f.last_up = now
            else:
                f.pk_dn += 1
                f.by_dn += size
                w.pk_dn += 1
                w.by_dn += size
                w.sq_dn += size * size
                if size > w.mx_dn:
                    w.mx_dn = size
                if f.last_dn:
                    d = now - f.last_dn
                    w.iat_n_dn += 1
                    w.iat_s_dn += d
                    w.iat_q_dn += d * d
                f.last_dn = now
            if size < 300:
                w.small += 1
            elif size > 1200:
                w.large += 1
            if extra is not None:
                _experience(f, w, up, size, now, extra)
            if len(f.early) < EARLY_N:
                f.early.append((now - f.first, size, 1 if up else 0))
            if flags:
                if flags & 0x02:
                    f.syn = True
                if flags & 0x05:            # FIN or RST
                    f.fin = True

    def roll_windows(self, now: float) -> None:
        """Close the current 0.5 s window of every flow and expire idle flows."""
        if self.source is not None and self.source.alive():
            self.source.roll(self, now)
            return
        with self.lock:
            dead = []
            for k, f in self.flows.items():
                f.hist.append(f.cur)
                f.cur = Window()
                idle = now - f.last
                if idle > self.idle_timeout or (f.fin and idle > 3.0):
                    dead.append(k)
            for k in dead:
                del self.flows[k]

    def active(self, now: float, max_idle: float = 10.0) -> list[Flow]:
        with self.lock:
            return [f for f in self.flows.values() if now - f.last <= max_idle]


class Capture:
    """AF_PACKET capture thread feeding a FlowTable."""

    def __init__(self, iface: str, table: FlowTable):
        self.iface = iface
        self.table = table
        self.running = False
        self.errors = 0
        self.kernel_drops = 0
        self.kernel_packets = 0
        self._sock = None
        self._thread = None

    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def stats(self) -> dict:
        if self._sock is not None:
            try:
                raw = self._sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)
                p, d = struct.unpack("II", raw)   # counters reset on read
                self.kernel_packets += p
                self.kernel_drops += d
            except OSError:
                pass
        return {"iface": self.iface, "packets": self.table.packets, "bytes": self.table.bytes,
                "kernel_packets": self.kernel_packets, "kernel_drops": self.kernel_drops,
                "errors": self.errors, "flows": len(self.table.flows), "snaplen": SNAPLEN, "backend": "python"}

    def _open(self):
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
        s.bind((self.iface, 0))
        s.settimeout(0.5)
        return s

    def _run(self) -> None:
        backoff = 0.5
        buf = bytearray(SNAPLEN)
        mv = memoryview(buf)
        while self.running:
            try:
                self._sock = self._open()
                backoff = 0.5
                recv = self._sock.recvfrom_into
                add = self.table.add
                clock = time.monotonic
                while self.running:
                    try:
                        n, _addr = recv(mv, SNAPLEN, socket.MSG_TRUNC)
                    except (socket.timeout, TimeoutError):
                        continue
                    p = parse_frame(buf, min(n, SNAPLEN), n)
                    if p is not None:
                        add(p, clock())
            except OSError:
                self.errors += 1
                if not self.running:
                    break
                time.sleep(backoff)
                backoff = min(5.0, backoff * 2)


class HelperCapture:
    """Flow accounting done by the native helper `iq-capture` (router/iq-capture.c).

    On a router the per-packet loop is the hot path; the helper reads a TPACKET_V3 ring, truncates
    packets to SNAPLEN in the kernel, and keeps the same per-flow aggregates as FlowTable.add().
    Every policy tick the engine asks it to close the window and receives one line per flow.
    If the helper dies, the engine falls back to the Python capture (and says so in the event log).
    """

    def __init__(self, iface: str, table: FlowTable, path: str, subnets: list[str], idle_timeout: float = 30.0,
                 emit=None, kernel_obj: str | None = None, wan: str | None = None):
        self.iface, self.table, self.path = iface, table, path
        self.args = [path, "-i", iface, "-t", str(idle_timeout)]
        for n in subnets:
            self.args += ["-n", n]
        # kernel flow engine (router/iq-flow.bpf.c): aggregates kept in the kernel, verdicts applied there too
        self.kernel = bool(kernel_obj)
        if kernel_obj:
            self.args += ["-e", kernel_obj] + (["-w", wan] if wan else [])
        self.wlock = threading.Lock()
        self.bridge = os.path.isdir(f"/sys/class/net/{iface}/brif")
        self.emit = emit or (lambda *a, **k: None)
        self.proc = None
        self.st = {"packets": 0, "bytes": 0, "kernel_packets": 0, "kernel_drops": 0, "flows": 0, "overflow": 0}
        self.errors = 0
        self.fallback: Capture | None = None
        self.running = False

    def start(self) -> None:
        import subprocess
        self.running = True
        self.proc = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        time.sleep(0.2)
        if self.proc.poll() is not None:
            err = (self.proc.stderr.read() or "").strip()
            raise OSError(f"iq-capture exited: {err}")
        self.table.source = self

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        self.running = False
        if self.alive():
            try:
                self.proc.stdin.write("Q\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        if self.fallback:
            self.fallback.stop()

    def _fail(self, why: str) -> None:
        self.errors += 1
        self.table.source = None
        if self.running and self.fallback is None:
            self.emit("capture", f"native capture helper failed ({why}); falling back to Python capture", "error")
            self.fallback = Capture(self.iface, self.table)
            self.fallback.start()

    def _send(self, text: str) -> bool:
        if not self.alive():
            return False
        try:
            with self.wlock:
                self.proc.stdin.write(text)
                self.proc.stdin.flush()
            return True
        except (OSError, BrokenPipeError):
            return False

    def classes(self, prio: dict, dscp: dict, mark_dscp: bool) -> None:
        """Kernel engine: skb->priority (tc classid major:minor, hex as tc writes it) and DSCP per tin."""
        if not self.kernel:
            return
        parts = []
        for t in range(5):
            maj, _, mi = str(prio.get(t, "0:0")).partition(":")
            parts.append(f"{(int(maj or '0', 16) << 16) | int(mi or '0', 16)} {DSCP_CODES.get(dscp.get(t), 255)}")
        self._send(f"P {int(bool(mark_dscp))} {' '.join(parts)}\n")

    def verdicts(self, changes: dict) -> None:
        """Kernel engine: {flow_key: tin or None}; None (or best effort) removes the verdict."""
        if not self.kernel or not changes:
            return
        out = []
        for (lan, rem, proto, lp, rp), tin in changes.items():
            fam = 6 if ":" in lan else 4
            out.append(f"M {fam} {lan} {rem} {proto} {lp} {rp} {255 if tin is None else int(tin)}\n")
        self._send("".join(out))

    def clear_verdicts(self) -> None:
        if self.kernel:
            self._send("C\n")

    def roll(self, table: FlowTable, now: float) -> None:
        try:
            with self.wlock:
                self.proc.stdin.write("R\n")
                self.proc.stdin.flush()
            lines = []
            while True:
                ln = self.proc.stdout.readline()
                if not ln:
                    raise OSError("helper closed its output")
                if ln[0] == "E":
                    e = ln.split()
                    self.st.update(packets=int(e[1]), bytes=int(e[2]), kernel_packets=int(e[3]), kernel_drops=int(e[4]),
                                   flows=int(e[5]), overflow=int(e[6]))
                    break
                lines.append(ln)
        except (OSError, ValueError, BrokenPipeError) as ex:
            self._fail(str(ex))
            return
        with table.lock:
            table.packets, table.bytes = self.st["packets"], self.st["bytes"]
            for ln in lines:
                p = ln.split()
                key = (p[2], p[3], int(p[4]), int(p[5]), int(p[6]))
                if p[0] == "X":
                    table.flows.pop(key, None)
                    continue
                f = table.flows.get(key)
                if f is None:
                    f = Flow(key, int(p[1]), float(p[7]))
                    table.flows[key] = f
                f.last = float(p[8])
                f.pk_up, f.pk_dn, f.by_up, f.by_dn = int(p[9]), int(p[10]), int(p[11]), int(p[12])
                w = Window()
                (w.pk_up, w.by_up, w.sq_up, w.mx_up, w.pk_dn, w.by_dn, w.sq_dn, w.mx_dn, w.small, w.large,
                 w.iat_n_up) = (int(x) for x in p[13:24])
                w.iat_s_up, w.iat_q_up = float(p[24]), float(p[25])
                w.iat_n_dn = int(p[26])
                w.iat_s_dn, w.iat_q_dn = float(p[27]), float(p[28])
                f.hist.append(w)
                f.syn, f.fin = p[29] == "1", p[30] == "1"
                for name, v in zip(XFIELDS, p[31:31 + len(XFIELDS)]):
                    setattr(w, name, float(v) if "." in v else int(v))
                e0 = 31 + len(XFIELDS)
                if p[e0] != "-":
                    n = int(p[e0])
                    f.early = [(float(p[e0 + 1 + 3 * j]), int(p[e0 + 2 + 3 * j]), int(p[e0 + 3 + 3 * j])) for j in range(n)]

    def stats(self) -> dict:
        if self.fallback:
            d = self.fallback.stats()
            d["backend"] = "python (helper failed)"
            return d
        backend = "native helper"
        if self.kernel:
            backend = ("kernel flow engine (eBPF on bridge ports)" if self.bridge else
                       "kernel flow engine (eBPF; LAN round trips include this interface's queue)")
        return {"iface": self.iface, "packets": self.st["packets"], "bytes": self.st["bytes"],
                "kernel_packets": self.st["kernel_packets"], "kernel_drops": self.st["kernel_drops"],
                "errors": self.errors, "flows": self.st["flows"], "snaplen": None if self.kernel else SNAPLEN,
                "backend": backend, "overflow": self.st["overflow"]}


def make_capture(cfg: dict, iface: str, table: FlowTable, emit=None):
    """Pick the capture backend: the native helper when present (routers), else the Python loop."""
    import shutil
    c = cfg.get("capture", {})
    want = c.get("backend", "auto")
    if want == "ebpf":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        helpers = [c.get("helper_path") or "", os.path.join(here, "router", "iq-capture"), shutil.which("iq-capture") or ""]
        objs = [c.get("bpf_object") or "", os.path.join(here, "router", "iq-flow.bpf.o"), "/usr/lib/intentqos/iq-flow.bpf.o"]
        hp = next((p for p in helpers if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)
        ob = next((p for p in objs if p and os.path.isfile(p)), None)
        if hp and ob:
            h = HelperCapture(iface, table, hp, cfg["interfaces"]["lan_subnets"], emit=emit, kernel_obj=ob,
                              wan=cfg["interfaces"].get("wan"))
            try:
                h.start()
                return h
            except OSError as e:
                if emit:
                    emit("capture", f"kernel flow engine unavailable ({e}); using the packet-ring helper", "warn")
        elif emit:
            emit("capture", "kernel flow engine needs router/iq-capture and iq-flow.bpf.o; using the packet-ring helper",
                 "warn")
        want = "auto"
    if want in ("auto", "helper"):
        path = c.get("helper_path") or ""
        cands = [path] if path else []
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cands += [os.path.join(here, "router", "iq-capture"), shutil.which("iq-capture") or ""]
        for p in cands:
            if p and os.path.isfile(p) and os.access(p, os.X_OK):
                h = HelperCapture(iface, table, p, cfg["interfaces"]["lan_subnets"], emit=emit)
                try:
                    h.start()
                    return h
                except OSError as e:
                    if want == "helper":
                        raise
                    if emit:
                        emit("capture", f"native capture helper unavailable ({e}); using Python capture", "warn")
                    break
        if want == "helper":
            raise OSError("capture.backend = helper but iq-capture was not found")
    cap = Capture(iface, table)
    return cap
