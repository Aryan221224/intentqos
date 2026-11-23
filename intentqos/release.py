"""Signed release instructions: freshness, rollback protection and threshold signatures.

Design follows The Update Framework (TUF) in a reduced form that fits a gateway:

* **Targets** (what may be installed): a model's identity {version, sha256, size} is signed by the
  operator's *release keys*. With several release keys a threshold (k of n) must sign, so one stolen
  key cannot ship a model. Kept offline in production; the controller only stores the signatures.
* **Instruction** (what to do now): every config or model command is wrapped at delivery time in an
  envelope signed by the controller's *online key*: {kind, device, seq, issued, expires, body}.
    - ``device`` binds it to one gateway (no cross-device replay),
    - ``seq`` is a counter that only grows: a captured older instruction is refused (rollback attack),
    - ``expires`` is short, so a withheld or delayed instruction goes stale (freeze attack).
  An explicit return to an older model is still possible: the operator issues a NEW instruction for
  it, with a new ``seq``.

Pure Python (uses intentqos.ed25519); the router only verifies.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import ed25519

SKEW_S = 300.0              # tolerated clock difference between controller and gateway
INSTRUCTION_TTL_S = 900.0   # an instruction is valid this long after the controller signed it


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def keyid(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()[:16]


def sign_with(seed: bytes, payload) -> dict:
    """One signature record {keyid, sig} over the canonical form of `payload`."""
    _, pub = ed25519.keypair(seed)
    return {"keyid": keyid(pub), "sig": ed25519.sign(seed, canonical(payload)).hex()}


def verify_threshold(payload, sigs: list, pubkeys: list[bytes], threshold: int) -> tuple[bool, str]:
    """True when at least `threshold` DISTINCT trusted keys signed `payload` (each key counts once)."""
    if threshold < 1:
        return False, "threshold must be at least 1"
    by_id = {keyid(p): p for p in pubkeys}
    msg = canonical(payload)
    good: set[str] = set()
    for s in sigs or []:
        if not isinstance(s, dict):
            continue
        kid = s.get("keyid", "")
        pk = by_id.get(kid)
        if pk is None or kid in good:
            continue
        try:
            if ed25519.verify(pk, msg, bytes.fromhex(s.get("sig", ""))):
                good.add(kid)
        except ValueError:
            continue
    if len(good) >= threshold:
        return True, f"{len(good)} of {len(pubkeys)} release keys signed (threshold {threshold})"
    return False, f"only {len(good)} valid signature(s) from trusted release keys; {threshold} required"


def make_instruction(seed: bytes, kind: str, device: str, seq: int, body: dict, now: float | None = None,
                     ttl: float = INSTRUCTION_TTL_S) -> dict:
    now = time.time() if now is None else now
    env = {"kind": kind, "device": device, "seq": int(seq), "issued": round(now, 3),
           "expires": round(now + ttl, 3), "body": body}
    return {**env, "sig": sign_with(seed, env)}


def check_instruction(env: dict, kind: str, device: str | None, online_pub: bytes | None, last_seq: int,
                      now: float | None = None) -> str | None:
    """None when the envelope may be acted on; otherwise the reason it must be refused."""
    now = time.time() if now is None else now
    if not isinstance(env, dict) or "sig" not in env:
        return "unsigned instruction (no release envelope)"
    if online_pub is None:
        return "no release key configured on this gateway"
    body = {k: env.get(k) for k in ("kind", "device", "seq", "issued", "expires", "body")}
    ok, why = verify_threshold(body, [env.get("sig")], [online_pub], 1)
    if not ok:
        return "signature check failed: " + why
    if env.get("kind") != kind:
        return f"instruction is for {env.get('kind')!r}, not {kind!r}"
    if device and env.get("device") != device:
        return "instruction was issued for another gateway"
    try:
        seq, expires, issued = int(env["seq"]), float(env["expires"]), float(env["issued"])
    except (KeyError, TypeError, ValueError):
        return "malformed instruction"
    if seq <= int(last_seq or 0):
        return f"stale instruction (sequence {seq} <= last accepted {last_seq}): possible replay"
    if now > expires + SKEW_S:
        return f"instruction expired {now - expires:.0f} s ago (check the gateway clock)"
    if issued > now + SKEW_S:
        return "instruction issued in the future (check the gateway clock)"
    return None
