#!/usr/bin/env python3
"""Build installable OpenWrt/prplOS packages (.ipk) without the OpenWrt SDK.

    python3 router/build_ipk.py            -> router/dist/*.ipk

  intentqos_<ver>_all.ipk            the engine, watchdog, agent, console and model (pure Python, any CPU)
  iq-capture_<ver>_<arch>.ipk        the static capture helper for each CPU family (built by `make router`)

Install on a router:   opkg install iq-capture_*_<your arch>.ipk intentqos_*_all.ipk
                       uci set intentqos.engine.down_mbit=200 ; uci commit ; /etc/init.d/intentqos restart
An .ipk is a gzipped tar of debian-binary, control.tar.gz and data.tar.gz (what opkg reads).
OpenWrt 25.x moves to apk packages; the same files go into an APKBUILD.
"""
from __future__ import annotations

import glob
import gzip
import io
import os
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OW = os.path.join(ROOT, "router", "openwrt", "files")
DIST = os.path.join(ROOT, "router", "dist")
VERSION = "2.0.0-1"
# static helper binary -> OpenWrt package architectures it serves
ARCHES = {"aarch64": ["aarch64_cortex-a53", "aarch64_cortex-a72", "aarch64_generic"],
          "armv7": ["arm_cortex-a7_neon-vfpv4", "arm_cortex-a9_vfpv3-d16", "arm_cortex-a15_neon-vfpv4"],
          "mipsel": ["mipsel_24kc", "mipsel_74kc"],
          "x86_64": ["x86_64"]}
DEPENDS = ("python3-light, python3-email, python3-urllib, python3-openssl, python3-logging, tc-full, kmod-sched-core, "
           "kmod-sched-cake, nftables, iq-capture")


def _tar(entries: list[tuple[str, bytes, int]], gz: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w", format=tarfile.GNU_FORMAT) as t:
        dirs = set()
        for path, data, mode in entries:
            parts = path.strip("./").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "./" + "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo(d)
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
                    t.addfile(ti)
            ti = tarfile.TarInfo(path)
            ti.size, ti.mode, ti.mtime, ti.uname, ti.gname = len(data), mode, int(time.time()), "root", "root"
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def ipk(name: str, arch: str, files: list[tuple[str, bytes, int]], control: dict, scripts: dict | None = None,
        conffiles: list[str] | None = None) -> str:
    data = _tar(files)
    ctl = "".join(f"{k}: {v}\n" for k, v in {**control, "Package": name, "Version": VERSION, "Architecture": arch,
                                             "Installed-Size": str(sum(len(f[1]) for f in files))}.items())
    centries = [("./control", ctl.encode(), 0o644)]
    if conffiles:
        centries.append(("./conffiles", ("\n".join(conffiles) + "\n").encode(), 0o644))
    for sname, body in (scripts or {}).items():
        centries.append((f"./{sname}", body.encode(), 0o755))
    outer = _tar([("./debian-binary", b"2.0\n", 0o644), ("./control.tar.gz", _tar(centries), 0o644),
                  ("./data.tar.gz", data, 0o644)])
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, f"{name}_{VERSION}_{arch}.ipk")
    with open(path, "wb") as f:
        f.write(outer)
    return path


def read(p: str) -> bytes:
    with open(p, "rb") as f:
        return f.read()


def main() -> int:
    for old in glob.glob(os.path.join(DIST, "*.ipk")):
        os.remove(old)
    files = []
    for p in sorted(glob.glob(os.path.join(ROOT, "intentqos", "*.py"))):
        files.append((f"./usr/lib/intentqos/intentqos/{os.path.basename(p)}", read(p), 0o644))
    files.append(("./usr/lib/intentqos/intentqos/web/console.html", read(os.path.join(ROOT, "intentqos", "web", "console.html")), 0o644))
    files.append(("./usr/lib/intentqos/models/flowclf.iqm", read(os.path.join(ROOT, "models", "flowclf.iqm")), 0o644))
    files.append(("./etc/config/intentqos", read(os.path.join(OW, "intentqos.config")), 0o644))
    files.append(("./etc/init.d/intentqos", read(os.path.join(OW, "intentqos.init")), 0o755))
    files.append(("./usr/sbin/intentqos-uci", read(os.path.join(OW, "intentqos-uci")), 0o755))
    files.append(("./etc/hotplug.d/dhcp/90-intentqos",
                  read(os.path.join(ROOT, "router", "kit", "files", "etc", "hotplug.d", "dhcp", "90-intentqos")), 0o755))
    for obj in ("iq-flow.bpf.o", "iq-flow-eb.bpf.o"):              # kernel flow engine, opt-in (capture=ebpf)
        if os.path.isfile(os.path.join(ROOT, "router", obj)):
            files.append((f"./usr/lib/intentqos/{obj}", read(os.path.join(ROOT, "router", obj)), 0o644))
    post = "#!/bin/sh\n[ -n \"$IPKG_INSTROOT\" ] || { /etc/init.d/intentqos enable; /etc/init.d/intentqos restart; }\nexit 0\n"
    prerm = "#!/bin/sh\n[ -n \"$IPKG_INSTROOT\" ] || { /etc/init.d/intentqos stop; /etc/init.d/intentqos disable; }\nexit 0\n"
    out = [ipk("intentqos", "all", files, {"Depends": DEPENDS, "Section": "net", "License": "MIT", "Source": "intentqos",
                                           "Maintainer": "IntentQoS project", "SourceName": "intentqos",
                                           "Description": "Intent-aware adaptive QoS engine: latency-driven shaping, header-only "
                                                          "traffic classification, temporary priority leases, watchdog and "
                                                          "operator agent."},
               {"postinst": post, "prerm": prerm}, ["/etc/config/intentqos"])]
    for binname, arches in ARCHES.items():
        b = os.path.join(ROOT, "router", "bin", f"iq-capture.{binname}")
        if not os.path.exists(b):
            print(f"skipping {binname}: {b} not built (make router)")
            continue
        for arch in arches:
            out.append(ipk("iq-capture", arch, [("./usr/sbin/iq-capture", read(b), 0o755)],
                           {"Depends": "libc", "Section": "net", "License": "MIT", "Source": "intentqos",
                            "Maintainer": "IntentQoS project",
                            "Description": "Header-only flow accounting helper for IntentQoS (TPACKET_V3, static binary)."}))
    for p in out:
        print(f"{os.path.relpath(p, ROOT)}  {os.path.getsize(p) / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
