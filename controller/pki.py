"""Create the lab PKI: an operator CA and the controller's TLS certificate (openssl CLI).

    python3 -m controller.pki --dir controller/data/pki --ip 10.255.0.1 --ip 127.0.0.1 --dns localhost

Routers trust only this CA for the controller (agent `ca_file`). In production the CA lives in the
operator's PKI and routers additionally carry factory-installed client certificates (mTLS).
"""
from __future__ import annotations

import argparse
import os
import subprocess


def run(*a: str) -> None:
    subprocess.run(a, check=True, capture_output=True)


def make(d: str, ips: list[str], dns: list[str], days: int = 825) -> None:
    os.makedirs(d, exist_ok=True)
    ca_key, ca = os.path.join(d, "ca.key"), os.path.join(d, "ca.pem")
    if not os.path.exists(ca):
        run("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", ca_key,
            "-out", ca, "-days", "3650", "-subj", "/CN=IntentQoS lab operator CA")
    key, csr, crt = (os.path.join(d, n) for n in ("server.key", "server.csr", "server.pem"))
    san = ",".join([f"IP:{i}" for i in ips] + [f"DNS:{n}" for n in dns])
    ext = os.path.join(d, "san.ext")
    with open(ext, "w") as f:
        f.write(f"subjectAltName={san}\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n")
    run("openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", key, "-out", csr,
        "-subj", "/CN=IntentQoS controller")
    run("openssl", "x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key, "-CAcreateserial", "-out", crt,
        "-days", str(days), "-extfile", ext)
    for p in (ca_key, key):
        os.chmod(p, 0o600)
    os.remove(csr)
    print(f"CA: {ca}\nserver certificate: {crt} ({san})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="controller/data/pki")
    ap.add_argument("--ip", action="append", default=[])
    ap.add_argument("--dns", action="append", default=[])
    a = ap.parse_args()
    make(a.dir, a.ip or ["127.0.0.1"], a.dns or ["localhost"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
