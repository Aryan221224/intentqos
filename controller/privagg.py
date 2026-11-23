"""Leader and helper aggregators for private fleet statistics (IETF DAP data flow, two-party additive shares).

Each gateway's telemetry record carries the LEADER share of its QoO histogram; the HELPER share goes to a
separately operated helper (agent `helper_url`), which runs `serve_helper` from this module. The leader
never stores a home's share next to its telemetry: shares are pulled out of the record on arrival and kept
in their own table keyed by a report id, so the two halves only ever meet as sums.

A total is released only for a batch (scope + time window) with at least MIN_BATCH distinct homes whose
reports reached BOTH aggregators; each aggregator sums only the agreed report ids and the two sums are
added. Without Prio3 validity proofs a dishonest gateway can still skew a total, so released totals are
checked for plausibility (every bin within 0..40 x homes, the per-home app cap) and flagged if not.

In the home kit, with no separate helper, the agent sends both shares to the console (flagged
`demo_both_shares`). That shows the data flow end to end but gives no privacy against the console, and
the operator view says so.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from intentqos import privagg

SCHEMA = """
CREATE TABLE IF NOT EXISTS agg_shares (role TEXT, task TEXT, report TEXT, gw INTEGER, scope TEXT, t REAL, share TEXT,
  PRIMARY KEY (role, task, report));
