"""OpenWrt integration: turn the UCI config (/etc/config/intentqos) into the engine's TOML config.

    intentqos-uci > /var/etc/intentqos.toml          (called by /etc/init.d/intentqos on every start)

UCI is how OpenWrt and prplOS users (and LuCI) configure services; the engine keeps one config format
(TOML). This module reads `uci -q show intentqos` and writes the TOML. Only stdlib.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys

DEFAULT_ROOT = "/usr/lib/intentqos"


def parse_show(text: str) -> dict[str, dict]:
    """Parse `uci show` output into {section: {option: value | [values]}}."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        parts = key.split(".")
        if len(parts) == 2:
            out.setdefault(parts[1], {})["_type"] = val
            continue
        if len(parts) != 3:
            continue
        sec, opt = parts[1], parts[2]
        vals = shlex.split(val)
        out.setdefault(sec, {})[opt] = vals if len(vals) > 1 or opt in LISTS else (vals[0] if vals else "")
    return out


LISTS = {"lan_subnet", "reflector"}


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v))


def to_config(u: dict, root: str = DEFAULT_ROOT) -> dict:
    e, a = u.get("engine", {}), u.get("agent", {})

    def num(d, k, default):
        try:
            return float(d.get(k, default))
        except (TypeError, ValueError):
            return default
    lst = lambda d, k, default: d.get(k) if isinstance(d.get(k), list) else ([d[k]] if d.get(k) else default)  # noqa: E731
    cfg = {
        "interfaces": {"lan": e.get("lan", "br-lan"), "wan": e.get("wan", "wan"),
                       "lan_subnets": lst(e, "lan_subnet", ["192.168.1.0/24"])},
        "link": {"down_mbit": num(e, "down_mbit", 100.0), "up_mbit": num(e, "up_mbit", 20.0)},
        "estimator": {"enabled": e.get("estimator", "1") == "1", "reflectors": lst(e, "reflector", ["1.1.1.1", "8.8.8.8", "9.9.9.9", "2606:4700:4700::1111", "2001:4860:4860::8888"])},
        "classifier": {"mode": e.get("classifier", "ml"), "model_path": f"{root}/models/flowclf.iqm",
                       "overrides_path": "/etc/intentqos/overrides.json", "corrections_path": "/tmp/intentqos/corrections.jsonl"},
        # capture "ebpf" = kernel flow engine (needs tc with BPF support: opkg install tc-bpf or tc-full + kmod-sched-bpf)
        "capture": {"backend": e.get("capture", "auto"), "helper_path": "/usr/sbin/iq-capture",
                    "bpf_object": f"{root}/" + ("iq-flow-eb.bpf.o" if sys.byteorder == "big" else "iq-flow.bpf.o")},
        "enforce": {"backend": e.get("backend", "auto"), "nat": e.get("nat", "1") == "1",
                    "l4s": e.get("l4s", "1") == "1", "flow_offload": e.get("flow_offload", "off")},
        "outcome": {"enabled": e.get("outcome", "1") == "1"},
        "identity": {"dhcp_hook_path": "/tmp/intentqos/dhcp.jsonl"},
        "cellular": {"enabled": e.get("cellular", "0") == "1"},
        "api": {"listen": e.get("api_listen", "192.168.1.1:8080"),
                "require_auth": e.get("require_auth", "1") == "1",      # on by default on a router
                "key_file": "/etc/intentqos/admin.key"},
        "watchdog": {"heartbeat_path": "/var/run/intentqos/heartbeat.json", "timeout_s": num(e, "watchdog_timeout_s", 8.0),
                     "exit_state": e.get("exit_state", "safe")},
        "intents": {"store_path": "/etc/intentqos/intents.json"},
        "data": {"dir": "/tmp/intentqos", "state_dir": "/etc/intentqos"},
    }
    if a.get("enabled", "0") == "1":
        cfg["agent"] = {k: a[k] for k in ("controller_url", "claim_code", "release_pubkey", "ca_file", "client_cert", "client_key",
                                          "serial", "helper_url") if a.get(k)}
        cfg["agent"].update(state_path="/etc/intentqos/agent.json", models_dir="/etc/intentqos/models",
                            telemetry_interval_s=num(a, "telemetry_interval_s", 15),
                            # the engine API listens on the LAN address, not on loopback
                            engine_api="http://" + cfg["api"]["listen"], discover=a.get("discover", "0") == "1")
    return cfg


def dump_toml(cfg: dict) -> str:
    lines = ["# generated from /etc/config/intentqos by intentqos-uci; edit the UCI file, not this one"]
    for sec, opts in cfg.items():
        lines.append(f"\n[{sec}]")
        for k, v in opts.items():
            lines.append(f"{k} = {_toml(v)}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "--from-file":
        text = open(argv[1]).read()
    else:
        text = subprocess.run(["uci", "-q", "show", "intentqos"], capture_output=True, text=True).stdout
    sys.stdout.write(dump_toml(to_config(parse_show(text))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
