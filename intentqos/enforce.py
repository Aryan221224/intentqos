"""Module 5 -- Enforcement: Linux `tc` shaping + nftables per-flow class marking.

Two interchangeable, fully inspectable backends:

* CakeBackend -- `cake diffserv4` with per-host isolation (dual-dsthost on the
  LAN side, dual-srchost on the WAN side, optional `nat`). The engine changes
  only the bandwidth and which tin each flow is in. Preferred on stock kernels.
* HtbBackend  -- a 5-class HTB tree (intent / voice / video / best-effort /
  bulk) with guaranteed rates (anti-starvation floors) and borrowing by
  priority; leaves use fq_codel when the kernel has it, else short bfifos.
  Lets the policy engine reserve bandwidth for intents explicitly.

Flow -> class mapping is an nftables verdict map keyed on the 5-tuple (both
directions) with element timeouts, so marks expire by themselves if the engine
dies. A mark becomes skb->priority (selects the HTB class / CAKE tin) and,
optionally, a DSCP code point (so Wi-Fi WMM and upstream gear can honour it).

Every change is a transaction: the last-known-good configuration is kept,
the new one is applied in one `tc -batch`, read back and verified, and on any
failure the last-known-good one is re-applied (bounded, observable, reversible).
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import threading
import time

from . import TIN_BE, TIN_BULK, TIN_INTENT, TIN_NAMES, TIN_VIDEO, TIN_VOICE

TINS = [TIN_INTENT, TIN_VOICE, TIN_VIDEO, TIN_BE, TIN_BULK]
HTB_CLASSID = {TIN_INTENT: 5, TIN_VOICE: 10, TIN_VIDEO: 20, TIN_BE: 30, TIN_BULK: 40}
HTB_PRIO = {TIN_INTENT: 0, TIN_VOICE: 0, TIN_VIDEO: 1, TIN_BE: 2, TIN_BULK: 3}
# CAKE diffserv4 priority-override minors are 1-based in display order: Bulk, Best Effort, Video, Voice
CAKE_MINOR = {TIN_INTENT: 4, TIN_VOICE: 4, TIN_VIDEO: 3, TIN_BE: 2, TIN_BULK: 1}
DSCP = {TIN_INTENT: "ef", TIN_VOICE: "ef", TIN_VIDEO: "af41", TIN_BE: "cs0", TIN_BULK: "cs1"}
MARK_BASE = 0x51000          # marks 0x51000..0x51004 -> tins 0..4 (leaves other mark bits for the firewall)


class TcError(RuntimeError):
    pass


def run(cmd: list[str], inp: str | None = None, check: bool = True) -> str:
    r = subprocess.run(cmd, input=inp, text=True, capture_output=True)
    if check and r.returncode != 0:
        detail = ""
        m = re.search(r"-:(\d+)", r.stderr or "")
        if inp and m:                      # name the batch line that failed, so the log says what broke
            lines = inp.splitlines()
            k = int(m.group(1)) - 1
            if 0 <= k < len(lines):
                detail = f" [line: {lines[k][:160]}]"
        raise TcError(f"{' '.join(cmd)} -> {r.returncode}: {r.stderr.strip()[:400]}{detail}")
    return r.stdout


def tc_json(cmd: list[str]):
    """Run a `tc -j ...` command and parse its JSON. Some iproute2 builds print text around (or instead
    of) the JSON; the JSON array is cut out when it is there, otherwise TcError says what came back."""
    out = run(cmd).strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except ValueError:
        i, j = out.find("["), out.rfind("]")
        if 0 <= i < j:
            try:
                return json.loads(out[i:j + 1])
            except ValueError:
                pass
        raise TcError(f"{' '.join(cmd)}: not JSON ({out[:120]!r})")


_UNIT = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}


def _bits(v: str) -> float:
    """'90Mbit', '4500Kbit', '800bit' -> bits per second."""
    m = re.match(r"([\d.]+)([kmgt]?)bit", v.strip().lower())
    return float(m.group(1)) * _UNIT[m.group(2)] if m else 0.0


def _bytes(v: str) -> int:
    """'1514b', '12Kb', '3Mb' -> bytes (tc's backlog sizes use powers of 1024)."""
    m = re.match(r"(\d+)([kmg]?)b", v.strip().lower())
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)] if m else 0


