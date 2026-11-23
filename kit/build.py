#!/usr/bin/env python3
"""Build the IntentQoS home-kit programs into dist/kit/ (needs Go 1.22+, Python 3.11+, Pillow for icons).

    python3 kit/build.py                 # everything: Windows, Linux, macOS
    python3 kit/build.py --only windows  # one platform

Output
  dist/kit/windows/  IntentQoS Operator Console.exe, IntentQoS Router Setup.exe, IntentQoS Test Kit.exe,
                     IntentQoS 4G Bridge.exe
  dist/kit/linux/    intentqos-console, intentqos-router-setup, intentqos-testkit, intentqos-4g-bridge (x86-64)
  dist/kit/macos/    the same for Apple silicon and Intel (console uses the system Python 3.11+)
  dist/kit/router/   intentqos-router-<version>.tar.gz (what Router Setup installs; also for manual installs)

The Operator Console carries a private, trimmed Python runtime (python-build-standalone, downloaded once
into kit/.cache) and the controller code, so nothing has to be installed on the operator laptop.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from intentqos import __version__  # noqa: E402

CACHE = os.path.join(HERE, ".cache")
EMBED = os.path.join(HERE, "embed")
DIST = os.path.join(ROOT, "dist", "kit")
PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20260610/cpython-3.12.13+20260610-{}-install_only_stripped.tar.gz"
PY_TARGETS = {("windows", "amd64"): "x86_64-pc-windows-msvc", ("linux", "amd64"): "x86_64-unknown-linux-gnu"}
PARTS = {  # tag: (Windows name, Unix name)
    "console": ("IntentQoS Operator Console", "intentqos-console"),
    "router": ("IntentQoS Router Setup", "intentqos-router-setup"),
    "testkit": ("IntentQoS Test Kit", "intentqos-testkit"),
    "bridge": ("IntentQoS 4G Bridge", "intentqos-4g-bridge"),
}
PLATFORMS = {
    "windows": [("windows", "amd64", "windows")],
    "linux": [("linux", "amd64", "linux")],
    "macos": [("darwin", "arm64", "macos/apple-silicon"), ("darwin", "amd64", "macos/intel")],
}
TRIM = ("Lib/test/", "Lib/idlelib/", "Lib/tkinter/", "Lib/turtledemo/", "Lib/ensurepip/", "Lib/site-packages/", "Lib/lib2to3/",
        "tcl/", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll", "include/", "share/", "lib/tcl", "lib/tk", "lib/itcl",
        "lib/thread", "lib/libtcl", "lib/libtk", "bin/idle", "bin/pip", "bin/pydoc", "bin/2to3", "Scripts/", "libs/")
TRIM_ANY = ("/test/", "/tests/", "/idlelib/", "/tkinter/", "/turtledemo/", "/ensurepip/", "/site-packages/", "/lib2to3/",
            "/__pycache__/", "/config-3.12-", "/pydoc_data/")


def run(cmd, **kw):
    print("+", " ".join(cmd) if isinstance(cmd, list) else cmd)
    subprocess.run(cmd, check=True, **kw)


def payload() -> str:
    out = os.path.join(DIST, "router")
    os.makedirs(out, exist_ok=True)
    p = subprocess.run([sys.executable, os.path.join(ROOT, "router", "build_payload.py"), out], check=True,
                       capture_output=True, text=True).stdout.split()[0]
    shutil.copy(p, os.path.join(EMBED, "router-payload.tar.gz"))
    return p


def app_zip() -> None:
    """The controller code the Operator Console runs (plus the engine modules it imports and the model it signs)."""
    files = []
    for top in ("controller", "intentqos"):
        for dp, dn, fn in os.walk(os.path.join(ROOT, top)):
            dn[:] = [d for d in dn if d not in ("__pycache__", "data")]
            for f in fn:
                if f.endswith((".pyc",)) or f == "controller.toml":
                    continue
                files.append(os.path.relpath(os.path.join(dp, f), ROOT))
    files += ["models/flowclf.iqm", "models/flowclf.report.json", "LICENSE", "THIRD_PARTY_LICENSES.md"]
    with zipfile.ZipFile(os.path.join(EMBED, "app.zip"), "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(files):
            data = open(os.path.join(ROOT, f), "rb").read()
            if f.endswith((".py", ".html", ".js", ".json", ".md", ".txt", ".webmanifest")):
                data = data.replace(b"\r\n", b"\n")
            zi = zipfile.ZipInfo(f.replace(os.sep, "/"), (2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, data)
    print("app.zip", os.path.getsize(os.path.join(EMBED, "app.zip")) // 1024, "KiB")


def python_zip(goos: str, goarch: str) -> str:
    out = os.path.join(CACHE, f"python-{goos}-{goarch}.zip")
    if os.path.exists(out):
        return out
    trip = PY_TARGETS.get((goos, goarch))
    if not trip:
        open(out, "wb").close()          # macOS: the console uses the system Python
        return out
    os.makedirs(CACHE, exist_ok=True)
    tgz = os.path.join(CACHE, f"cpython-{trip}.tar.gz")
    if not os.path.exists(tgz):
        print("downloading", PBS.format(trip))
        urllib.request.urlretrieve(PBS.format(trip), tgz)
    kept = 0
    with tarfile.open(tgz) as tf, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for m in tf.getmembers():
            name = m.name
            rel = name.split("/", 1)[1] if "/" in name else ""
            if not rel or any(rel.startswith(t) for t in TRIM) or any(t in "/" + rel for t in TRIM_ANY):
                continue
            # Linux: bin/python3 is a static binary; drop its duplicate names and the libpython for embedding
            if rel in ("bin/python", "bin/python3.12", "bin/python3-config", "bin/python3.12-config") \
                    or rel.startswith("lib/libpython") or "_tkinter." in rel or "/_dbm." in rel:
                continue
            if m.isdir():
                continue
            if m.issym():                  # python-build-standalone uses a few symlinks (bin/python3 -> python3.12)
                target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
                try:
                    m2 = tf.getmember(target)
                except KeyError:
                    continue
                data, mode = tf.extractfile(m2).read(), m2.mode
            elif m.isfile():
                data, mode = tf.extractfile(m).read(), m.mode
            else:
                continue
            zi = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (mode & 0o777) << 16
            z.writestr(zi, data)
            kept += 1
    print(f"python runtime {goos}/{goarch}: {kept} files, {os.path.getsize(out) // 1024 // 1024} MiB")
    return out


def icons() -> dict:
    """One .ico per program (Windows), drawn from the app icon with a coloured corner mark."""
    from PIL import Image, ImageDraw
    base = Image.open(os.path.join(ROOT, "controller", "web", "pwa", "icon-512.png")).convert("RGBA")
    colours = {"console": (36, 89, 196), "router": (28, 124, 82), "testkit": (194, 90, 12), "bridge": (142, 124, 235)}
    out = {}
    os.makedirs(CACHE, exist_ok=True)
    for part, col in colours.items():
        im = base.copy()
        d = ImageDraw.Draw(im)
        d.ellipse([330, 330, 500, 500], fill=col, outline=(255, 255, 255), width=14)
        p = os.path.join(CACHE, f"{part}.ico")
        im.save(p, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        out[part] = p
    return out


def rsrc_tool() -> str | None:
    gobin = subprocess.run(["go", "env", "GOPATH"], capture_output=True, text=True).stdout.strip() + "/bin"
    exe = os.path.join(gobin, "rsrc")
    if not os.path.exists(exe):
        try:
            run(["go", "install", "github.com/akavel/rsrc@v0.10.2"], cwd=HERE)
        except subprocess.CalledProcessError:
            return None
    return exe if os.path.exists(exe) else None


def build(goos: str, goarch: str, outdir: str, ico: dict, rsrc: str | None) -> None:
    os.makedirs(outdir, exist_ok=True)
    for part, (wname, uname) in PARTS.items():
        if part == "console":
            shutil.copy(python_zip(goos, goarch), os.path.join(EMBED, "python.zip"))
        syso = os.path.join(HERE, "rsrc_windows_amd64.syso")
        if os.path.exists(syso):
            os.remove(syso)
        if goos == "windows" and rsrc:
            run([rsrc, "-ico", ico[part], "-arch", goarch, "-o", syso], cwd=HERE)
        name = wname + ".exe" if goos == "windows" else uname
        ld = f"-s -w -X main.version={__version__}" + (" -H windowsgui" if goos == "windows" else "")
        env = dict(os.environ, GOOS=goos, GOARCH=goarch, CGO_ENABLED="0")
        run(["go", "build", "-trimpath", "-tags", part, "-ldflags", ld, "-o", os.path.join(outdir, name), "."], cwd=HERE, env=env)
        if os.path.exists(syso):
            os.remove(syso)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(PLATFORMS), action="append")
    a = ap.parse_args()
    os.makedirs(EMBED, exist_ok=True)
    p = payload()
    print("router payload:", p)
    app_zip()
    ico = icons()
    rsrc = rsrc_tool()
    for plat in a.only or list(PLATFORMS):
        for goos, goarch, sub in PLATFORMS[plat]:
            build(goos, goarch, os.path.join(DIST, sub), ico, rsrc)
    with open(os.path.join(DIST, "SHA256SUMS.txt"), "w") as f:
        for dp, _, fn in sorted(os.walk(DIST)):
            for x in sorted(fn):
                if x == "SHA256SUMS.txt":
                    continue
                fp = os.path.join(dp, x)
                f.write(f"{hashlib.sha256(open(fp, 'rb').read()).hexdigest()}  {os.path.relpath(fp, DIST)}\n")
    print(open(os.path.join(DIST, "SHA256SUMS.txt")).read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
