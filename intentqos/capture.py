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
import socket
import struct
import threading
import time

ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_STATISTICS = 6
SNAPLEN = 96          # enough for Eth+VLAN+IPv6+TCP base header; payload is never parsed
EARLY_N = 16          # per-flow "first packets" kept for early classification
WINDOW_S = 0.5        # window length for rolling statistics
HISTORY_WINDOWS = 16  # 8 s of per-window history (ON/OFF pattern detection)

PROTO_TCP, PROTO_UDP = 6, 17


class Window:
    __slots__ = ("pk_up", "by_up", "sq_up", "mx_up", "pk_dn", "by_dn", "sq_dn", "mx_dn",
                 "small", "large", "iat_n_up", "iat_s_up", "iat_q_up", "iat_n_dn", "iat_s_dn", "iat_q_dn")

    def __init__(self):
        self.pk_up = self.by_up = self.sq_up = self.mx_up = 0
        self.pk_dn = self.by_dn = self.sq_dn = self.mx_dn = 0
        self.small = self.large = 0
        self.iat_n_up = 0
        self.iat_s_up = self.iat_q_up = 0.0
        self.iat_n_dn = 0
        self.iat_s_dn = self.iat_q_dn = 0.0


class Flow:
    __slots__ = ("key", "family", "first", "last", "pk_up", "pk_dn", "by_up", "by_dn", "early", "cur",
                 "hist", "last_up", "last_dn", "fin", "syn", "fid", "label", "meta")

    def __init__(self, key, family, now):
        self.key = key
        self.family = family
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
    """Parse L2/L3/L4 headers. Returns (family, src, dst, proto, sport, dport, ip_len, tcp_flags) or None.

    Only header fields are read. `buf` may contain a few payload bytes after the
    L4 header because of SNAPLEN, but they are never touched.
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
        ip_len = (buf[off + 2] << 8) | buf[off + 3]
        frag = ((buf[off + 6] & 0x1F) << 8) | buf[off + 7]
        proto = buf[off + 9]
        src = socket.inet_ntop(socket.AF_INET, bytes(buf[off + 12:off + 16]))
        dst = socket.inet_ntop(socket.AF_INET, bytes(buf[off + 16:off + 20]))
        l4 = off + ihl
        fam = 4
        if frag != 0:   # non-first fragment: no L4 header
            return (fam, src, dst, proto, 0, 0, ip_len, 0)
    elif etype == 0x86DD:
        if n_captured < off + 40:
            return None
        ip_len = ((buf[off + 4] << 8) | buf[off + 5]) + 40
        proto = buf[off + 6]
        src = socket.inet_ntop(socket.AF_INET6, bytes(buf[off + 8:off + 24]))
        dst = socket.inet_ntop(socket.AF_INET6, bytes(buf[off + 24:off + 40]))
        l4 = off + 40
        fam = 6
        # skip a few common extension headers (hop-by-hop, routing, dest opts)
        hops = 0
        while proto in (0, 43, 60) and n_captured >= l4 + 2 and hops < 4:
            proto = buf[l4]
            l4 += (buf[l4 + 1] + 1) * 8
            hops += 1
    else:
        return None
    sport = dport = flags = 0
    if proto in (PROTO_TCP, PROTO_UDP) and n_captured >= l4 + 4:
        sport = (buf[l4] << 8) | buf[l4 + 1]
        dport = (buf[l4 + 2] << 8) | buf[l4 + 3]
        if proto == PROTO_TCP and n_captured >= l4 + 14:
            flags = buf[l4 + 13]
    return (fam, src, dst, proto, sport, dport, ip_len, flags)


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
        fam, src, dst, proto, sport, dport, size, flags = parsed
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
                 emit=None):
        self.iface, self.table, self.path = iface, table, path
        self.args = [path, "-i", iface, "-t", str(idle_timeout)]
        for n in subnets:
            self.args += ["-n", n]
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

    def roll(self, table: FlowTable, now: float) -> None:
        try:
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
                if p[31] != "-":
                    n = int(p[31])
                    f.early = [(float(p[32 + 3 * j]), int(p[33 + 3 * j]), int(p[34 + 3 * j])) for j in range(n)]

    def stats(self) -> dict:
        if self.fallback:
            d = self.fallback.stats()
            d["backend"] = "python (helper failed)"
            return d
        return {"iface": self.iface, "packets": self.st["packets"], "bytes": self.st["bytes"],
                "kernel_packets": self.st["kernel_packets"], "kernel_drops": self.st["kernel_drops"],
                "errors": self.errors, "flows": self.st["flows"], "snaplen": SNAPLEN, "backend": "native helper",
                "overflow": self.st["overflow"]}


def make_capture(cfg: dict, iface: str, table: FlowTable, emit=None):
    """Pick the capture backend: the native helper when present (routers), else the Python loop."""
    import os
    import shutil
    c = cfg.get("capture", {})
    want = c.get("backend", "auto")
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
