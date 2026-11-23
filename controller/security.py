"""Credentials, sessions, request signatures and the release signing key."""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time

from intentqos import ed25519, release
from intentqos.agent import canonical, sign_request

ROLES = {
    # role: what the role may do (checked in server.py)
    "viewer": {"read"},
    "support": {"read", "home_actions", "relay_read", "tickets", "notes", "line_test"},
    "noc": {"read", "home_actions", "relay_read", "relay_write", "tickets", "notes", "line_test", "incidents",
            "notify"},
    "admin": {"read", "home_actions", "relay_read", "relay_write", "tickets", "notes", "line_test", "incidents",
              "notify", "profiles", "rollouts", "models", "users", "privacy", "integrations"},
}


def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _alg, it, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, AttributeError):
        return False


def token(n: int = 24) -> str:
    return os.urandom(n).hex()


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class NonceCache:
    """Rejects a (device, nonce) seen within the signature validity window: no request replay."""

    def __init__(self, window_s: int = 300):
        self.window = window_s
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            if len(self.seen) > 200_000:
                self.seen = {k: t for k, t in self.seen.items() if now - t < self.window}
            if key in self.seen and now - self.seen[key] < self.window:
                return False
            self.seen[key] = now
            return True


def verify_agent(secret_hex: str, method: str, path: str, body: bytes, ts: str, nonce: str, sig: str,
                 nonces: NonceCache, device: str) -> str | None:
    """Returns None if valid, else the reason."""
    try:
        if abs(time.time() - int(ts)) > 300:
            return "timestamp outside the 5-minute window"
    except (TypeError, ValueError):
        return "missing timestamp"
    want = sign_request(bytes.fromhex(secret_hex), method, path, body, ts, nonce)
    if not hmac.compare_digest(want, sig or ""):
        return "bad signature"
    if not nonces.check(f"{device}:{nonce}"):
        return "replayed request"
    return None


class ReleaseKey:
    """The operator's Ed25519 release key. Signs config profiles and models that routers will accept.
    In production this lives in an HSM / KMS; here it is a file readable only by the controller."""

    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                self.seed = bytes.fromhex(f.read().strip())
        else:
            self.seed, _ = ed25519.keypair()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.seed.hex())
        _, self.pub = ed25519.keypair(self.seed)

    def sign(self, payload: dict) -> str:
        return ed25519.sign(self.seed, canonical(payload)).hex()

    def offline_seeds(self) -> list[bytes]:
        """Extra release keys (release-offline-*.key beside the online key) for k-of-n model signing. In
        production these stay offline and sign on a separate machine; files here are a lab stand-in."""
        d = os.path.dirname(self.path) or "."
        out = []
        for n in sorted(os.listdir(d)):
            if n.startswith("release-offline-") and n.endswith(".key"):
                with open(os.path.join(d, n)) as f:
                    out.append(bytes.fromhex(f.read().strip()))
        return out

    def targets_sigs(self, payload: dict) -> list[dict]:
        """Signatures over a model's identity {version, sha256, size}: the online key plus any offline keys."""
        return [release.sign_with(s, payload) for s in [self.seed] + self.offline_seeds()]

    def instruction(self, kind: str, device: str, seq: int, body: dict) -> dict:
        return release.make_instruction(self.seed, kind, device, seq, body)

    @property
    def pub_hex(self) -> str:
        return self.pub.hex()