def htb_classes_text(text: str) -> list[dict]:
    """Parse `tc [-s] class show` text for HTB into the shape `tc -j` gives (rate in bytes/s)."""
    out = []
    for block in re.split(r"\n(?=class )", text.strip()):
        m = re.match(r"class htb (\S+) .*?\brate (\S+)", block)
        if not m:
            continue
        c = {"handle": m.group(1), "rate": _bits(m.group(2)) / 8}
        sm = re.search(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+)", block)
        bm = re.search(r"backlog (\S+) (\d+)p", block)
        if sm:
            c["stats"] = {"bytes": int(sm.group(1)), "packets": int(sm.group(2)), "drops": int(sm.group(3)),
                          "backlog": _bytes(bm.group(1)) if bm else 0, "qlen": int(bm.group(2)) if bm else 0}
        out.append(c)
    return out


def _kbit(x: float) -> str:
    return f"{max(8, int(x))}kbit"


class Backend:
    name = "base"

    def __init__(self, lan: str, wan: str, cfg: dict):
        self.lan, self.wan, self.cfg = lan, wan, cfg
        self.devs = {"down": lan, "up": wan}

    def setup(self, rates: dict) -> None: ...
    def apply(self, plan: dict) -> None: ...
    def stats(self) -> dict: ...

    def teardown(self) -> None:
        for dev in self.devs.values():
            run(["tc", "qdisc", "del", "dev", dev, "root"], check=False)

    def priority(self, tin: int) -> str: ...

    def probe_priority(self) -> str:
        """Where the router's own delay probes go: by default the top tin."""
        return self.priority(TIN_INTENT)

    def verify(self, plan: dict) -> bool:
        return True


