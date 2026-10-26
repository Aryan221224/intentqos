"""Seed a controller database for the lab: plans, staff accounts, an API key, config profiles, signed
models, the real lab home and a population of SIMULATED homes (clearly flagged as simulated).

    python3 -m controller.seed --config controller/controller.toml --sim 240

Every name, phone number and account here is fictional. Staff passwords are printed once and are
for the lab only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controller import security as S  # noqa: E402
from controller.core import Controller, jdump, now  # noqa: E402
from controller.server import load_config  # noqa: E402

PLANS = [("HOME-50", "Home 50", 50, 10, 240, 499), ("HOME-100", "Home 100", 100, 20, 480, 699),
         ("HOME-300", "Home 300", 300, 50, 600, 999), ("GIGA-1000", "Giga 1000", 1000, 200, 720, 2499)]
TOPOLOGY = {  # region -> pop -> OLTs (fictional naming, real place names for readability)
    "Greater Noida": {"GN-POP-1": ["GN-OLT-01", "GN-OLT-02", "GN-OLT-03"], "GN-POP-2": ["GN-OLT-07", "GN-OLT-08"]},
    "Noida": {"ND-POP-1": ["ND-OLT-11", "ND-OLT-12", "ND-OLT-13"]},
    "Ghaziabad": {"GZ-POP-1": ["GZ-OLT-21", "GZ-OLT-22"], "GZ-POP-2": ["GZ-OLT-25"]},
}
AREAS = {"Greater Noida": ["Knowledge Park II", "Alpha 1", "Pari Chowk", "Omicron 3", "Sector Mu"],
         "Noida": ["Sector 62", "Sector 18", "Sector 50", "Sector 137"],
         "Ghaziabad": ["Indirapuram", "Vaishali", "Raj Nagar Extension"]}
FIRST = ["Asha", "Ravi", "Priya", "Arjun", "Neha", "Vikram", "Kavya", "Rohan", "Isha", "Aditya", "Meera", "Karan", "Divya",
         "Sameer", "Ananya", "Nikhil", "Pooja", "Rahul", "Sneha", "Varun", "Tara", "Imran", "Farah", "Harpreet", "Joseph"]
LAST = ["Verma", "Sharma", "Gupta", "Iyer", "Khan", "Singh", "Nair", "Rao", "Mehta", "Das", "Reddy", "Bose", "Kapoor",
        "Joshi", "Menon", "Ali", "Thomas", "Chatterjee", "Pillai", "Saxena"]
PROFILES = [
    ("default", {"classifier.mode": "ml", "policy.floor_fraction.bulk": 0.05, "intents.max_boost_fraction": 0.6},
     "Balanced: realtime first, every class keeps a floor"),
    ("wfh-evening", {"classifier.mode": "ml", "policy.floor_fraction.video": 0.08, "policy.floor_fraction.bulk": 0.04,
                     "intents.max_boost_fraction": 0.6}, "More guaranteed room for video calls (work-from-home homes)"),
    ("bulk-friendly", {"classifier.mode": "ml", "policy.floor_fraction.bulk": 0.12, "policy.remainder_bulk_share": 0.5,
                       "intents.max_boost_fraction": 0.5},
     "Bigger floor for downloads and backups; for homes that complain about slow downloads"),
]


def _stamp(blob: bytes, release: str, note: str) -> bytes:
    """Write the release name into the model header (so each release is a distinct, signed file)."""
    import struct
    n = struct.unpack_from("<I", blob, 4)[0]
    hdr = json.loads(blob[8:8 + n])
    hdr["meta"]["release"] = release
    hdr["meta"]["note"] = note
    hj = json.dumps(hdr, separators=(",", ":")).encode()
    return blob[:4] + struct.pack("<I", len(hj)) + hj + blob[8 + n:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "controller", "controller.toml"))
    ap.add_argument("--sim", type=int, default=240, help="number of simulated homes")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--lab-serial", default="LAB-GW-0001")
    ap.add_argument("--lab-claim", default="LAB-CLAIM-0001")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if os.path.exists(os.path.join(cfg["data_dir"], "controller.db")):
        print("controller database exists; delete controller/data/controller.db* to re-seed", file=sys.stderr)
        return 1
    ctl = Controller(cfg)
    db = ctl.db
    rnd = random.Random(a.seed)
    for code, name, d, u, boost, price in PLANS:
        db.x("INSERT INTO plans (code, name, down_mbit, up_mbit, boost_minutes, price_inr) VALUES (?,?,?,?,?,?)",
             (code, name, d, u, boost, price))
    plan_id = {r["code"]: r["id"] for r in db.q("SELECT id, code FROM plans")}
    creds = {"admin": ("Admin (lab)", "admin"), "noc": ("Neha (NOC)", "noc"), "support": ("Rohan (Support)", "support"),
             "viewer": ("Viewer", "viewer")}
    pw_out = {}
    for user, (name, role) in creds.items():
        pw = f"{user}-{os.urandom(3).hex()}"
        pw_out[user] = pw
        db.x("INSERT INTO users (username, name, role, pw, created) VALUES (?,?,?,?,?)", (user, name, role, S.hash_pw(pw), now()))
    api_key = "nbk_" + S.token(16)
    db.x("INSERT INTO apikeys (name, key_hash, scopes, created) VALUES (?,?,?,?)", ("bss-lab", S.h(api_key), "nb", now()))
    for name, st, desc in PROFILES:
        db.x("INSERT INTO profiles (name, version, settings, description, created_by, created) VALUES (?,?,?,?,?,?)",
             (name, 1, jdump(st), desc, "seed", now()))
    # models: the shipped model, a retrain of it, and an experimental build used to demonstrate the rollout guard
    mdir = os.path.join(cfg["data_dir"], "models")
    os.makedirs(mdir, exist_ok=True)
    src = os.path.join(ROOT, "models", "flowclf.iqm")
    rep = json.load(open(os.path.join(ROOT, "models", "flowclf.report.json")))
    metrics = {"tin_accuracy_hybrid": rep["tin_level_accuracy"]["hybrid_conformal"], "rules": rep["tin_level_accuracy"]["rules"],
               "coverage": rep["conformal"]["empirical_coverage"], "trained_at": rep["trained_at"]}
    for ver, note in (("2026.10.01", "shipped with the firmware"), ("2026.10.02", "retrained with this week's corrections"),
                      ("2026.10.02-exp", "experimental feature set (lab: simulated homes regress on it)")):
        dst = os.path.join(mdir, f"flowclf-{ver}.iqm")
        blob = _stamp(open(src, "rb").read(), ver, note)
        with open(dst, "wb") as f:
            f.write(blob)
        sha = hashlib.sha256(blob).hexdigest()
        payload = {"version": ver, "sha256": sha, "size": len(blob)}
        db.x("INSERT INTO models (version, sha256, size, path, metrics, signature, created, note) VALUES (?,?,?,?,?,?,?,?)",
             (ver, sha, len(blob), dst, jdump(metrics), ctl.key.sign(payload), now(), note))
    default_pid = db.val("SELECT id FROM profiles WHERE name='default'")
    # the real lab home
    sid = db.x("INSERT INTO subscribers (account, name, phone, email, plan_id, region, pop, olt, pon, area, created) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("SN-100001", "Asha Verma", "9800000001", "asha@example.com",
                                                  plan_id["HOME-100"], "Greater Noida", "GN-POP-1", "GN-OLT-01", "1/1/3:12",
                                                  "Knowledge Park II", now() - 412 * 86400))
    db.x("INSERT INTO gateways (serial, claim_hash, subscriber_id, profile_id) VALUES (?,?,?,?)",
         (a.lab_serial, S.h(a.lab_claim), sid, default_pid))
    # simulated homes
    olts = [(r, p, o) for r, pops in TOPOLOGY.items() for p, os_ in pops.items() for o in os_]
    sims = []
    for i in range(a.sim):
        region, pop, olt = olts[i % len(olts)] if i < len(olts) * 4 else rnd.choice(olts)
        plan = rnd.choices(["HOME-50", "HOME-100", "HOME-300", "GIGA-1000"], [25, 45, 22, 8])[0]
        name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        acct = f"SN-{200000 + i}"
        phone = f"98{rnd.randint(10, 99)}{i:06d}"[:10]
        sid = db.x("INSERT INTO subscribers (account, name, phone, email, plan_id, region, pop, olt, pon, area, created) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (acct, name, phone, None, plan_id[plan], region, pop, olt,
                                                      f"1/{rnd.randint(1, 4)}/{rnd.randint(1, 16)}:{rnd.randint(1, 64)}",
                                                      rnd.choice(AREAS[region]), now()))
        serial, claim = f"SIM-{i:05d}", f"SIMCLAIM-{os.urandom(4).hex()}"
        db.x("INSERT INTO gateways (serial, claim_hash, subscriber_id, profile_id, simulated) VALUES (?,?,?,?,1)",
             (serial, S.h(claim), sid, default_pid))
        hh = rnd.choices(["light", "family", "wfh", "gamer", "streamer"], [20, 35, 20, 10, 15])[0]
        sims.append({"serial": serial, "claim": claim, "plan": plan, "down": dict((p[0], p[2]) for p in PLANS)[plan],
                     "up": dict((p[0], p[3]) for p in PLANS)[plan], "olt": olt, "household": hh,
                     "line_fault": rnd.random() < 0.012})
    with open(os.path.join(cfg["data_dir"], "sim-homes.json"), "w") as f:
        json.dump(sims, f)
    with open(os.path.join(cfg["data_dir"], "lab-credentials.txt"), "w") as f:
        f.write("IntentQoS controller (lab only)\n\nstaff sign-in:\n")
        for u, pw in pw_out.items():
            f.write(f"  {u:8s} {pw}\n")
        f.write(f"\nnorthbound API key: {api_key}\n\nconsumer app: phone 9800000001 (Asha Verma, the real lab home); "
                "the one-time code is shown on screen in demo mode\n")
    os.chmod(os.path.join(cfg["data_dir"], "lab-credentials.txt"), 0o600)
    print(open(os.path.join(cfg["data_dir"], "lab-credentials.txt")).read())
    print(f"release public key: {ctl.key.pub_hex}")
    print(f"seeded {a.sim} simulated homes on {len(olts)} OLTs + 1 real lab home ({a.lab_serial})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
