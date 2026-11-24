"""IntentQoS engine daemon: wires the modules into the observe -> decide -> enforce loop.

    python3 -m intentqos.daemon --config config/testbed.toml [--set classifier.mode=rules] ...

Loop (every estimator tick, default 250 ms):
  1. read shaper counters            -> achieved throughput, per-class rates/queues
  2. capacity estimator              -> shaper rate per direction
  3. every 500 ms: close flow windows, classify flows, run intent leases,
     map flows to classes, sync nftables marks
  4. policy.decide -> policy.shield  -> transactional apply (verify / rollback)
  5. starvation check, heartbeat for the watchdog, history + snapshot for the API
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys
import threading
import socket
import struct
import time
import traceback

from . import TIN_NAMES, __version__
from . import config as C
from .capture import WINDOW_S, FlowTable, make_capture
from .classifier import Classifier
from .console import DeviceRegistry, GradeTracker, ProcStats, Scheduler, activity
from .manage import Managed, UsageMeter
from .enforce import Enforcer, FlowMarker, choose_backend, safe_plan
from .estimator import CapacityEstimator
from .features import recent_rates
from .camara import QoDSessions
from .cellular import CellularCeiling
from .experience import ExperienceTracker
from .identity import Identity
from .intents import IntentManager
from .metrics import Events, History, demand_normalised_jain
from .offers import Offers
from .outcome import OutcomeController
from .policy import decide, demand_by_tin, flow_tins, shield, significant
from .wifi import BottleneckLocator


class Engine:
    def __init__(self, cfg: dict, timeseries_path: str | None = None):
        self.cfg = cfg
        self.t_start = time.time()
        data_dir = C.resolve(cfg, cfg["data"]["dir"])               # volatile: events, usage (tmpfs on routers)
        state_dir = C.resolve(cfg, cfg["data"].get("state_dir") or cfg["data"]["dir"])   # small files that must survive reboots
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        self.events = Events(os.path.join(data_dir, "events.jsonl"))
        self.emit = self.events.emit
        self.managed = Managed(os.path.join(state_dir, "managed.json"))
        skipped = self.managed.overlay(cfg)
        if skipped:
            self.emit("config", f"managed config entries ignored (invalid): {', '.join(skipped)}", "warn")
        self.usage = UsageMeter(os.path.join(data_dir, "usage.json"))
        I = cfg["interfaces"]
        self.lan, self.wan = I["lan"], I["wan"]
        self.devreg = DeviceRegistry(os.path.join(state_dir, "devices.json"), cfg.get("devices", {}))
        self.table = FlowTable(I["lan_subnets"])
        self.classifier = Classifier(cfg, lambda p: C.resolve(cfg, p), self.emit)
        self.estimator = CapacityEstimator(cfg, self.emit)
        self.intents = IntentManager(cfg, lambda p: C.resolve(cfg, p), self.emit)
        self.backend = choose_backend(cfg, self.lan, self.wan)
        self.marker = FlowMarker(self.backend, cfg, I["lan_subnets"])
        self.enforcer = Enforcer(self.backend, self.marker, self.emit)
        self.xp = ExperienceTracker(cfg)                     # how each app is doing (header-only)
        self.locator = BottleneckLocator(cfg)                # Wi-Fi, line or upstream, per device
        self.identity = Identity(cfg, os.path.join(state_dir, "identity.json"))   # device types from DHCP and MAC
        self.cell = CellularCeiling(cfg)                     # radio-aware ceiling when the router has a modem
        self.outcome = OutcomeController(cfg, self.xp, self.backend.name)
        self.offers = Offers()                               # proactive boost offers (household decides)
        self.qod = QoDSessions(self)                         # CAMARA-shaped Quality-on-Demand sessions
        self.idents: dict = {}
        self._last_ident = 0.0
        self._last_fade = 0.0
        self.probe_base_ms = cfg["estimator"]["probe_interval_ms"]
        self.passive_rtt_ps = 0.0
        self.scheduler = Scheduler(os.path.join(state_dir, "schedules.json"), self.intents, self.emit)
        self.grade = GradeTracker()
        self.proc = ProcStats()
        self.paused = False                 # operator hold: safe baseline, no adaptive changes
        self.bypass_until: float | None = None   # A/B test window: no shaping at all until this wall time
        self.bypass_by = None
        self.apply_log: collections.deque = collections.deque(maxlen=200)
        self._last_sched = 0.0
        self.history = History()
        self.snapshot: dict = {}
        self.decisions: dict = {}
        self.flow_tin: dict = {}
        self.flows: list = []
        self.active_intents: list = []
        self.demand = None
        self.shield_count = 0
        self.last_shield: list = []
        self.held = False
        self.prev_stats = None
        self.prev_t = None
        self.tin_view: dict = {"down": {}, "up": {}}
        self.loss: dict = {"down": 0.0, "up": 0.0}
        self.loss_rt: dict = {"down": 0.0, "up": 0.0}
        self.starve: dict = {}
        self.running = False
        self.paused_reason = None
        self.lock = threading.RLock()
        self.ts_path = timeseries_path
        self._ts = open(timeseries_path, "a") if timeseries_path else None
        hb = cfg["watchdog"]["heartbeat_path"]
        self.hb_path = hb
        self.wd_path = cfg["watchdog"].get("status_path") or os.path.join(os.path.dirname(hb), "watchdog.json")
        os.makedirs(os.path.dirname(hb), exist_ok=True)
        self.tick_s = cfg["estimator"]["tick_ms"] / 1000.0
        self._last_policy = 0.0
        self._last_refresh = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        rd = self.estimator.down.rate
        ru = self.estimator.up.rate
        plan = decide(self.cfg, {"down": rd, "up": ru}, _zero_demand(), [])
        shield(self.cfg, plan, None, self._bounds(), 0)
        self.enforcer.setup(plan)
        for ip, d in list(self.devreg.data.items()):        # re-apply household pauses
            pu = d.get("paused_until")
            if pu is not None and (pu == 0 or pu > time.time()):
                self.marker.pause(ip, None if pu == 0 else pu - time.time())
        self.capture = make_capture(self.cfg, self.lan, self.table, self.emit)
        if not hasattr(self.capture, "proc"):
            self.capture.start()
        self.marker.attach_kernel(self.capture)
        self.estimator.start()
        self.running = True
        self.emit("engine", f"IntentQoS {__version__} started: backend={self.backend.name}"
                            f"{'/' + getattr(self.backend, 'leaf', '') if self.backend.name == 'htb' else ''}, "
                            f"classifier={self.classifier.mode}"
                            f"{'' if self.classifier.ml or self.classifier.mode != 'ml' else ' (no model: rules)'}, "
                            f"estimator={'on' if self.estimator.enabled else 'off'}", "info")

    def stop(self, state: str | None = None) -> None:
        self.running = False
        self.capture.stop()
        if self.estimator.prober:
            self.estimator.prober.stop()
        state = state or self.cfg["watchdog"]["exit_state"]
        try:
            if state == "passthrough":
                self.backend.teardown()
            else:
                self.backend.setup(safe_plan(self.cfg))
            self.marker.teardown()
        except Exception as e:  # noqa: BLE001
            print(f"teardown error: {e}", file=sys.stderr)
        self.usage.save()
        self._heartbeat(stopped=True)
        self.emit("engine", f"engine stopped; left the gateway in '{state}' state", "info")
        if self._ts:
            self._ts.close()

    @property
    def devices(self) -> dict:
        """ip -> friendly name (config [devices] merged with names set in the console)."""
        return self.devreg.names()

    def _bounds(self) -> dict:
        e = self.estimator
        return {"down": (e.down.min, e.down.max), "up": (e.up.min, e.up.max)}

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        nxt = time.monotonic()
        while self.running:
            try:
                self.tick(time.monotonic())
            except Exception as e:  # noqa: BLE001 -- keep last-known-good policy, never crash the gateway
                msg = f"tick error ({type(e).__name__}: {e}); keeping last-known-good policy"
                self.emit("engine", msg, "error")
                traceback.print_exc()
                if self.snapshot:   # keep the console honest: fresh clock, the error, the event that says so
                    w = time.time()
                    self.snapshot = {**self.snapshot, "t": w, "uptime_s": round(w - self.t_start, 1),
                                     "error": msg[:400], "events": self.events.recent(80)}
            else:
                if self.snapshot and "error" in self.snapshot:
                    self.snapshot = {k: v for k, v in self.snapshot.items() if k != "error"}
            nxt += self.tick_s
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.monotonic()

    def _read_stats(self, now: float):
        st = self.backend.stats()
        ach = {"down": 0.0, "up": 0.0}
        if self.prev_stats is not None and self.prev_t is not None:
            dt = max(1e-3, now - self.prev_t)
            for d in ("down", "up"):
                cur, prev = st.get(d, {}), self.prev_stats.get(d, {})
                if cur.get("total") and prev.get("total"):
                    ach[d] = max(0.0, (cur["total"]["bytes"] - prev["total"]["bytes"]) * 8 / dt / 1000.0)
                view = {}
                pk = dr = pk_rt = dr_rt = 0
                plan = self.enforcer.applied or {}
                for name, t in cur.get("tins", {}).items():
                    p = prev.get("tins", {}).get(name, {})
                    kbit = max(0.0, (t["bytes"] - p.get("bytes", 0)) * 8 / dt / 1000.0)
                    if t.get("avg_delay_us") is not None:
                        qms = t["avg_delay_us"] / 1000.0
                    else:
                        tin_id = next((k for k, v in TIN_NAMES.items() if v == name), None)
                        g = plan.get(d, {}).get("guarantee", {}).get(tin_id, 0.0) if plan else 0.0
                        svc = max(kbit, g, 64.0)
                        qms = t.get("backlog", 0) * 8 / svc if t.get("backlog") else 0.0
                    dpk = max(0, t.get("packets", 0) - p.get("packets", 0))
                    ddr = max(0, t.get("drops", 0) - p.get("drops", 0))
                    pk += dpk
                    dr += ddr
                    if name in ("intent", "voice", "video"):
                        pk_rt += dpk
                        dr_rt += ddr
                    view[name] = {"kbit": round(kbit, 1), "backlog": t.get("backlog", 0), "drops": t.get("drops", 0),
                                  "drops_per_s": round(ddr / dt, 2), "queue_ms": round(qms, 2),
                                  "packets": t.get("packets", 0)}
                    # starvation detector: backlog but no service
                    key = (d, name)
                    if t.get("backlog", 0) > 0 and kbit == 0.0:
                        self.starve[key] = self.starve.get(key, 0.0) + dt
                    else:
                        self.starve[key] = 0.0
                self.tin_view[d] = view
                # drop rate at our shaper, smoothed (loss we cause on purpose to keep queues short)
                inst = dr / (pk + dr) if pk + dr else 0.0
                self.loss[d] = 0.8 * self.loss.get(d, 0.0) + 0.2 * inst
                # ... and in the realtime classes alone, which is what calls and games actually feel
                inst_rt = dr_rt / (pk_rt + dr_rt) if pk_rt + dr_rt else 0.0
                self.loss_rt[d] = 0.8 * self.loss_rt.get(d, 0.0) + 0.2 * inst_rt
        self.prev_stats, self.prev_t = st, now
        return ach

    def tick(self, now: float) -> None:
        with self.lock:
            if self.bypass_until is not None:
                if time.time() < self.bypass_until:
                    self._heartbeat()              # alive and deliberately not shaping: the watchdog stays quiet
                    return
                self.set_bypass(False, who="timer")
            self._tick(now)

    def set_bypass(self, on: bool, minutes: float = 5.0, who: str = "operator") -> dict:
        """Bounded 'no QoS' window for before/after measurements: all shaping and flow marks are removed
        and come back by themselves when the window ends (at most 30 minutes), or when it is switched off."""
        with self.lock:
            if on:
                minutes = max(0.25, min(30.0, float(minutes)))
                if self.bypass_until is None:
                    self.backend.teardown()
                    self.marker.teardown()
                    self._log_apply(time.time(), "bypass", self.enforcer.applied, self.enforcer.applied, [],
                                    f"shaping removed for a test by {who}")
                self.bypass_until = time.time() + minutes * 60
                self.bypass_by = who
                self.emit("engine", f"no-QoS test window for {minutes:g} min ({who}): shaping removed, "
                                    "comes back automatically", "warn")
            elif self.bypass_until is not None:
                self.bypass_until, self.bypass_by = None, None
                plan = decide(self.cfg, {"down": self.estimator.down.rate, "up": self.estimator.up.rate},
                              _zero_demand(), self.active_intents)
                shield(self.cfg, plan, None, self._bounds(), 0)
                self.enforcer.setup(plan)
                for ip, d in list(self.devreg.data.items()):
                    pu = d.get("paused_until")
                    if pu is not None and (pu == 0 or pu > time.time()):
                        self.marker.pause(ip, None if pu == 0 else pu - time.time())
                self.prev_stats = self.prev_t = None
                self.emit("engine", f"no-QoS test window ended ({who}): shaping restored", "info")
            return {"bypass": self.bypass_until is not None, "until": self.bypass_until, "by": self.bypass_by}

    def _wan_gateway(self) -> str | None:
        """The WAN's default gateway from /proc/net/route (cheap: no process, no netlink)."""
        try:
            with open("/proc/net/route") as f:
                for ln in f.readlines()[1:]:
                    p = ln.split()
                    if len(p) > 2 and p[0] == self.wan and p[1] == "00000000":
                        return socket.inet_ntoa(struct.pack("<I", int(p[2], 16)))
        except (OSError, ValueError):
            pass
        return None

    def _watch_watchdog(self, now: float) -> None:
        """If the watchdog stepped in (the engine was too slow), it removed the flow marks and installed the safe
        plan behind our back: put our shaping and marks back as soon as we are running again."""
        try:
            with open(self.wd_path) as f:
                trips = int(json.load(f).get("trips", 0))
        except (OSError, ValueError, TypeError):
            return
        seen = getattr(self, "_wd_trips_seen", None)
        self._wd_trips_seen = trips
        if seen is None or trips <= seen or self.bypass_until is not None or not self.enforcer.applied:
            return
        plan = self.enforcer.applied
        self.enforcer.setup(plan)              # cake + a fresh nft table
        self.marker.current.clear()
        n = self.marker.sync(self.flow_tin)
        self.emit("engine", f"watchdog had stepped in (engine too slow): shaping and {n} flow marks re-applied",
                  "warn")

    def _watch_wan(self, now: float) -> None:
        if now - getattr(self, "_wan_check_t", 0.0) < 1.0:
            return
        self._wan_check_t = now
        self._watch_watchdog(now)
        gw = self._wan_gateway()
        old = getattr(self, "_wan_gw", None)
        if gw and old and gw != old:
            self.estimator.path_changed(now, f"gateway {old} -> {gw}")
        if gw:
            self._wan_gw = gw

    def _tick(self, now: float) -> None:
        wall = time.time()
        self._watch_wan(now)
        ach = self._read_stats(now)
        rd, ru = self.estimator.tick(now, ach["down"], ach["up"])
        rd, ru = self._cellular(now, rd, ru)
        if now - self._last_policy >= WINDOW_S - 1e-3:
            self._last_policy = now
            self._policy_step(now, wall)
        dem = self.demand or _zero_demand()
        plan = decide(self.outcome.cfg_for(self.cfg), {"down": rd, "up": ru}, dem, self.active_intents)
        rep = shield(self.cfg, plan, self.enforcer.applied, self._bounds(), self.enforcer.changes_last_minute())
        if not rep.held and self.enforcer.applied and significant(plan, self.enforcer.applied,
                                                                   self.cfg["policy"]["apply_threshold"]):
            why = self.outcome.gate(plan, self.enforcer.applied, dem, self.active_intents)
            if why:
                # keep the applied guarantees (scaled to the new rate); rate changes still go through
                for d in ("down", "up"):
                    old = self.enforcer.applied[d]
                    k = plan[d]["rate"] / old["rate"] if old["rate"] else 1.0
                    plan[d]["guarantee"] = {t: v * k for t, v in old["guarantee"].items()}
                rep.interventions.append(why)
        if rep.interventions:
            self.shield_count += len(rep.interventions)
            self.last_shield = (rep.interventions + self.last_shield)[:10]
        self.held = rep.held
        if (not self.paused and not rep.held
                and significant(plan, self.enforcer.applied, self.cfg["policy"]["apply_threshold"])):
            prev = self.enforcer.applied
            if self.enforcer.apply(plan):
                self._log_apply(wall, "adapt", prev, plan, rep.interventions)
        for key, t in list(self.starve.items()):
            if t >= self.cfg["policy"]["starvation_s"]:
                self.starve[key] = 0.0
                prev = self.enforcer.applied
                self.enforcer.rollback(f"class {key[1]} ({key[0]}) starved for {t:.1f}s")
                self._log_apply(wall, "rollback", prev, self.enforcer.applied, [],
                                f"class {key[1]} ({key[0]}) starved for {t:.1f}s")
        self._heartbeat()
        if now - self._last_policy < 1e-6:
            self._record(wall, ach)

    def _cellular(self, now: float, rd: float, ru: float) -> tuple[float, float]:
        """Respect the radio's capacity on a cellular WAN; cut ahead of a sharp fade."""
        c = self.cell.poll(now)
        if not c:
            return rd, ru
        e = self.estimator
        if c["fade"] and now - self._last_fade > 5.0:
            self._last_fade = now
            e.down.rate = max(e.down.min, e.down.rate * 0.8)
            e.up.rate = max(e.up.min, e.up.rate * 0.8)
            self.emit("estimator", f"cellular fade (SINR {c['sinr']:.0f} dB): rates cut ahead of the queue", "info")
        cap = c["ceiling_kbit"]
        e.down.rate = min(e.down.rate, max(e.down.min, cap))
        e.up.rate = min(e.up.rate, max(e.up.min, cap * 0.5))
        return e.down.rate, e.up.rate

    def _experience_step(self, now: float, wall: float, flows: list) -> None:
        names = self.devices
        self.locator.update(flows, self.xp.lan, wall)
        self.xp.update(flows, self.decisions, self.flow_tin, self.estimator, self.tin_view, self.enforcer.applied,
                       self.loss_rt, names, self.locator.locate)
        self.outcome.step(self.cfg, {"down": self.estimator.down.rate, "up": self.estimator.up.rate},
                          self.demand or _zero_demand(), self.active_intents, self.enforcer.applied, now,
                          self.estimator)
        for o in self.offers.consider(self.xp.snapshot.get("apps", []), self.snapshot.get("devices", []),
                                      self.estimator, self.active_intents, names, wall):
            self.emit("offer", f"offered a boost for {o['device']}: {o['reason']}", "info")
        # passive-first probing: when real TCP flows give plenty of RTT samples, probe the reflectors less
        n = sum(getattr(f.hist[-1], "rw_n", 0) for f in flows if f.hist)
        self.passive_rtt_ps = 0.8 * self.passive_rtt_ps + 0.2 * (n / WINDOW_S)
        pr = self.estimator.prober
        if pr is not None:
            busy = self.passive_rtt_ps >= 10.0 and self.estimator.down.state not in ("bloat", "probe", "rebaseline")
            pr.interval = (self.probe_base_ms * (3 if busy else 1)) / 1000.0
        if now - self._last_ident >= 10.0:
            self._last_ident = now
            self.classifier.weak.maybe_refresh()
            try:
                self.idents = self.identity.refresh()
            except Exception as ex:  # noqa: BLE001
                self.emit("identity", f"device identity error: {ex}", "warn")

    def _policy_step(self, now: float, wall: float) -> None:
        self.table.roll_windows(now)
        flows = self.table.active(now)
        if now - self._last_sched >= 5.0:
            self._last_sched = now
            for ip, d in list(self.devreg.data.items()):
                pu = d.get("paused_until")
                if pu and pu < wall:
                    self.devreg.update(ip, paused_until=None)
                    self.emit("device", f"{d.get('name') or ip}: pause ended", "info")
            try:
                self.scheduler.tick(wall, self.estimator.down.rate, self.estimator.up.rate)
            except Exception as e:  # noqa: BLE001
                self.emit("schedule", f"scheduler error: {e}", "error")
        self.usage.add_window(flows, wall)
        with self.lock:
            self.decisions = self.classifier.classify(flows, now)
            acts = self.intents.tick(wall, self.estimator.down.rate, self.estimator.up.rate)
            self.flow_tin = flow_tins(flows, self.decisions, acts)
            self.demand = demand_by_tin(flows, self.flow_tin)
            self.flows = flows
            self.active_intents = acts
        self.marker.sync(self.flow_tin)
        if hasattr(self.backend, "tune_leaves") and self.enforcer.applied:
            for d in ("down", "up"):
                served = {k: v["kbit"] for k, v in self.tin_view.get(d, {}).items()}
                self.backend.tune_leaves(d, served, self.enforcer.applied[d]["guarantee"])
        if now - self._last_refresh > 20.0:
            self._last_refresh = now
            self.marker.refresh(self.flow_tin)
        for it in acts:
            self.intents.assure(it, self._device_measure(it.target))
        try:
            self._experience_step(now, wall, flows)
        except Exception as ex:  # noqa: BLE001 -- experience sensing must never stop shaping
            self.emit("experience", f"experience sensing error ({type(ex).__name__}: {ex})", "error")

    def _device_measure(self, ip: str) -> dict:
        fl = [f for f in self.flows if f.lan_ip == ip]
        up = dn = 0.0
        qms = 0.0
        for f in fl:
            u, d = recent_rates(f)
            up += u
            dn += d
            tin = self.flow_tin.get(f.key)
            if tin is not None:
                q = self.tin_view.get("down", {}).get(TIN_NAMES[tin], {}).get("queue_ms", 0.0)
                qms = max(qms, q)
        isp = max(0.0, self.estimator.down.delta) if self.estimator.enabled else 0.0
        return {"flows": len(fl), "down_kbit": dn, "up_kbit": up,
                "est_latency_ms": (isp + qms) if fl else None}

    # ------------------------------------------------------------------ change log
    def _explain(self, prev: dict | None, plan: dict | None) -> str:
        """Plain-language reason for a policy change, from what changed and the estimator's state."""
        if not prev or not plan:
            return "initial policy"
        parts = []
        why = {"bloat": "queue building at the modem (delay rose)", "high": "link busy and delay low: probing upward",
               "low": "light load: drifting back toward the plan rate", "idle": "idle: back to the plan rate",
               "blind": "no delay samples: holding (blind mode)", "static": "static rate",
               "rebaseline": "the path got longer (failover or route change): baseline re-learned, earlier cuts undone"}
        for d in ("down", "up"):
            a, b = prev[d]["rate"], plan[d]["rate"]
            if abs(b - a) >= 0.02 * max(a, 1):
                st = getattr(self.estimator, d).state
                parts.append(f"{d} rate {a / 1000:.1f}\u2192{b / 1000:.1f} Mbit/s ({why.get(st, st)})")
        for d in ("down", "up"):
            ga, gb = prev[d]["guarantee"], plan[d]["guarantee"]
            moved = []
            for tin in gb:
                x, y = ga.get(tin, 0.0), gb[tin]
                if abs(y - x) >= max(0.03 * plan[d]["rate"], 50):
                    sign = "+" if y > x else "\u2212"
                    moved.append(f"{TIN_NAMES[tin]} {sign}{abs(y - x) / 1000:.1f}")
            if moved:
                cause = "lease active" if self.active_intents and any("intent" in m for m in moved) else "demand changed"
                parts.append(f"{d} guarantees {', '.join(moved)} Mbit/s ({cause})")
        return "; ".join(parts) or "minor adjustment"

    def _log_apply(self, wall: float, kind: str, prev, plan, interventions, reason: str | None = None) -> None:
        if not plan:
            return
        self.apply_log.appendleft({
            "t": round(wall, 2), "kind": kind,
            "down": round(plan["down"]["rate"]), "up": round(plan["up"]["rate"]),
            "g_down": {TIN_NAMES[k]: round(v) for k, v in plan["down"]["guarantee"].items()},
            "reason": reason or self._explain(prev, plan),
            "shield": list(interventions)[:3]})

    def set_paused(self, paused: bool, who: str = "operator") -> None:
        if paused and not self.paused:
            prev = self.enforcer.applied
            self.go_safe(f"{who} paused automation")
            self.paused = True
            self._log_apply(time.time(), "pause", prev, self.enforcer.applied, [],
                            "automation paused: holding the safe baseline")
            self.emit("engine", f"automation paused by {who}: safe baseline held, no adaptive changes", "warn")
        elif not paused and self.paused:
            self.paused = False
            self.emit("engine", f"automation resumed by {who}", "info")

    def operator_rollback(self) -> dict:
        prev = self.enforcer.applied
        self.enforcer.rollback("operator requested rollback")
        self._log_apply(time.time(), "rollback", prev, self.enforcer.applied, [], "operator rollback to last-known-good")
        return {"ok": True, "applied": {d: round(p["rate"]) for d, p in (self.enforcer.applied or {}).items()}}

    # ------------------------------------------------------------------ outputs
    def _heartbeat(self, stopped: bool = False) -> None:
        hb = {"ts": time.time(), "pid": os.getpid(), "stopped": stopped, "backend": self.backend.name,
              "lan": self.lan, "wan": self.wan}
        ap = self.enforcer.applied if hasattr(self, "enforcer") else None
        if ap:
            hb["rates_kbit"] = {"down": round(ap["down"]["rate"]), "up": round(ap["up"]["rate"])}
        tmp = self.hb_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(hb, f)
            os.replace(tmp, self.hb_path)
        except OSError:
            pass

    def _record(self, wall: float, ach: dict) -> None:
        e = self.estimator
        plan = self.enforcer.applied or {}
        devs = self._devices()
        elastic = [d for d in devs if d["elastic"]]
        jn = None
        if len(elastic) >= 2:
            cap = e.down.rate
            jn = demand_normalised_jain([d["kbps_dn"] for d in elastic], [d["demand_dn"] for d in elastic], cap)
        row = {"t": round(wall, 3),
               "rd": round(e.down.rate), "ru": round(e.up.rate),
               "ad": round(ach["down"]), "au": round(ach["up"]),
               "dd": round(e.down.delta, 2), "du": round(e.up.delta, 2),
               "jd": round(e.down.jitter, 2), "ju": round(e.up.jitter, 2),
               "ld": round(self.loss["down"] * 100, 3), "lu": round(self.loss["up"] * 100, 3),
               "lrd": round(self.loss_rt["down"] * 100, 3), "lru": round(self.loss_rt["up"] * 100, 3),
               "sd": e.down.state, "su": e.up.state,
               "tin_d": {k: v["kbit"] for k, v in self.tin_view.get("down", {}).items()},
               "tin_u": {k: v["kbit"] for k, v in self.tin_view.get("up", {}).items()},
               "q_d": {k: v["queue_ms"] for k, v in self.tin_view.get("down", {}).items()},
               "g_d": {TIN_NAMES[k]: round(v) for k, v in plan.get("down", {}).get("guarantee", {}).items()},
               "jain": round(jn, 4) if jn is not None else None,
               "flows": len(self.flows), "intents": len(self.active_intents),
               "cpu": self.proc.sample()["cpu_pct"]}
        self.history.add(row)
        self.grade.add(wall, e.down.delta if e.enabled else 0.0, e.up.delta if e.enabled else 0.0)
        if self._ts:
            labs = {}
            for f in self.flows:
                d = self.decisions.get(f.id())
                if d:
                    labs[f.id()] = [d.label, d.committed, d.source, round(d.confidence, 3)]
            row2 = dict(row)
            row2["labels"] = labs
            self._ts.write(json.dumps(row2) + "\n")
            self._ts.flush()
        self.snapshot = self._build_snapshot(wall, devs, jn)

    def _devices(self) -> list[dict]:
        devs: dict[str, dict] = {}
        intent_by_ip = {it.target: it.id for it in self.active_intents}
        names0 = self.devices
        for f in self.flows:
            d = devs.setdefault(f.lan_ip, {"ip": f.lan_ip, "name": names0.get(f.lan_ip, ""), "kbps_up": 0.0,
                                           "kbps_dn": 0.0, "flows": 0, "labels": {}, "elastic": False,
                                           "demand_dn": 0.0, "intent": intent_by_ip.get(f.lan_ip)})
            u, dn = recent_rates(f)
            d["kbps_up"] += u
            d["kbps_dn"] += dn
            d["flows"] += 1
            dec = self.decisions.get(f.id())
            lab = dec.committed if dec else None
            d["labels"][lab or "?"] = d["labels"].get(lab or "?", 0) + 1
            if lab in ("bulk", "streaming", "web") and dn > 500:
                d["elastic"] = True
                d["demand_dn"] = 1e9      # elastic flows take whatever they get
        names = self.devices
        today = self.usage.days.get(time.strftime("%Y-%m-%d"), {})
        for ip in names:                     # known devices appear even when idle
            if ip not in devs and ip in self.devreg.data:
                devs[ip] = {"ip": ip, "name": names[ip], "kbps_up": 0.0, "kbps_dn": 0.0, "flows": 0, "labels": {},
                            "elastic": False, "demand_dn": 0.0, "intent": intent_by_ip.get(ip)}
        out = sorted(devs.values(), key=lambda x: (-(x["kbps_dn"] + x["kbps_up"]), x["name"] or x["ip"]))
        for d in out:
            d["kbps_up"] = round(d["kbps_up"], 1)
            d["kbps_dn"] = round(d["kbps_dn"], 1)
            reg = self.devreg.get(d["ip"])
            auto = self.idents.get(d["ip"], {})
            d["type"] = reg.get("type") or auto.get("type", "other")
            d["type_source"] = "household" if reg.get("type") else (auto.get("why") or "")
            d["activity"] = activity(d["labels"])
            pu = reg.get("paused_until")
            d["paused"] = pu is not None and (pu == 0 or pu > time.time())
            d["paused_until"] = pu if d["paused"] else None
            if d["paused"]:
                d["activity"] = "Paused"
            u = today.get(d["ip"])
            d["today_bytes"] = (u[0] + u[1]) if u else 0
        return out

    def _build_snapshot(self, wall: float, devs: list, jn) -> dict:
        plan = self.enforcer.applied or {}
        names = self.devices
        flows = []
        for f in self.flows:
            dec = self.decisions.get(f.id())
            u, dn = recent_rates(f)
            tin = self.flow_tin.get(f.key)
            flows.append({"id": f.id(), "lan_ip": f.lan_ip, "device": names.get(f.lan_ip, ""),
                          "remote": f.remote_ip, "proto": {6: "tcp", 17: "udp"}.get(f.proto, str(f.proto)),
                          "label": dec.label if dec else None, "committed": dec.committed if dec else None,
                          "restored": bool(dec and dec.restored),
                          "confidence": round(dec.confidence, 3) if dec else None,
                          "source": dec.source if dec else None, "pset": dec.pset if dec else [],
                          "tin": TIN_NAMES.get(tin) if tin is not None else None,
                          "kbps_up": round(u, 1), "kbps_dn": round(dn, 1), "age_s": round(time.monotonic() - f.first, 1)})
        flows.sort(key=lambda x: -(x["kbps_up"] + x["kbps_dn"]))
        return {
            "t": wall, "version": __version__, "uptime_s": round(wall - self.t_start, 1),
            "config": {"backend": self.backend.name, "leaf": getattr(self.backend, "leaf", None),
                       "lan": self.lan, "wan": self.wan,
                       "nominal_down_mbit": self.cfg["link"]["down_mbit"], "nominal_up_mbit": self.cfg["link"]["up_mbit"],
                       "classifier": self.classifier.mode, "estimator": self.estimator.enabled,
                       "adaptive_shares": self.cfg["policy"]["adaptive_shares"],
                       "bloat_threshold_ms": self.cfg["estimator"]["bloat_threshold_ms"],
                       "max_changes_per_minute": self.cfg["policy"]["max_changes_per_minute"],
                       "safe_fraction": self.cfg["policy"]["safe_fraction"]},
            "estimator": self.estimator.info(),
            "plan": {d: {"rate": round(p["rate"]), "guarantee": {TIN_NAMES[k]: round(v) for k, v in p["guarantee"].items()}}
                     for d, p in plan.items()},
            "tins": self.tin_view,
            "loss_pct": {d: round(v * 100, 3) for d, v in self.loss.items()},
            "loss_rt_pct": {d: round(v * 100, 3) for d, v in self.loss_rt.items()},
            "flows": flows[:80],
            "devices": devs,
            "fairness": {"jain": round(jn, 4) if jn is not None else None,
                         "devices": sum(1 for d in devs if d["elastic"])},
            "intents": self.intents.snapshot(wall),
            "events": self.events.recent(80),
            "safety": {"rollbacks": self.enforcer.rollbacks, "apply_failures": self.enforcer.failures,
                       "shield_interventions": self.shield_count, "recent_interventions": self.last_shield,
                       "held": self.held, "changes_last_minute": self.enforcer.changes_last_minute()},
            "classifier": self.classifier.info(),
            "capture": self.capture.stats(),
            "grade": self.grade.summary(),
            "paused": self.paused,
            "bypass": ({"until": self.bypass_until, "by": self.bypass_by} if self.bypass_until else None),
            "apply_log": list(self.apply_log)[:60],
            "schedules": self.scheduler.items,
            "budget": {"daily_min": self.intents.budget_min, "max_lease_s": self.intents.max_duration,
                       "max_boost_fraction": self.intents.max_boost,
                       "used_min": {r: round(self.intents.used_minutes(r, wall), 1) for r in self.intents.usage}},
            "floors": dict(self.cfg["policy"]["floor_fraction"]),
            "proc": self.proc.sample(),
            "managed": {k: v for k, v in self.managed.doc.items() if k != "set"},
            "experience": self.xp.snapshot,
            "bottlenecks": self.locator.snapshot(),
            "identity": self.idents,
            "outcome": self.outcome.info(),
            "l4s": {"mode": self.backend.l4s_mode(),
                    "flows": sum(1 for a in self.xp.snapshot.get("apps", []) if a.get("l4s"))},
            "cellular": self.cell.last or None,
            "offers": self.offers.open(wall),
            "camara_sessions": len(self.qod.sessions),
            "probing": {"interval_ms": round((self.estimator.prober.interval if self.estimator.prober else 0) * 1000),
                        "passive_rtt_per_s": round(self.passive_rtt_ps, 1)},
        }

    # ------------------------------------------------------------------ API actions
    def find_flow(self, fid: str):
        with self.lock:
            for f in self.flows:
                if f.id() == fid:
                    return f
        return None

    def load_model(self, path: str) -> dict:
        """Swap the classifier model in place (after the agent verified its signature)."""
        from .classifier import MLClassifier
        m = MLClassifier(path)                     # raises on a bad file or a different feature set
        old = self.classifier.ml.path if self.classifier.ml else None
        with self.classifier.lock:
            self.classifier.ml = m
            self.classifier.model_error = None
            self.classifier._init_aci()                # a new model starts from its own calibration
        self.cfg["classifier"]["model_path"] = path
        self.emit("model", f"classifier model switched to {os.path.basename(path)} "
                           f"({m.meta.get('trained_at', '?')}; previous {os.path.basename(old) if old else 'none'})", "info")
        return {"ok": True, "path": path, "previous": old, "meta": m.meta, "runtime": m.runtime}

    def pause_device(self, ip: str, minutes: float | None, who: str = "household") -> dict:
        until = 0 if not minutes else time.time() + minutes * 60
        self.marker.pause(ip, None if not minutes else minutes * 60)
        d = self.devreg.update(ip, paused_until=until)
        self.emit("device", f"{d.get('name') or ip} paused by {who}" + (f" for {minutes:.0f} min" if minutes else " until resumed"),
                  "info")
        return {"ok": True, "ip": ip, "paused_until": until}

    def resume_device(self, ip: str, who: str = "household") -> dict:
        self.marker.resume(ip)
        d = self.devreg.update(ip, paused_until=None)
        self.emit("device", f"{d.get('name') or ip} resumed by {who}", "info")
        return {"ok": True, "ip": ip}

    def go_safe(self, reason: str) -> None:
        self.enforcer.last_good = safe_plan(self.cfg)
        self.enforcer.rollback(reason)


def _zero_demand() -> dict:
    return {"down": {t: 0.0 for t in TIN_NAMES}, "up": {t: 0.0 for t in TIN_NAMES}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IntentQoS adaptive QoS engine")
    ap.add_argument("--config", default=None)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a config value, e.g. --set classifier.mode=rules")
    ap.add_argument("--timeseries", default=None, help="append per-tick JSON rows to this file")
    ap.add_argument("--no-api", action="store_true")
    a = ap.parse_args(argv)
    cfg = C.load(a.config)
    for kv in a.set:
        k, v = kv.split("=", 1)
        C.set_dotted(cfg, k, C.parse_value(v))
    if os.geteuid() != 0:
        print("IntentQoS must run as root (tc/nft/raw sockets)", file=sys.stderr)
        return 2
    eng = Engine(cfg, a.timeseries)

    def _sig(_s, _f):
        eng.running = False

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    eng.start()
    if not a.no_api:
        from .api import serve
        threading.Thread(target=serve, args=(eng,), daemon=True, name="api").start()
    try:
        eng.run()
    finally:
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
