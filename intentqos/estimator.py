"""Module 3 -- Link-capacity estimator (latency-driven, per direction).

Idea (re-implemented from the documented design of cake-autorate, GPL-2.0 --
no code copied): the useful capacity of an access link is the highest rate
at which *delay does not rise*. So instead of running speed tests we:

  1. probe a few reflectors with ICMP timestamp requests (type 13), which give
     one-way delay up and down separately (clock offsets cancel because we only
     use the rise above a per-reflector baseline); ICMP echo is the RTT fallback;
  2. keep an unloaded-delay baseline per reflector that falls fast and rises
     only slowly and only while the link is lightly loaded (load-gated);
  3. compare achieved throughput (from the shaper's own counters) with the
     current shaper rate to know whether the direction is loaded;
  4. on bufferbloat under load -> cut the rate below what is being achieved;
     when loaded and healthy -> probe upward in small steps; when idle -> drift
     back toward the base rate.

Inputs : reflector probes; achieved rate per direction each tick.
Outputs: shaper rate per direction (kbit/s), state, delay deltas, confidence.
Failure: no probe replies for 2 s => "blind": never increase, cap at base rate,
         emit an event (bounded behaviour). Raw-socket failure => static rate.
Success: after a 100 -> 20 Mbit/s drop the interactive-class p99 delay returns
         under the threshold within a few seconds, with no oscillation.
"""
from __future__ import annotations

import collections
import socket
import struct
import threading
import time

SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)


def _median(v) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _csum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _ms_since_midnight_utc(t_wall: float) -> int:
    return int((t_wall % 86400.0) * 1000.0)


def _wrap_ms(d: float) -> float:
    d = d % 86_400_000.0
    if d > 43_200_000.0:
        d -= 86_400_000.0
    return d