class HtbBackend(Backend):
    name = "htb"

    def __init__(self, lan, wan, cfg, leaf: str | None = None):
        super().__init__(lan, wan, cfg)
        self.leaf = leaf or ("fq_codel" if self._probe_leaf(lan, "fq_codel") else "bfifo")
        self._leaf_limits: dict = {}

    @staticmethod
    def _probe_leaf(dev: str, kind: str) -> bool:
        # Test on a temporary handle under a temporary root, then restore nothing (setup() replaces root anyway)
        r = subprocess.run(["tc", "qdisc", "replace", "dev", dev, "root", "handle", "1:", kind],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        subprocess.run(["tc", "qdisc", "del", "dev", dev, "root"], capture_output=True)
        return ok

    def priority(self, tin: int) -> str:
        return f"1:{HTB_CLASSID[tin]}"

    # The router's delay probes get their own root-level class, outside the shaped parent 1:1, so they measure the
    # line's queue and not ours: under a low shaped rate even the top tin waits behind the packet being sent, and
    # an estimator that sees its own queue keeps cutting (found on the 4G backup link). Probes are a few kbit/s.
    PROBE_CLASSID = 99

    def probe_priority(self) -> str:
        return f"1:{self.PROBE_CLASSID}"

    @staticmethod
    def _burst(rate_kbit: float) -> int:
        return max(15140, int(rate_kbit * 1000 / 8 * 0.002))   # >= 2 ms of data at line rate

    # Without fq_codel the leaves are byte FIFOs. To keep their *queueing time* bounded (a poor
    # man's CoDel) each FIFO is re-sized every policy tick to `target` ms at the rate the class is
    # actually being served (max of measured throughput and its guarantee). See tune_leaves().
    LEAF_TARGET_MS = {TIN_INTENT: 10, TIN_VOICE: 10, TIN_VIDEO: 25, TIN_BE: 20, TIN_BULK: 25}
    LEAF_MIN, LEAF_MAX = 3 * 1514, 1_000_000

    def _leaf_limit(self, tin: int, service_kbit: float) -> int:
        lim = int(service_kbit * 1000 / 8 * self.LEAF_TARGET_MS[tin] / 1000)
        return max(self.LEAF_MIN, min(self.LEAF_MAX, lim))

    def _leaf_cmd(self, dev: str, tin: int, rate_kbit: float, verb: str = "replace") -> str:
        cid = HTB_CLASSID[tin]
        if self.leaf == "fq_codel":
            return f"qdisc {verb} dev {dev} parent 1:{cid} handle {cid}: fq_codel"
        return f"qdisc {verb} dev {dev} parent 1:{cid} handle {cid}: bfifo limit {self._leaf_limit(tin, rate_kbit)}"

    def tune_leaves(self, d: str, served_kbit: dict, guarantee: dict) -> int:
        """Re-size bfifo leaves to a time target at the class's current service rate."""
        if self.leaf != "bfifo":
            return 0
        dev = self.devs[d]
        cur = self._leaf_limits.setdefault(d, {})
        cmds = []
        for tin in TINS:
            svc = max(served_kbit.get(TIN_NAMES[tin], 0.0), guarantee.get(tin, 0.0))
            want = self._leaf_limit(tin, svc)
            have = cur.get(tin)
            if have is None or abs(want - have) / have > 0.3:
                cmds.append(f"qdisc change dev {dev} parent 1:{HTB_CLASSID[tin]} handle {HTB_CLASSID[tin]}: "
                            f"bfifo limit {want}")
                cur[tin] = want
        if cmds:
            run(["tc", "-batch", "-"], "\n".join(cmds) + "\n", check=False)
        return len(cmds)

    def _class_cmds(self, verb: str, dev: str, total: float, g: dict) -> list[str]:
        b = self._burst(total)
        cmds = [f"class {verb} dev {dev} parent 1: classid 1:1 htb rate {_kbit(total)} ceil {_kbit(total)} "
                f"burst {b} cburst {b} quantum 3028"]
        for tin in TINS:
            cmds.append(f"class {verb} dev {dev} parent 1:1 classid 1:{HTB_CLASSID[tin]} htb rate {_kbit(g[tin])} "
                        f"ceil {_kbit(total)} prio {HTB_PRIO[tin]} burst {b} cburst {b} quantum 3028")
        return cmds

    def setup(self, plan: dict) -> None:
        cmds = []
        for d, dev in self.devs.items():
            run(["tc", "qdisc", "del", "dev", dev, "root"], check=False)
            total = plan[d]["rate"]
            cmds.append(f"qdisc add dev {dev} root handle 1: htb default {HTB_CLASSID[TIN_BE]} r2q 10")
            cmds += self._class_cmds("replace", dev, total, plan[d]["guarantee"])
            cmds.append(f"class replace dev {dev} parent 1: classid 1:{self.PROBE_CLASSID} htb rate 256kbit "
                        f"ceil 10000000kbit prio 0 quantum 1514")
            for tin in TINS:
                cmds.append(self._leaf_cmd(dev, tin, plan[d]["guarantee"][tin]))
        run(["tc", "-batch", "-"], "\n".join(cmds) + "\n")
        self._leaf_limits = {d: {t: self._leaf_limit(t, plan[d]["guarantee"][t]) for t in TINS} for d in self.devs}

    def apply(self, plan: dict) -> None:
        cmds = []
        for d, dev in self.devs.items():
            cmds += self._class_cmds("change", dev, plan[d]["rate"], plan[d]["guarantee"])
        run(["tc", "-batch", "-"], "\n".join(cmds) + "\n")

    @staticmethod
    def classes(dev: str, stats: bool = False) -> list[dict]:
        """HTB classes of dev from `tc -j`, or from tc's text output where its JSON is unusable."""
        s = ["-s"] if stats else []
        try:
            return tc_json(["tc", "-j"] + s + ["class", "show", "dev", dev])
        except TcError:
            return htb_classes_text(run(["tc"] + s + ["class", "show", "dev", dev]))

    def verify(self, plan: dict) -> bool:
        for d, dev in self.devs.items():
            js = self.classes(dev)
            root = [c for c in js if c.get("handle") == "1:1"]
            if not root:
                return False
            want = plan[d]["rate"] * 1000 / 8
            got = float(root[0].get("rate", 0))
            if want > 0 and abs(got - want) / want > 0.05:
                self.verify_detail = f"{dev} 1:1 rate {got * 8 / 1000:.0f} kbit/s, wanted {want * 8 / 1000:.0f}"
                return False
        return True

    def stats(self) -> dict:
        out = {}
        for d, dev in self.devs.items():
            try:
                js = self.classes(dev, stats=True)
            except (TcError, ValueError):
                js = []
            per = {}
            total = None
            for c in js:
                h = c.get("handle", "")
                st = c.get("stats", {})
                rec = {"bytes": st.get("bytes", 0), "packets": st.get("packets", 0), "drops": st.get("drops", 0),
                       "backlog": st.get("backlog", 0), "qlen": st.get("qlen", 0)}
                if h == "1:1":
                    total = rec
                for tin, cid in HTB_CLASSID.items():
                    if h == f"1:{cid}":
                        per[TIN_NAMES[tin]] = rec
            out[d] = {"total": total, "tins": per}
        return out


class CakeBackend(Backend):
    name = "cake"

    def priority(self, tin: int) -> str:
        return f"1:{CAKE_MINOR[tin]}"

    def _qdisc(self, verb: str, d: str, rate: float) -> list[str]:
        dev = self.devs[d]
        iso = "dual-dsthost" if d == "down" else "dual-srchost"
        nat = " nat" if self.cfg["enforce"].get("nat") else ""
        ack = " ack-filter" if d == "up" else ""
        if verb == "change":
            return ["tc", "qdisc", "change", "dev", dev, "root", "cake", "bandwidth", _kbit(rate)]
        return ["tc", "qdisc", "replace", "dev", dev, "root", "handle", "1:", "cake", "bandwidth", _kbit(rate),
                "diffserv4", iso] + nat.split() + ack.split()

    def setup(self, plan: dict) -> None:
        for d in self.devs:
            run(self._qdisc("replace", d, plan[d]["rate"]))

    def apply(self, plan: dict) -> None:
        for d in self.devs:
            run(self._qdisc("change", d, plan[d]["rate"]))

    def verify(self, plan: dict) -> bool:
        for d, dev in self.devs.items():
            js = tc_json(["tc", "-j", "qdisc", "show", "dev", dev])
            root = [q for q in js if q.get("kind") == "cake"]
            if not root:
                return False
            got = float(root[0].get("options", {}).get("bandwidth", 0))
            want = plan[d]["rate"] * 1000 / 8
            if want > 0 and abs(got - want) / want > 0.05:
                return False
        return True

    def stats(self) -> dict:
        names = ["bulk", "besteffort", "video", "voice"]
        out = {}
        for d, dev in self.devs.items():
            try:
                js = tc_json(["tc", "-j", "-s", "qdisc", "show", "dev", dev])
            except (TcError, ValueError):
                js = []
            q = next((x for x in js if x.get("kind") == "cake"), None)
            per, total = {}, None
            if q:
                total = {"bytes": q.get("bytes", 0), "packets": q.get("packets", 0), "drops": q.get("drops", 0),
                         "backlog": q.get("backlog", 0), "qlen": q.get("qlen", 0)}
                for i, t in enumerate(q.get("tins", [])[:4]):
                    per[names[i]] = {"bytes": t.get("sent_bytes", 0), "packets": t.get("sent_packets", 0),
                                     "drops": t.get("drops", 0), "backlog": t.get("backlog_bytes", 0),
                                     "qlen": 0, "avg_delay_us": t.get("avg_delay_us"),
                                     "peak_delay_us": t.get("peak_delay_us")}
            out[d] = {"total": total, "tins": per}
        return out


class FlowMarker:
    """nftables verdict maps: 5-tuple -> mark -> skb->priority (+ DSCP)."""

    def __init__(self, backend: Backend, cfg: dict, lan_subnets: list[str]):
        self.backend = backend
        self.table = cfg["enforce"]["nft_table"]
        self.mark_dscp = cfg["enforce"]["mark_dscp"]
        self.timeout = int(cfg["enforce"]["flow_timeout_s"])
        self.current: dict[tuple, int] = {}
        self.lock = threading.Lock()
        self.lan_subnets = lan_subnets

    def setup(self) -> None:
        run(["nft", "delete", "table", "inet", self.table], check=False)
        prio_rules, dscp_rules = [], []
        for tin in TINS:
            m = MARK_BASE + tin
            prio_rules.append(f"    meta mark 0x{m:x} meta priority set {self.backend.priority(tin)}")
            if self.mark_dscp:
                dscp_rules.append(f"    meta mark 0x{m:x} ip dscp set {DSCP[tin]}")
                dscp_rules.append(f"    meta mark 0x{m:x} ip6 dscp set {DSCP[tin]}")
        top = self.backend.probe_priority()
        ruleset = f"""
table inet {self.table} {{
  map cls4 {{ type ipv4_addr . ipv4_addr . inet_proto . inet_service . inet_service : mark ; flags timeout ; }}
  map cls6 {{ type ipv6_addr . ipv6_addr . inet_proto . inet_service . inet_service : mark ; flags timeout ; }}
  chain iq_forward {{
    type filter hook forward priority mangle ; policy accept ;
    meta nfproto ipv4 meta mark set ip saddr . ip daddr . meta l4proto . th sport . th dport map @cls4
    meta nfproto ipv6 meta mark set ip6 saddr . ip6 daddr . meta l4proto . th sport . th dport map @cls6
{chr(10).join(dscp_rules)}
  }}
  set paused4 {{ type ipv4_addr ; flags timeout ; }}
  set paused6 {{ type ipv6_addr ; flags timeout ; }}
  chain iq_access {{
    type filter hook forward priority filter ; policy accept ;
    ip saddr @paused4 drop
    ip daddr @paused4 drop
    ip6 saddr @paused6 drop
    ip6 daddr @paused6 drop
  }}
  chain iq_postrouting {{
    type filter hook postrouting priority mangle ; policy accept ;
{chr(10).join(prio_rules)}
  }}
  chain iq_output {{
    type filter hook output priority mangle ; policy accept ;
    meta l4proto {{ icmp, ipv6-icmp }} meta priority set {top}
  }}
}}
"""
        run(["nft", "-f", "-"], ruleset)
        self.current.clear()

    def teardown(self) -> None:
        run(["nft", "delete", "table", "inet", self.table], check=False)
        self.current.clear()

    def pause(self, ip: str, seconds: float | None) -> None:
        """Block a device's internet access (household "pause"); seconds=None means until resumed."""
        st = "paused6" if ":" in ip else "paused4"
        run(["nft", "delete", "element", "inet", self.table, st, "{", ip, "}"], check=False)
        to = f" timeout {max(1, int(seconds))}s" if seconds else ""
        run(["nft", "-f", "-"], f"add element inet {self.table} {st} {{ {ip}{to} }}\n")

    def resume(self, ip: str) -> None:
        st = "paused6" if ":" in ip else "paused4"
        run(["nft", "delete", "element", "inet", self.table, st, "{", ip, "}"], check=False)

    @staticmethod
    def _keys(key: tuple) -> tuple[str, tuple, tuple]:
        lan, rem, proto, lp, rp = key
        fam = "cls6" if ":" in lan else "cls4"
        pn = {6: "tcp", 17: "udp"}.get(proto, str(proto))
        return fam, (lan, rem, pn, lp, rp), (rem, lan, pn, rp, lp)

    def sync(self, desired: dict[tuple, int], refresh: bool = False) -> int:
        """Make the nft maps match `desired` {flow_key: tin}. Returns number of commands issued.

        Unchanged elements are skipped unless `refresh` is set, in which case they are
        deleted and re-added in the same atomic batch to renew their timeout."""
        cmds = []
        with self.lock:
            for key, tin in desired.items():
                if key[2] not in (6, 17):
                    continue
                if self.current.get(key) == tin and not refresh:
                    continue
                fam, a, b = self._keys(key)
                if self.current.get(key) is not None:
                    cmds.append(f"delete element inet {self.table} {fam} {{ {a[0]} . {a[1]} . {a[2]} . {a[3]} . {a[4]}, "
                                f"{b[0]} . {b[1]} . {b[2]} . {b[3]} . {b[4]} }}")
                if tin == TIN_BE:
                    self.current.pop(key, None)   # best effort == unmarked default class
                    continue
                m = MARK_BASE + tin
                cmds.append(f"add element inet {self.table} {fam} {{ {a[0]} . {a[1]} . {a[2]} . {a[3]} . {a[4]} "
                            f"timeout {self.timeout}s : 0x{m:x}, {b[0]} . {b[1]} . {b[2]} . {b[3]} . {b[4]} "
                            f"timeout {self.timeout}s : 0x{m:x} }}")
                self.current[key] = tin
            for key in [k for k in self.current if k not in desired]:
                fam, a, b = self._keys(key)
                cmds.append(f"delete element inet {self.table} {fam} {{ {a[0]} . {a[1]} . {a[2]} . {a[3]} . {a[4]}, "
                            f"{b[0]} . {b[1]} . {b[2]} . {b[3]} . {b[4]} }}")
                self.current.pop(key, None)
        if not cmds:
            return 0
        r = subprocess.run(["nft", "-f", "-"], input="\n".join(cmds) + "\n", text=True, capture_output=True)
        if r.returncode != 0:
            # an element may have expired underneath us; fall back to one-by-one, ignoring benign errors
            for c in cmds:
                subprocess.run(["nft", "-f", "-"], input=c + "\n", text=True, capture_output=True)
        return len(cmds)

    def refresh(self, desired: dict[tuple, int]) -> None:
        """Renew element timeouts for every flow that is still marked."""
        self.sync(desired, refresh=True)


def choose_backend(cfg: dict, lan: str, wan: str) -> Backend:
    want = cfg["enforce"]["backend"]
    if want in ("cake", "auto"):
        r = subprocess.run(["tc", "qdisc", "replace", "dev", lan, "root", "handle", "1:", "cake"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return CakeBackend(lan, wan, cfg)
        if want == "cake":
            raise TcError("CAKE requested but this kernel has no sch_cake")
    return HtbBackend(lan, wan, cfg)


class Enforcer:
    """Transactional wrapper: apply -> verify -> commit, or roll back to last-known-good."""

    def __init__(self, backend: Backend, marker: FlowMarker, emit):
        self.backend = backend
        self.marker = marker
        self.emit = emit
        self.last_good: dict | None = None
        self.applied: dict | None = None
        self.changes: list[float] = []
        self.rollbacks = 0
        self.failures = 0

    def setup(self, plan: dict) -> None:
        self.backend.setup(plan)
        self.marker.setup()
        self.last_good = self.applied = plan
        self.emit("enforce", f"{self.backend.name} shaping active: down {plan['down']['rate'] / 1000:.1f} / "
                             f"up {plan['up']['rate'] / 1000:.1f} Mbit/s", "info")

    def apply(self, plan: dict) -> bool:
        t = time.monotonic()
        try:
            self.backend.apply(plan)
            if not self.backend.verify(plan):
                raise TcError("read-back verification failed: " + getattr(self.backend, "verify_detail", "?"))
        except TcError as e:
            self.failures += 1
            self.rollback(f"apply failed: {e}")
            return False
        self.applied = plan
        self.last_good = plan
        self.changes.append(t)
        self.changes = [x for x in self.changes if t - x < 60]
        return True

    def rollback(self, reason: str) -> None:
        self.rollbacks += 1
        self.emit("rollback", f"rolling back to last-known-good policy ({reason})", "warn")
        if self.last_good is None:
            return
        try:
            try:
                self.backend.apply(self.last_good)          # in-place, no packet loss
            except TcError:
                self.backend.setup(self.last_good)          # rebuild the tree if it was damaged
            self.applied = self.last_good
        except TcError as e:
            self.emit("rollback", f"rollback failed ({e})", "error")
            raise

    def changes_last_minute(self) -> int:
        t = time.monotonic()
        return sum(1 for x in self.changes if t - x < 60)


def safe_plan(cfg: dict) -> dict:
    """The known-safe state: shape at a conservative fraction of nominal, one fair best-effort class,
    every tin keeps its floor. Deterministic and independent of any estimate or model."""
    f = cfg["policy"]["safe_fraction"]
    plan = {}
    for d, nom in (("down", cfg["link"]["down_mbit"]), ("up", cfg["link"]["up_mbit"])):
        rate = nom * 1000 * f
        fl = cfg["policy"]["floor_fraction"]
        g = {TIN_INTENT: rate * 0.0001, TIN_VOICE: rate * fl["voice"], TIN_VIDEO: rate * fl["video"],
             TIN_BULK: rate * fl["bulk"]}
        g[TIN_BE] = rate - sum(g.values())
        plan[d] = {"rate": rate, "guarantee": g}
    return plan


def in_subnets(ip: str, nets) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)