CREATE INDEX IF NOT EXISTS agg_shares_scope ON agg_shares (task, scope, t);
"""
APP_CAP = 40


def report_id(device_id: str, t: float) -> str:
    return hashlib.sha256(f"{device_id}|{round(float(t), 1)}".encode()).hexdigest()[:24]


class Aggregators:
    def __init__(self, db, helper_url: str = ""):
        self.db = db
        self.helper_url = helper_url.rstrip("/")        # separately operated helper; empty = local (home kit)
        with db.wlock:
            db.conn().executescript(SCHEMA)
            db.conn().commit()

    def take(self, gw: dict, scope: str, rec: dict) -> None:
        """Pull the shares out of a telemetry record (mutates rec) and file them per role."""
        agg = rec.pop("agg", None)
        if not isinstance(agg, dict) or agg.get("task") != "qoo_hist_v1":
            return
        rid = report_id(gw.get("device_id") or str(gw["id"]), rec["t"])
        self._put("leader", agg["task"], rid, gw["id"], scope, rec["t"], agg.get("leader"))
        if agg.get("helper") is not None:
            self._put("helper", agg["task"], rid, gw["id"], scope, rec["t"], agg["helper"])
            rec["agg_demo"] = True                  # this console holds both roles for this home

    def helper_upload(self, gw: dict, scope: str, body: dict) -> bool:
        if body.get("task") != "qoo_hist_v1":
            return False
        rid = report_id(gw.get("device_id") or str(gw["id"]), body.get("t", 0))
        return self._put("helper", body["task"], rid, gw["id"], scope, float(body.get("t", 0)), body.get("share"))

    def _put(self, role, task, rid, gw_id, scope, t, share) -> bool:
        n = len(privagg.CLASSES) * len(privagg.BINS)
        if not isinstance(share, list) or len(share) != n or not all(isinstance(x, int) for x in share):
            return False
        self.db.x("INSERT OR IGNORE INTO agg_shares (role, task, report, gw, scope, t, share) VALUES (?,?,?,?,?,?,?)",
                  (role, task, rid, gw_id, scope, t, json.dumps(share)))
        return True

    def _sum(self, role: str, task: str, reports: list[str]) -> list[int] | None:
        acc = None
        for i in range(0, len(reports), 500):
            chunk = reports[i:i + 500]
            rows = self.db.q(f"SELECT share FROM agg_shares WHERE role=? AND task=? AND report IN "
                             f"({','.join('?' * len(chunk))})", (role, task, *chunk))
            for r in rows:
                acc = privagg.add(acc, json.loads(r["share"]))
        return acc

    def collect(self, task: str, scope: str, start: float, end: float) -> dict:
        """Release the batch total if enough homes reached both aggregators; otherwise say how many did."""
        lead = self.db.q("SELECT report, gw FROM agg_shares WHERE role='leader' AND task=? AND scope=? AND t>=? AND t<?",
                         (task, scope, start, end))
        if self.helper_url:
            both = lead                       # the helper drops ids it never received and reports how many it summed
        else:
            help_ids = {r["report"] for r in self.db.q("SELECT report FROM agg_shares WHERE role='helper' AND task=? AND "
                                                        "scope=? AND t>=? AND t<?", (task, scope, start, end))}
            both = [r for r in lead if r["report"] in help_ids]
        homes = len({r["gw"] for r in both})
        out = {"scope": scope, "start": start, "end": end, "homes": homes, "reports": len(both),
               "min_homes": privagg.MIN_BATCH}
        if homes < privagg.MIN_BATCH:
            return {**out, "released": False, "why": f"fewer than {privagg.MIN_BATCH} homes in this batch"}
        ids = [r["report"] for r in both]
        hsum = self._helper_sum(task, ids)
        if hsum is None:
            return {**out, "released": False, "why": "the helper aggregator refused or could not be reached"}
        total = privagg.combine(self._sum("leader", task, ids), hsum)
        plausible = all(0 <= x <= APP_CAP * len(ids) for x in total)
        return {**out, "released": True, "plausible": plausible,
                "histogram": privagg.labelled(total) if plausible else None,
                "poor_share": _poor_share(total) if plausible else None}

    def _helper_sum(self, task: str, ids: list[str]) -> list[int] | None:
        if not self.helper_url:
            return self._sum("helper", task, ids)
        req = urllib.request.Request(self.helper_url + "/aggregate", method="POST",
                                     data=json.dumps({"task": task, "reports": ids}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read())
        except (OSError, ValueError):
            return None
        if res.get("count") != len(ids):      # helper lacks some shares: sums over different sets cannot be combined
            return None
        return res.get("sum")

    def fleet(self, scopes: list[str], hours: float = 1.0, now: float | None = None) -> list[dict]:
        t = time.time() if now is None else now
        return [self.collect("qoo_hist_v1", s, t - hours * 3600, t) for s in scopes]

    def prune(self, keep_s: float = 7 * 86400) -> None:
        self.db.x("DELETE FROM agg_shares WHERE t < ?", (time.time() - keep_s,))


def _poor_share(total: list[int]) -> dict:
    """Fraction of app-intervals below QoO 60, per class (the operator's headline number)."""
    nb = len(privagg.BINS)
    out = {}
    for ci, c in enumerate(privagg.CLASSES):
        row = total[ci * nb:(ci + 1) * nb]
        s = sum(row)
        if s:
            out[c] = round(sum(row[:3]) / s, 3)
    return out


# ---------------------------------------------------------------------------------------------- helper service
class Helper:
    """The helper aggregator: stores helper shares by report id; sums only batches of MIN_BATCH or more homes."""

    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.c.execute("CREATE TABLE IF NOT EXISTS shares (task TEXT, report TEXT, device TEXT, t REAL, share TEXT, "
                       "PRIMARY KEY (task, report))")
        self.c.commit()

    def upload(self, body: dict) -> bool:
        share, n = body.get("share"), len(privagg.CLASSES) * len(privagg.BINS)
        if body.get("task") != "qoo_hist_v1" or not isinstance(share, list) or len(share) != n or not body.get("device"):
            return False
        rid = report_id(str(body["device"]), body.get("t", 0))
        with self.lock:
            self.c.execute("INSERT OR IGNORE INTO shares VALUES (?,?,?,?,?)",
                           (body["task"], rid, str(body["device"]), float(body.get("t", 0)), json.dumps(share)))
            self.c.commit()
        return True

    def aggregate(self, task: str, reports: list[str]) -> dict:
        rows = []
        with self.lock:
            for i in range(0, len(reports), 500):
                chunk = reports[i:i + 500]
                rows += self.c.execute(f"SELECT device, share FROM shares WHERE task=? AND report IN "
                                       f"({','.join('?' * len(chunk))})", (task, *chunk)).fetchall()
        if len({d for d, _ in rows}) < privagg.MIN_BATCH:
            return {"error": f"batch smaller than {privagg.MIN_BATCH} homes", "count": len(rows)}
        acc = None
        for _d, sh in rows:
            acc = privagg.add(acc, json.loads(sh))
        return {"sum": acc, "count": len(rows)}


def serve_helper(path: str, host: str, port: int) -> ThreadingHTTPServer:
    h = Helper(path)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1 << 20)) or b"{}")
            except ValueError:
                body = {}
            if self.path == "/upload":
                out = {"ok": h.upload(body)}
            elif self.path == "/aggregate":
                out = h.aggregate(body.get("task", ""), [str(x) for x in body.get("reports", [])][:100000])
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer((host, port), H)
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IntentQoS helper aggregator (run by a party other than the ISP)")
    ap.add_argument("--db", default="helper.db")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8471)
    a = ap.parse_args(argv)
    print(f"helper aggregator on {a.host}:{a.port}, shares in {a.db} (put TLS in front of it in deployment)")
    serve_helper(a.db, a.host, a.port).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
