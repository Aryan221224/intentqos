#!/usr/bin/env python3
"""Build the router payload that IntentQoS Router Setup installs (or that you install by hand over SSH).

    python3 router/build_payload.py          # -> dist/router/intentqos-router-<version>.tar.gz

The archive works with opkg and apk firmware alike: the installer pulls Python, tc and nftables from the
router's own package feeds and copies IntentQoS itself into place, so one archive covers OpenWrt 21.02 to
25.12, prplOS and GL.iNet firmware on ARM64, ARMv7, MIPS (little-endian) and x86-64.
Manual install:  scp the archive to /tmp, then on the router:
    cd /tmp && tar xzf intentqos-router-*.tar.gz && sh intentqos-router/install.sh --down 100 --up 100
"""
from __future__ import annotations

import io
import os
import re
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from intentqos import __version__  # noqa: E402

KIT = os.path.join(ROOT, "router", "kit")
OWRT = os.path.join(ROOT, "router", "openwrt", "files")
TOP = "intentqos-router"


def build(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"intentqos-router-{__version__}.tar.gz")
    entries: list[tuple[str, bytes, int]] = []

    def add(arc: str, src: str | None = None, data: bytes | None = None, mode: int = 0o644):
        if data is None:
            with open(src, "rb") as f:
                data = f.read()
        if arc.endswith((".sh", ".init", ".config", "-uci", "-ctl", ".py", ".html", ".txt", "VERSION")) or "init.d" in arc:
            data = data.replace(b"\r\n", b"\n")
        entries.append((f"{TOP}/{arc}", data, mode))

    add("install.sh", os.path.join(KIT, "install.sh"), mode=0o755)
    add("VERSION", data=(__version__ + "\n").encode())
    add("defaults/intentqos.config", os.path.join(OWRT, "intentqos.config"))
    for arch in ("aarch64", "armv7", "mipsel", "x86_64"):
        add(f"bin/iq-capture.{arch}", os.path.join(ROOT, "router", "bin", f"iq-capture.{arch}"), mode=0o755)
    lib = "files/usr/lib/intentqos"
    for obj in ("iq-flow.bpf.o", "iq-flow-eb.bpf.o"):          # kernel flow engine (opt-in: capture.backend=ebpf)
        src = os.path.join(ROOT, "router", obj)
        if os.path.isfile(src):
            add(f"{lib}/{obj}", src)
    for f in sorted(os.listdir(os.path.join(ROOT, "intentqos"))):
        if f.endswith(".py"):
            add(f"{lib}/intentqos/{f}", os.path.join(ROOT, "intentqos", f))
    add(f"{lib}/intentqos/web/console.html", os.path.join(ROOT, "intentqos", "web", "console.html"))
    for f in sorted(os.listdir(os.path.join(ROOT, "intentqos", "web", "fonts"))):
        add(f"{lib}/intentqos/web/fonts/{f}", os.path.join(ROOT, "intentqos", "web", "fonts", f))
    add(f"{lib}/models/flowclf.iqm", os.path.join(ROOT, "models", "flowclf.iqm"))
    add(f"{lib}/uninstall.sh", os.path.join(KIT, "files", "usr", "lib", "intentqos", "uninstall.sh"), mode=0o755)
    add("files/etc/init.d/intentqos", os.path.join(OWRT, "intentqos.init"), mode=0o755)
    # device identity: dnsmasq calls this on every DHCP lease with the client's requested options (fingerprint)
    add("files/etc/hotplug.d/dhcp/90-intentqos", os.path.join(KIT, "files", "etc", "hotplug.d", "dhcp", "90-intentqos"), mode=0o755)
    add("files/usr/sbin/intentqos-uci", os.path.join(OWRT, "intentqos-uci"), mode=0o755)
    add("files/usr/sbin/intentqos-ctl", os.path.join(KIT, "files", "usr", "sbin", "intentqos-ctl"), mode=0o755)

    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as tf:
        dirs = set()
        for arc, data, mode in entries:
            parts = arc.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo(d)
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, 1700000000
                    tf.addfile(ti)
            ti = tarfile.TarInfo(arc)
            ti.size, ti.mode, ti.mtime, ti.uid, ti.gid = len(data), mode, 1700000000, 0, 0
            tf.addfile(ti, io.BytesIO(data))
    return path


if __name__ == "__main__":
    p = build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "router"))
    print(f"{p} ({os.path.getsize(p) // 1024} KiB)")