class Prober:
    """ICMP prober (timestamp or echo). Produces samples (t, reflector, rtt_ms, up_ms, down_ms)."""

    def __init__(self, reflectors: list[str], method: str, interval_ms: float):
        self.reflectors = list(reflectors)
        self.method = method
        self.interval = interval_ms / 1000.0
        self.ident = (id(self) >> 4) & 0xFFFF
        self.seq = 0
        self.pending: dict[int, tuple[str, float, int]] = {}
        self.samples: collections.deque = collections.deque(maxlen=4000)
        self.ts_ok: dict[str, int] = {r: 0 for r in reflectors}
        self.sent: dict[str, int] = {r: 0 for r in reflectors}
        self.recv: dict[str, int] = {r: 0 for r in reflectors}
        self.mode: dict[str, str] = {r: method for r in reflectors}
        self.running = False
        self.error = None
        self.lock = threading.Lock()
        self.sock = None

    def start(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
                self.kernel_ts = True
            except OSError:
                self.kernel_ts = False      # some kernels/emulators: fall back to user-space receive time
            self.sock.settimeout(0.5)
        except OSError as e:
            self.error = f"raw ICMP socket unavailable: {e}"
            return False
        self.running = True
        threading.Thread(target=self._tx, name="probe-tx", daemon=True).start()
        threading.Thread(target=self._rx, name="probe-rx", daemon=True).start()
        return True

    def stop(self):
        self.running = False

    def _packet(self, typ: int, seq: int) -> bytes:
        if typ == 13:
            body = struct.pack("!III", _ms_since_midnight_utc(time.time()), 0, 0)
        else:
            body = struct.pack("!d", time.time()) + b"IntentQoS-probe!"
        hdr = struct.pack("!BBHHH", typ, 0, 0, self.ident, seq)
        return struct.pack("!BBHHH", typ, 0, _csum(hdr + body), self.ident, seq) + body

    def _tx(self):
        while self.running:
            t_next = time.monotonic() + self.interval
            for r in self.reflectors:
                # fall back to echo for reflectors that never answer timestamp requests
                if self.mode[r] == "icmp-ts" and self.sent[r] >= 30 and self.ts_ok[r] == 0:
                    self.mode[r] = "icmp-echo"
                typ = 13 if self.mode[r] == "icmp-ts" else 8
                self.seq = (self.seq + 1) & 0xFFFF
                with self.lock:
                    self.pending[self.seq] = (r, time.time(), typ)
                    if len(self.pending) > 2000:
                        for k in list(self.pending)[:1000]:
                            self.pending.pop(k, None)
                try:
                    self.sock.sendto(self._packet(typ, self.seq), (r, 0))
                    self.sent[r] += 1
                except OSError:
                    pass
            d = t_next - time.monotonic()
            if d > 0:
                time.sleep(d)

    def _rx(self):
        while self.running:
            try:
                data, anc, _f, addr = self.sock.recvmsg(2048, socket.CMSG_SPACE(16))
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                if not self.running:
                    return
                time.sleep(0.1)
                continue
            rx_wall = None
            for lvl, typ, cd in anc:
                if lvl == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS and len(cd) >= 16:
                    sec, nsec = struct.unpack("qq", cd[:16])
                    rx_wall = sec + nsec / 1e9
            if rx_wall is None:
                rx_wall = time.time()
            if len(data) < 28:
                continue
            ihl = (data[0] & 0x0F) * 4
            icmp = data[ihl:]
            if len(icmp) < 8:
                continue
            typ, _code, _ck, ident, seq = struct.unpack("!BBHHH", icmp[:8])
            if ident != self.ident or typ not in (0, 14):
                continue
            with self.lock:
                p = self.pending.pop(seq, None)
            if p is None:
                continue
            r, tx_wall, _ptyp = p
            rtt = (rx_wall - tx_wall) * 1000.0
            up = down = None
            if typ == 14 and len(icmp) >= 20:
                orig, recv_ts, xmit = struct.unpack("!III", icmp[8:20])
                if not (recv_ts & 0x80000000 or xmit & 0x80000000):   # high bit => non-standard clock
                    up = _wrap_ms(recv_ts - orig)
                    down = _wrap_ms(rx_wall % 86400.0 * 1000.0 - xmit)
                    self.ts_ok[r] += 1
            self.recv[r] += 1
            self.samples.append((time.monotonic(), r, rtt, up, down))

    def drain(self) -> list:
        out = []
        while self.samples:
            try:
                out.append(self.samples.popleft())
            except IndexError:
                break
        return out

    def info(self) -> dict:
        return {r: {"mode": self.mode[r], "sent": self.sent[r], "recv": self.recv[r],
                    "timestamps": "kernel" if getattr(self, "kernel_ts", True) else "user-space"} for r in self.reflectors}


class Direction:
    def __init__(self, name: str, nominal_kbit: float, cfg: dict):
        L = cfg["link"]
        self.name = name
        self.nominal = nominal_kbit
        self.min = max(L.get("min_kbit", 0), L["min_fraction"] * nominal_kbit)
        self.max = L.get(f"max_fraction_{name}", L["max_fraction"]) * nominal_kbit
        self.base = L["start_fraction"] * nominal_kbit
        self.rate = self.base
        self.achieved = 0.0
        self.load = 0.0
        self.state = "idle"
        self.last_decrease = -1e9
        self.last_bloat = -1e9
        self.hold_decrease_until = 0.0
        self.hold_increase_until = 0.0
        self.bloat_rate = None             # throughput at which a standing queue last formed
        self.deltas: collections.deque = collections.deque(maxlen=64)
        self.delta = 0.0
        self.jitter = 0.0                  # RFC 3550-style smoothed |change| between consecutive probe delays
        # path-shift detector: a run of cuts that lowered our own throughput but not the delay
        self.streak = 0                    # consecutive cuts with a flat delay
        self.streak_delta0 = self.streak_ach0 = self.streak_rate0 = 0.0
        self.probe_until = 0.0             # drain probe in progress until this time
        self.probe_rate0 = 0.0
        self.last_probe = -1e9
        self.rebaselines = 0


class CapacityEstimator:
    def __init__(self, cfg: dict, emit, prober: Prober | None = None):
        self.cfg = cfg
        e = cfg["estimator"]
        self.e = e
        self.emit = emit
        self.down = Direction("down", cfg["link"]["down_mbit"] * 1000, cfg)
        self.up = Direction("up", cfg["link"]["up_mbit"] * 1000, cfg)
        self.enabled = bool(e["enabled"])
        if not self.enabled:
            for d in (self.down, self.up):
                d.rate = d.max          # static arm: fixed at the configured ceiling
                d.state = "static"
        self.prober = prober
        self.baseline: dict[tuple, float] = {}   # (reflector, kind) -> ms
        self.last_delta: dict[tuple, tuple] = {}  # (reflector, kind) -> (t, delta)
        self.last_raw: dict[tuple, float] = {}    # (reflector, kind) -> last raw delay, for jitter
        self.last_sample_t = time.monotonic()
        self.blind = False
        self.samples_total = 0
        self.decisions = 0

    def start(self) -> None:
        if not self.enabled:
            return
        if self.prober is None:
            self.prober = Prober(self.e["reflectors"], self.e["method"], self.e["probe_interval_ms"])
        if not self.prober.start():
            self.emit("estimator", f"{self.prober.error}; falling back to static rates", "warn")
            self.enabled = False
            for d in (self.down, self.up):
                d.rate = d.max
                d.state = "static"

    def _update_baseline(self, key, value: float, load: float) -> float:
        b = self.baseline.get(key)
        if b is None:
            self.baseline[key] = value
            return 0.0
        if value < b:
            b += self.e["baseline_alpha_down"] * (value - b)
        elif load < self.e["low_load"]:
            b += self.e["baseline_alpha_up"] * (value - b)
        self.baseline[key] = b
        return value - b

    def _jitter(self, d: Direction, key, value: float) -> None:
        prev = self.last_raw.get(key)
        self.last_raw[key] = value
        if prev is not None:
            d.jitter += (abs(value - prev) - d.jitter) / 16.0

    def ingest(self, samples: list) -> None:
        for t, r, rtt, up, down in samples:
            self.samples_total += 1
            self.last_sample_t = t
            if up is not None and down is not None:
                self._jitter(self.up, (r, "up"), up)
                self._jitter(self.down, (r, "down"), down)
                du = self._update_baseline((r, "up"), up, self.up.load)
                dd = self._update_baseline((r, "down"), down, self.down.load)
                self.up.deltas.append((t, max(-50.0, min(500.0, du))))
                self.down.deltas.append((t, max(-50.0, min(500.0, dd))))
                self.last_delta[(r, "up")] = (t, du)
                self.last_delta[(r, "down")] = (t, dd)
            else:
                self._jitter(self.down, (r, "rtt"), rtt / 2.0)
                d = self._update_baseline((r, "rtt"), rtt, max(self.up.load, self.down.load))
                d = max(-50.0, min(500.0, d))
                self.last_delta[(r, "rtt")] = (t, d)
                # RTT only: attribute to the loaded direction(s)
                hi = self.e["high_load"] * 0.8
                targets = [x for x in (self.down, self.up) if x.load >= hi] or \
                    [max((self.down, self.up), key=lambda x: x.load)]
                for x in targets:
                    x.deltas.append((t, d))

    # ------------------------------------------------------------------ path-shift check (drain probe)
    # Queueing delay falls when the queue drains; a longer path (failover to a 4G backup, a route change)
    # does not. When the delay stays flat across consecutive cuts we cannot tell the two apart from rate
    # changes alone (a full modem buffer is flat too), so we measure directly, like BBR's ProbeRTT: hold
    # the shaper at its floor for a moment so any queue drains, then read the delay. Still high -> the
    # path got longer: re-learn the baseline and undo the cuts. Low -> it was a queue: keep cutting.
    def _start_drain_probe(self, d: Direction, now: float) -> None:
        e = self.e
        d.probe_rate0 = d.rate
        d.rate = d.min
        d.probe_until = now + max(e.get("drain_probe_ms", 800) / 1000.0, 1.5 * max(0.0, d.delta) / 1000.0)
        d.last_probe = now
        d.state = "probe"

    def _finish_drain_probe(self, d: Direction, now: float) -> None:
        e = self.e
        win = e.get("drain_probe_read_ms", 300) / 1000.0
        recent = [x for (t, x) in d.deltas if d.probe_until - win <= t <= now]
        d.probe_until = 0.0
        m = min(recent) if len(recent) >= e["bloat_min_samples"] else None
        # a path change adds tens of ms (fibre -> 4G about 15-50); hundreds of ms after the hold means a deep queue
        # upstream that our hold has not drained yet (download senders back off slowly), not a longer path
        if m is not None and m > e.get("shift_max_ms", 100.0):
            # inconclusive: the queue upstream has not drained yet; allow another probe soon
            d.last_probe = now - e.get("drain_probe_every_s", 20.0) + e.get("drain_probe_retry_s", 4.0)
        # after the hold any queue of ours is gone, so a lasting residual of a few ms is the path itself (a path
        # change can sit right at the bloat threshold: 4G adds about 15 ms one way)
        if m is not None and e.get("shift_min_ms", 5.0) < m <= e.get("shift_max_ms", 100.0):
            for k in list(self.baseline):
                if k[1] in (d.name, "rtt"):
                    self.baseline[k] += m
            d.deltas.clear()
            # resume from where we were before the probe; load-driven probing climbs from there. (Restoring the
            # pre-streak rate overshoots when the path change came with a slower line, as a 4G failover does.)
            # never above what the link delivered when the flat-delay streak began (safe after a failover to a
            # slower line), never below where the probe started
            d.rate = min(d.max, max(d.min, d.probe_rate0, min(d.streak_rate0, d.streak_ach0)))
            d.bloat_rate = None
            d.last_bloat = now
            d.rebaselines += 1
            d.streak = 0
            d.hold_decrease_until = now + 1.0
            d.state = "rebaseline"
            self.decisions += 1
            self.emit("estimator", f"{d.name}: path delay rose {m:.0f} ms and stayed with our traffic held back "
                                   f"(failover or route change): baseline re-learned, rate back to "
                                   f"{d.rate / 1000:.1f} Mbit/s", "warn")
        else:
            # a real queue: it drained at the floor, so capacity is below where we were. Resume below the
            # throughput the link delivered while the queue stood.
            ref = min(d.probe_rate0, d.streak_ach0 or d.probe_rate0)
            d.rate = max(d.min, ref * (1.0 - e["decrease_factor"]))
            d.streak = 0
            d.hold_decrease_until = now + e["refractory_ms"] / 1000.0
            d.hold_increase_until = now + e["increase_hold_ms"] / 1000.0
            d.state = "bloat"

    def _bloated(self, d: Direction, now: float) -> bool:
        """CoDel-style standing-queue test: bloat only if EVERY delay sample of the last
        `bloat_window_ms` is above the threshold, i.e. the *minimum* rose. Transient spikes
        (probe jitter, a burst, CPU scheduling) never raise the minimum; a standing queue does."""
        win = self.e["bloat_window_ms"] / 1000.0
        recent = [x for (t, x) in d.deltas if now - t <= win]
        if not recent:
            return False
        d.delta = _median(recent)
        return len(recent) >= self.e["bloat_min_samples"] and min(recent) > self.e["bloat_threshold_ms"]

    def tick(self, now: float, achieved_down_kbit: float, achieved_up_kbit: float) -> tuple[float, float]:
        for d, a in ((self.down, achieved_down_kbit), (self.up, achieved_up_kbit)):
            d.achieved = a
            d.load = a / d.rate if d.rate > 0 else 0.0
        if not self.enabled:
            return self.down.rate, self.up.rate
        if self.prober is not None:
            self.ingest(self.prober.drain())
        blind = now - self.last_sample_t > 2.0
        if blind != self.blind:
            self.blind = blind
            self.emit("estimator", "reflectors silent: holding rates at or below base (blind mode)" if blind
                      else "reflectors responding again", "warn" if blind else "info")
        e = self.e
        for d in (self.down, self.up):
            old, old_state = d.rate, d.state
            if blind:
                d.rate = min(d.rate, d.base)
                d.state = "blind"
                continue
            bloat = self._bloated(d, now)
            # Cut deep enough to drain the queue we found within ~drain_target: a 400 ms queue needs a
            # bigger cut than a 20 ms one. f is bounded to [decrease_factor, max_decrease_factor].
            f = min(e["max_decrease_factor"], max(e["decrease_factor"], max(0.0, d.delta) / e["drain_target_ms"]))
            # time for that queue to drain once arrivals drop by f: delay / f
            drain = min(4.0, max(e["refractory_ms"] / 1000.0, (max(0.0, d.delta) / 1000.0) / f))
            if d.probe_until:
                if now < d.probe_until:
                    d.rate, d.state = d.min, "probe"
                    continue
                self._finish_drain_probe(d, now)
                continue
            if bloat and now >= d.hold_decrease_until:
                if (d.streak == 0 or now - d.last_decrease > e.get("shift_streak_gap_s", 8.0)
                        or d.delta < e.get("shift_delta_keep", 0.7) * d.streak_delta0
                        or d.delta > d.streak_delta0 / e.get("shift_delta_keep", 0.7)):
                    # (a delay that keeps growing is a filling queue, not a flat path change)
                    d.streak, d.streak_delta0, d.streak_ach0, d.streak_rate0 = 1, d.delta, d.achieved, d.rate
                else:
                    d.streak += 1
                if (d.streak >= e.get("shift_min_cuts", 3)
                        and now - d.last_probe >= e.get("drain_probe_every_s", 20.0)):
                    # (also at the floor: holding there for the probe time is the same test)
                    self._start_drain_probe(d, now)     # flat delay across cuts: is it a queue or the path?
                    continue
                if d.achieved >= max(d.min, 0.1 * d.rate) or d.delta >= e.get("deep_queue_ms", 100.0):
                    # (a deep queue while we push little: the line itself got much slower, e.g. a failover)
                    # the link delivered `achieved` while a queue was building: capacity is below that
                    ref = min(d.rate, max(d.achieved, d.min))
                    d.bloat_rate = ref
                else:
                    ref = d.rate      # bloat with almost no traffic of ours: gentle multiplicative cut
                d.rate = max(d.min, ref * (1.0 - f))
                d.last_decrease = d.last_bloat = now
                d.hold_decrease_until = now + drain      # do not cut again while the old queue drains
                d.hold_increase_until = max(now + e["increase_hold_ms"] / 1000.0, d.hold_decrease_until)
                d.state = "bloat"
            elif bloat:
                # still draining: if the queue turned out bigger than first measured, wait longer
                d.hold_decrease_until = max(d.hold_decrease_until, d.last_decrease + drain)
                d.last_bloat = now
                d.state = "bloat"
            elif d.load >= e["high_load"] and now >= d.hold_increase_until:
                # (the flat-delay streak is NOT reset here: a path change that sits near the threshold flickers
                # between bloat and healthy, and resetting would keep the drain probe from ever running)
                if d.bloat_rate and d.rate > 1.1 * d.bloat_rate:
                    d.bloat_rate = None          # we are well past the old ceiling without bloat: capacity grew
                if d.bloat_rate and d.rate >= 0.9 * d.bloat_rate and now - d.last_bloat < 30.0:
                    # close to where the queue last formed: probe gently (like CUBIC's plateau)
                    step = d.rate * e["slow_step_fraction"]
                else:
                    # far below any known ceiling: recover quickly (and close a big gap geometrically)
                    step = max(d.rate * e["increase_step_fraction"], d.max * e["increase_min_step_fraction"])
                    if d.bloat_rate:
                        step = max(step, 0.25 * (0.9 * d.bloat_rate - d.rate))
                d.rate = min(d.max, d.rate + step)
                d.state = "high"
            elif d.load < e["low_load"]:
                # drift back toward the plan rate only when no queue formed recently: right after a bloat or
                # a re-baseline the plan rate may no longer be the line's (a 4G backup), so only load-driven
                # probing ("high") may raise the rate then
                if now - d.last_bloat >= e.get("drift_quiet_s", 30.0):
                    d.rate += (d.base - d.rate) * 0.02
                d.state = "idle" if d.load < 0.1 else "low"
            else:
                d.state = "high" if d.load >= e["high_load"] else "low"
            if d.state != old_state and d.state == "bloat":
                self.decisions += 1
                self.emit("estimator", f"{d.name}: bufferbloat (+{d.delta:.0f} ms) at load {d.load:.2f} -> "
                                       f"rate {old / 1000:.1f} -> {d.rate / 1000:.1f} Mbit/s", "info")
        return self.down.rate, self.up.rate

    def info(self) -> dict:
        def dd(d: Direction):
            return {"rate_kbit": round(d.rate), "achieved_kbit": round(d.achieved), "load": round(d.load, 3),
                    "state": d.state, "delay_delta_ms": round(d.delta, 2), "rebaselines": d.rebaselines, "jitter_ms": round(d.jitter, 2), "min_kbit": round(d.min),
                    "max_kbit": round(d.max), "base_kbit": round(d.base), "nominal_kbit": round(d.nominal)}
        return {"enabled": self.enabled, "blind": self.blind, "down": dd(self.down), "up": dd(self.up),
                "baselines_ms": {f"{k[0]}/{k[1]}": round(v, 2) for k, v in self.baseline.items()},
                "reflectors": self.prober.info() if self.prober else {}, "samples": self.samples_total}
