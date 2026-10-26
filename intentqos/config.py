"""Configuration loading (TOML, stdlib only) with documented defaults."""
from __future__ import annotations

import copy
import os
import tomllib

DEFAULTS: dict = {
    "interfaces": {
        "lan": "eth1",                 # egress toward the home  => DOWNLOAD shaping happens here
        "wan": "eth0",                 # egress toward the ISP   => UPLOAD shaping happens here
        "lan_subnets": ["192.168.1.0/24"],
    },
    "link": {
        "down_mbit": 100.0,            # nominal plan rates (what the ISP sells)
        "up_mbit": 20.0,
        "start_fraction": 0.90,        # initial shaper rate as a fraction of nominal
        "min_fraction": 0.02,          # never shape below this fraction of the plan ... (bounded remediation;
        "min_kbit": 1000,              # ... nor below 1 Mbit/s. Low enough to shape a 4G backup link that is
                                       # a tenth of a symmetric fibre plan, e.g. 8 Mbit/s up on a 100/100 plan)
        "max_fraction": 0.95,          # never shape above this fraction of nominal (keeps the queue in our hands)
        "max_fraction_down": 0.90,     # download is shaped AFTER the bottleneck, so it needs more margin
        "max_fraction_up": 0.95,       # with the estimator disabled (static arm) the shaper sits at these ceilings
    },
    "estimator": {
        "enabled": True,
        "reflectors": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
        "method": "icmp-ts",           # icmp-ts (one-way delay per direction) | icmp-echo (RTT only)
        "probe_interval_ms": 100,
        "tick_ms": 200,
        "bloat_threshold_ms": 15.0,    # delay rise over baseline that counts as bufferbloat
        "bloat_window_ms": 400,        # bloat = the MINIMUM delay rise over this window exceeds the threshold
        "bloat_min_samples": 3,        # ... and the window holds at least this many samples
        "decrease_factor": 0.15,       # on bloat: new rate = achieved x (1 - f), f >= this ...
        "max_decrease_factor": 0.50,   # ... and <= this,
        "drain_target_ms": 1500,       # f = queue delay / drain_target, so big queues drain fast
        "increase_step_fraction": 0.015,  # per tick, multiplicative, when loaded and healthy
        "increase_min_step_fraction": 0.005,  # ... but at least this fraction of max rate per tick
        "slow_step_fraction": 0.003,   # within 10 % of the last bloat point: probe gently (~1.2 %/s)
        "high_load": 0.75,
        "low_load": 0.40,
        "refractory_ms": 750,          # minimum hold between decreases; extended to (delay / decrease_factor),
                                       # the time the queue we found needs to drain
        "increase_hold_ms": 2000,      # after a decrease, hold increases for this long
        "baseline_alpha_up": 0.002,    # slow upward drift of the unloaded delay baseline
        "baseline_alpha_down": 0.25,   # fast downward correction
        "shift_min_cuts": 3,           # path-shift check: after this many consecutive cuts with a flat delay ...
        "shift_delta_keep": 0.7,       # ... (each still >= 70 % of the first) ...
        "shift_streak_gap_s": 15.0,    # ... no more than this apart ...
        "drain_probe_ms": 800,         # ... hold the shaper at its floor this long so any queue drains,
        "drain_probe_read_ms": 300,    # ... read the delay over the end of the hold: still high = longer path
        "drain_probe_every_s": 20.0,   # at most one drain probe per direction in this period
        "shift_min_ms": 5.0,          # after the drain hold, a residual delay above this is the path itself
        "shift_max_ms": 100.0,        # a "longer path" above this is treated as a queue still draining
        "drain_probe_retry_s": 4.0,   # ... and the probe may run again this soon
        "deep_queue_ms": 100.0,       # a queue this deep: cut to what the line delivered, however little
        "drift_quiet_s": 30.0,        # drift back toward the plan rate only this long after the last queue
    },
    "capture": {
        "backend": "auto",             # auto: native helper (router/iq-capture) when present, else Python
        "helper_path": "",
    },
    "classifier": {
        "mode": "ml",                  # ml | rules  (ml falls back to rules if no model is present)
        "model_path": "models/flowclf.iqm",   # pure-Python router format (ml/export_model.py)
        "conformal_alpha": 0.10,       # 90% coverage prediction sets; abstain when the set has >1 class
        "commit_ticks": 2,             # a new label must persist N ticks before it moves a flow (anti-flap)
        "demote_ticks": 20,            # ... and N ticks (~10 s) before it moves a flow to a LOWER-priority tin:
                                       # a starved video stream looks like a download, and demoting it would
                                       # starve it further (lock-in), so demotion needs sustained evidence
        "bulk_trigger_pps": 100,       # rule baseline (qosify-inspired defaults)
        "bulk_trigger_s": 5.0,
        "prio_max_avg_pkt_len": 500,
        "overrides_path": "data/overrides.json",
        "labels_path": "",             # committed class per live flow, kept across an engine restart; default <data.dir>/labels.json
        "restore_max_age_s": 120,      # labels older than this are not restored (the flows will have ended)
        "corrections_path": "data/corrections.jsonl",
    },
    "policy": {
        "tick_ms": 500,
        "adaptive_shares": True,
        "floor_fraction": {"voice": 0.05, "video": 0.05, "besteffort": 0.10, "bulk": 0.05},
        "realtime_headroom": 1.3,      # reserve measured realtime demand x headroom
        "max_realtime_fraction": 0.60,
        "remainder_bulk_share": 1 / 3,  # what is left after floors and realtime: bulk gets this share, best effort the rest
        "max_rate_increase_per_tick": 0.15,
        "max_rate_decrease_per_tick": 0.50,
        "max_changes_per_minute": 90,
        "apply_threshold": 0.05,       # skip tc updates smaller than 5 % (hysteresis)
        "starvation_s": 3.0,           # a tin with backlog but no service for this long => rollback
        "safe_fraction": 0.85,         # the known-safe baseline shapes at 85 % of nominal, one fair class
    },
    "enforce": {
        "backend": "auto",             # auto | cake | htb
        "nat": False,                  # set true when the gateway NATs (CAKE nat keyword)
        "mark_dscp": True,             # also write DSCP so Wi-Fi WMM / CAKE diffserv honour the class
        "nft_table": "intentqos",
        "flow_timeout_s": 60,
    },
    "intents": {
        "max_duration_s": 8 * 3600,
        "default_duration_s": 3600,
        "daily_budget_min": 480,       # boost-minutes per requester per day (token bucket)
        "max_boost_fraction": 0.60,    # all intents together may reserve at most this share
        "default_demand_kbit": {"down": 3000, "up": 2000},
        "store_path": "data/intents.json",
    },
    "api": {
        "listen": "0.0.0.0:8080",
        "token_env": "INTENTQOS_TOKEN",  # if this env var is set, mutating calls need Bearer <token>
        "require_auth": False,         # True on a real router: every /api call needs the router key or a session
        "key_file": "",                # the router key (created 0600 on first start when require_auth is on)
    },
    "watchdog": {
        "heartbeat_path": "/run/intentqos/heartbeat.json",
        "timeout_s": 3.0,
        "exit_state": "safe",          # safe | passthrough  (what to leave behind on shutdown/failure)
    },
    "agent": {
        "controller_url": "",          # operator controller, e.g. https://qos.isp.example ; empty = standalone
        "ca_file": "",                 # operator CA bundle for the controller's TLS certificate
        "client_cert": "",             # optional mTLS client certificate/key (factory-provisioned)
        "client_key": "",
        "serial": "",                  # default: device-tree serial, DMI serial or WAN MAC
        "claim_code": "",              # one-time code printed on the router label (zero-touch enrolment)
        "release_pubkey": "",          # operator release key (Ed25519, hex) that signs configs and models
        "engine_api": "http://127.0.0.1:8080",
        "state_path": "data/agent.json",
        "models_dir": "data/models",   # models installed by the controller (the shipped one stays in models/)
        "telemetry_interval_s": 15,
        "buffer_records": 480,
    },
    "data": {
        "dir": "data",                 # volatile files (events, usage); on a router: /tmp/intentqos (RAM, no flash wear)
        "state_dir": "",               # files that must survive a reboot (devices, schedules, managed config); default = dir
        "timeseries": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None, overrides: dict | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    if overrides:
        cfg = _merge(cfg, overrides)
    # Relative paths (models/, data/) resolve against the project root when the config lives in
    # <project>/config/, otherwise against the working directory (e.g. systemd WorkingDirectory).
    base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    cfg["_root"] = os.path.dirname(base) if os.path.basename(base) == "config" else os.getcwd()
    return cfg


def resolve(cfg: dict, rel: str) -> str:
    """Resolve a project-relative path from the config."""
    return rel if os.path.isabs(rel) else os.path.join(cfg.get("_root", os.getcwd()), rel)


def set_dotted(cfg: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def parse_value(s: str):
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s
