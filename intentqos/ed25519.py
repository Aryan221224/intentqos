"""Ed25519 signatures (RFC 8032) in pure Python, for signed configuration and model bundles.

Routers verify every bundle the controller sends (a new model, a policy profile) against the
operator's release public key before applying it, independently of TLS. Pure Python because the
gateway has no third-party crypto libraries; verification takes a few tens of milliseconds, which
is fine for an occasional bundle. The controller signs with the same module (or any RFC 8032
implementation). Constant-time behaviour is NOT a goal here: the router only verifies, which uses
no secret; signing happens on the controller, where a vetted library should be used in production.
"""
from __future__ import annotations

import hashlib
import os

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
I = pow(2, (P - 1) // 4, P)


def _h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = (x * I) % P
    if x % 2:
        x = P - x
    return x


_BY = (4 * pow(5, P - 2, P)) % P
_B = (_xrecover(_BY) % P, _BY, 1, (_xrecover(_BY) * _BY) % P)   # extended coordinates (X, Y, Z, T)
_ZERO = (0, 1, 1, 0)


def _add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s: int, p):
    q = _ZERO
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _enc(p) -> bytes:
    x, y, z, _t = p
    zi = pow(z, P - 2, P)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _dec(s: bytes):
    if len(s) != 32:
        raise ValueError("bad point length")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x * x * -1 + y * y - 1 - D * x * x * y * y) % P:
        raise ValueError("point not on curve")
    if x & 1 != sign:
        x = P - x
    return (x, y, 1, x * y % P)


def _eq(p, q) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _clamp(h: bytes) -> int:
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


def keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    """(private seed 32 bytes, public key 32 bytes)."""
    seed = seed or os.urandom(32)
    a = _clamp(_h(seed))
    return seed, _enc(_mul(a, _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    h = _h(seed)
    a = _clamp(h)
    pk = _enc(_mul(a, _B))
    r = int.from_bytes(_h(h[32:] + msg), "little") % L
    R = _enc(_mul(r, _B))
    k = int.from_bytes(_h(R + pk + msg), "little") % L
    s = (r + k * a) % L
    return R + s.to_bytes(32, "little")


def verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        if len(sig) != 64:
            return False
        A = _dec(pk)
        R = _dec(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_h(sig[:32] + pk + msg), "little") % L
        return _eq(_mul(s, _B), _add(R, _mul(k, A)))
    except ValueError:
        return False
