#!/usr/bin/env python3
"""Build the Windows launchers into dist/ (runs on Linux, macOS or Windows; needs Go 1.21+).

    python3 launcher/build.py            # dist/IntentQoS Gateway.exe, ... (windows/amd64)
    python3 launcher/build.py --arm64    # also windows/arm64 builds in dist/arm64/

Steps: zip the project (tracked files, LF line endings, without docs images, run logs or the
launcher itself), embed it with launcher/wsl-setup.sh into launcher/go, draw an icon per part,
and cross-compile one small Go program five times with a different `main.part`. Icons need the
`rsrc` tool (github.com/akavel/rsrc, MIT) on PATH or in $RSRC; without it the exes get no icon.
No PyInstaller, no Python on Windows: each exe is a ~5 MB native program.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "launcher")
GO = os.path.join(HERE, "go")
DIST = os.path.join(ROOT, "dist")

PARTS = [  # part, file name, icon
    ("gateway", "IntentQoS Gateway", "gateway"),
    ("operator", "IntentQoS Operator Console", "operator"),
    ("app", "SampleNet Home App", "app"),
    ("demo", "IntentQoS Demo (all three)", "demo"),
    ("video", "IntentQoS Video Demo (no router)", "demo"),
    ("stop", "Stop IntentQoS", "stop"),
]
SKIP_PREFIX = ("launcher/", "dist/", "docs/img/", "docs/console-replay.html", "results/replay.json", "results/runs/", "ml/data/",
               "models/flowclf.joblib", ".github/")
TEXT = (".py", ".sh", ".toml", ".html", ".json", ".txt", ".md", ".service", ".c", ".conf", ".init", ".config")


def tracked_files() -> list[str]:
    try:
        out = subprocess.check_output(["git", "-C", ROOT, "ls-files", "--cached", "--others", "--exclude-standard"], text=True)
        files = [f for f in out.splitlines() if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for dp, dn, fn in os.walk(ROOT):
            dn[:] = [d for d in dn if d not in (".git", "__pycache__", "data", "results-lab", "dist", "build")]
            files += [os.path.relpath(os.path.join(dp, f), ROOT).replace(os.sep, "/") for f in fn]
    extra = ["router/bin/iq-capture.x86_64", "router/bin/iq-capture.aarch64"]      # built binaries, in case they are untracked
    return sorted({f for f in files + extra if os.path.isfile(os.path.join(ROOT, f)) and not f.startswith(SKIP_PREFIX)
                   and "__pycache__" not in f and not f.endswith((".pyc", ".exe", ".zip"))})


def build_zip(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in tracked_files():
            full = os.path.join(ROOT, f)
            data = open(full, "rb").read()
            if f.endswith(TEXT) or os.path.basename(f) in ("Makefile", "intentqos", "intentqos-uci"):
                data = data.replace(b"\r\n", b"\n")
            zi = zipfile.ZipInfo(f, date_time=(2026, 10, 3, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            exe = f.endswith(".sh") or f.startswith("router/bin/") or os.access(full, os.X_OK)
            zi.external_attr = (0o755 if exe else 0o644) << 16
            z.writestr(zi, data)
    print(f"app.zip: {os.path.getsize(path) // 1024} KiB")


def draw_icon(kind: str, path: str) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    marigold, indigo, slate = (242, 169, 59, 255), (29, 43, 94, 255), (18, 25, 34, 255)
    if kind == "gateway":
        d.rounded_rectangle([8, 8, S - 8, S - 8], 52, fill=marigold)
        for x, h in ((60, 120), (104, 84), (148, 150), (192, 62)):
            d.rounded_rectangle([x - 14, 196 - h, x + 14, 196], 12, fill=slate)
    elif kind == "operator":
        d.rounded_rectangle([8, 8, S - 8, S - 8], 52, fill=indigo)
        cols = [(79, 162, 114), (28, 124, 82), (201, 162, 39), (224, 123, 38), (28, 124, 82)]
        for i in range(4):
            for j in range(4):
                c = cols[(i * 3 + j * 2) % 5] if (i, j) != (1, 2) else (207, 74, 43)
                d.rounded_rectangle([44 + j * 44, 44 + i * 44, 44 + j * 44 + 36, 44 + i * 44 + 36], 7, fill=c + (255,))
    elif kind == "app":
        d.ellipse([8, 8, S - 8, S - 8], fill=marigold)
        d.arc([56, 92, 200, 236], 200, 340, fill=indigo, width=22)
        d.arc([88, 120, 168, 200], 200, 340, fill=indigo, width=20)
        d.ellipse([110, 160, 146, 196], fill=indigo)
    elif kind == "demo":
        d.rounded_rectangle([8, 8, S - 8, S - 8], 52, fill=slate)
        d.rounded_rectangle([40, 52, 216, 132], 14, fill=indigo)
        d.ellipse([52, 148, 116, 212], fill=marigold)
        d.rounded_rectangle([132, 148, 216, 212], 14, fill=(79, 162, 114, 255))
    else:  # stop
        d.rounded_rectangle([8, 8, S - 8, S - 8], 52, fill=(60, 66, 78, 255))
        d.rounded_rectangle([78, 78, 178, 178], 16, fill=(240, 112, 89, 255))
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm64", action="store_true")
    a = ap.parse_args()
    go = shutil.which("go")
    if not go:
        print("Go is needed: https://go.dev/dl/", file=sys.stderr)
        return 1
    build = os.path.join(HERE, "build")
    os.makedirs(build, exist_ok=True)
    os.makedirs(DIST, exist_ok=True)
    build_zip(os.path.join(GO, "app.zip"))
    shutil.copy(os.path.join(HERE, "wsl-setup.sh"), os.path.join(GO, "wsl-setup.sh"))
    rsrc = os.environ.get("RSRC") or shutil.which("rsrc")
    targets = [("amd64", DIST)] + ([("arm64", os.path.join(DIST, "arm64"))] if a.arm64 else [])
    for arch, out in targets:
        os.makedirs(out, exist_ok=True)
        for part, name, icon in PARTS:
            syso = os.path.join(GO, f"rsrc_windows_{arch}.syso")
            ico = os.path.join(build, f"{icon}.ico")
            if rsrc and draw_icon(icon, ico):
                subprocess.check_call([rsrc, "-ico", ico, "-arch", arch, "-o", syso])
            env = {**os.environ, "GOOS": "windows", "GOARCH": arch, "CGO_ENABLED": "0", "GOFLAGS": "-mod=mod",
                   "GOPROXY": "off"}
            exe = os.path.join(out, name + ".exe")
            subprocess.check_call([go, "build", "-trimpath", "-ldflags", f"-s -w -H windowsgui -X main.part={part}",
                                   "-o", exe, "."], cwd=GO, env=env)
            if os.path.exists(syso):
                os.remove(syso)
            print(f"{exe}  {os.path.getsize(exe) // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
