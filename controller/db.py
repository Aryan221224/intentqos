"""Controller storage: SQLite (WAL) with a schema that maps 1:1 to a production database.

SQLite keeps the reference controller dependency-free and easy to run; every table and index here
has an obvious PostgreSQL/TimescaleDB equivalent for an operator-scale deployment (see
docs/CONTROLLER.md, "Scaling").
"""
from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, down_mbit REAL, up_mbit REAL,
  boost_minutes REAL, price_inr REAL);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, role TEXT, pw TEXT,
  created REAL, disabled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, kind TEXT, principal INTEGER, expires REAL, created REAL);
CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY, account TEXT UNIQUE, name TEXT, phone TEXT UNIQUE,
  email TEXT, plan_id INTEGER, region TEXT, pop TEXT, olt TEXT, pon TEXT, area TEXT, created REAL,
  consent TEXT DEFAULT '{}', status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS gateways (id INTEGER PRIMARY KEY, serial TEXT UNIQUE, claim_hash TEXT, device_id TEXT UNIQUE,
  secret TEXT, subscriber_id INTEGER, hw_model TEXT, arch TEXT, agent_version TEXT, engine_version TEXT,
  model_version TEXT, config_version INTEGER DEFAULT 0, desired_config_version INTEGER DEFAULT 0, profile_id INTEGER,
  level TEXT DEFAULT 'standard', level_until REAL, last_seen REAL, enrolled REAL, simulated INTEGER DEFAULT 0,
  buffered INTEGER DEFAULT 0, online INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS telemetry (gw INTEGER, t REAL, rec TEXT);
CREATE INDEX IF NOT EXISTS telemetry_gw_t ON telemetry (gw, t);
CREATE INDEX IF NOT EXISTS telemetry_t ON telemetry (t);
CREATE TABLE IF NOT EXISTS rollup5 (gw INTEGER, t REAL, grade TEXT, p95_add_d REAL, p95_add_u REAL, add_d REAL,
  jit_d REAL, loss_d REAL, rate_d REAL, ach_d REAL, plan_d REAL, cpu REAL, PRIMARY KEY (gw, t));
CREATE TABLE IF NOT EXISTS latest (gw INTEGER PRIMARY KEY, t REAL, rec TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, gw INTEGER, t REAL, kind TEXT, level TEXT, msg TEXT);
CREATE INDEX IF NOT EXISTS events_gw_t ON events (gw, t);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, gw INTEGER, rule TEXT, severity TEXT, opened REAL,
  updated REAL, closed REAL, status TEXT, detail TEXT, incident_id INTEGER, ack_by TEXT, streak INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (status, rule);
CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, scope_type TEXT, scope TEXT, kind TEXT, opened REAL,
  updated REAL, closed REAL, status TEXT, summary TEXT, affected INTEGER, total INTEGER, notified REAL, owner TEXT,
  notes TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY, gw INTEGER, kind TEXT, args TEXT, status TEXT,
  requested_by TEXT, created REAL, sent REAL, done REAL, result TEXT, rollout_id INTEGER);
CREATE INDEX IF NOT EXISTS commands_pending ON commands (gw, status);
CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY, name TEXT, version INTEGER, settings TEXT, description TEXT,
  created_by TEXT, created REAL, UNIQUE(name, version));
CREATE TABLE IF NOT EXISTS models (id INTEGER PRIMARY KEY, version TEXT UNIQUE, sha256 TEXT, size INTEGER, path TEXT,
  metrics TEXT, signature TEXT, created REAL, note TEXT);
CREATE TABLE IF NOT EXISTS rollouts (id INTEGER PRIMARY KEY, kind TEXT, target TEXT, name TEXT, stages TEXT,
  stage INTEGER DEFAULT 0, status TEXT, guardrails TEXT, created_by TEXT, created REAL, stage_started REAL,
  decisions TEXT DEFAULT '[]', previous TEXT);
CREATE TABLE IF NOT EXISTS rollout_members (rollout_id INTEGER, gw INTEGER, stage INTEGER, assigned REAL, status TEXT,
  PRIMARY KEY (rollout_id, gw));
CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, subscriber_id INTEGER, gw INTEGER, source TEXT, category TEXT,
  status TEXT, created REAL, updated REAL, summary TEXT, diagnosis TEXT, snapshot TEXT, assignee TEXT,
  thread TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, subscriber_id INTEGER, author TEXT, t REAL, text TEXT);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, t REAL, actor TEXT, actor_type TEXT, action TEXT,
  target TEXT, detail TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS audit_t ON audit (t);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, subscriber_id INTEGER, t REAL, kind TEXT, title TEXT,
  body TEXT, read INTEGER DEFAULT 0, ref TEXT);
CREATE INDEX IF NOT EXISTS notif_sub ON notifications (subscriber_id, t);
CREATE TABLE IF NOT EXISTS privacy_requests (id INTEGER PRIMARY KEY, subscriber_id INTEGER, kind TEXT, status TEXT,
  created REAL, due REAL, closed REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS otp (phone TEXT PRIMARY KEY, code_hash TEXT, expires REAL, attempts INTEGER, sent REAL);
CREATE TABLE IF NOT EXISTS webhooks (id INTEGER PRIMARY KEY, url TEXT, events TEXT, secret TEXT, created REAL,
  last_status TEXT);
CREATE TABLE IF NOT EXISTS apikeys (id INTEGER PRIMARY KEY, name TEXT, key_hash TEXT, scopes TEXT, created REAL);
"""


class DB:
    """One connection per thread; writes serialised with a lock (SQLite allows one writer)."""

    def __init__(self, path: str):
        self.path = path
        self.local = threading.local()
        self.wlock = threading.RLock()
        with self.wlock:
            c = self.conn()
            c.executescript(SCHEMA)
            c.commit()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self.local.c = c
        return c

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        r = self.conn().execute(sql, args).fetchone()
        return dict(r) if r else None

    def val(self, sql: str, args=()):
        r = self.conn().execute(sql, args).fetchone()
        return r[0] if r else None

    def x(self, sql: str, args=()) -> int:
        with self.wlock:
            c = self.conn()
            cur = c.execute(sql, args)
            c.commit()
            return cur.lastrowid

    def many(self, sql: str, rows) -> None:
        with self.wlock:
            c = self.conn()
            c.executemany(sql, rows)
            c.commit()

    def meta(self, k: str, default=None):
        v = self.val("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(v) if v is not None else default

    def set_meta(self, k: str, v) -> None:
        self.x("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
