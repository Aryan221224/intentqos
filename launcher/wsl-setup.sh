#!/usr/bin/env bash
# Install or update IntentQoS inside WSL2 for the Windows launchers (runs as root, idempotent).
#
#   bash wsl-setup.sh <app.zip, as a WSL path> <release id>
#
# Unpacks the project into /opt/intentqos (keeping the controller database, keys and lab data),
# checks the few system tools the lab needs and installs only those that are missing. Nothing is
# installed with pip: the engine, agent and controller use the Python standard library only.
set -euo pipefail
ZIP=$1
REL=$2
APP=/opt/intentqos

say() { echo "[setup] $*"; }

if [ -x "$APP/scripts/stack.sh" ]; then
  bash "$APP/scripts/stack.sh" down all > /dev/null 2>&1 || true
fi
mkdir -p "$APP"

# --- system tools -----------------------------------------------------------------------------
need=()
command -v ip > /dev/null || need+=(iproute2)
command -v tc > /dev/null || need+=(iproute2)
command -v nft > /dev/null || need+=(nftables)
command -v openssl > /dev/null || need+=(openssl)
command -v python3 > /dev/null || need+=(python3)
if [ ${#need[@]} -gt 0 ]; then
  say "installing ${need[*]} (one time, needs internet)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq "${need[@]}" > /dev/null
fi

# Python 3.11 or newer (the code uses tomllib). Ubuntu 22.04 ships 3.10: add 3.11 next to it.
PY=python3
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  if ! command -v python3.11 > /dev/null; then
    say "installing python3.11 (one time, needs internet)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3.11 > /dev/null
  fi
  PY=python3.11
fi

# --- unpack -------------------------------------------------------------------------------------
say "unpacking release $REL"
"$PY" - "$ZIP" "$APP" <<'PYEOF'
import os, sys, zipfile
zp, app = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zp) as z:
    for m in z.infolist():
        if m.is_dir():
            continue
        dst = os.path.join(app, m.filename)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with z.open(m) as src, open(dst, "wb") as out:
            out.write(src.read())
        mode = (m.external_attr >> 16) & 0o777
        if mode:
            os.chmod(dst, mode)
PYEOF
cd "$APP"
chmod +x scripts/*.sh router/bin/* 2>/dev/null || true
case "$(uname -m)" in
  x86_64) cp router/bin/iq-capture.x86_64 router/iq-capture ;;
  aarch64) cp router/bin/iq-capture.aarch64 router/iq-capture ;;
esac
chmod +x router/iq-capture 2>/dev/null || true
if [ "$PY" != python3 ]; then echo "$PY" > .python; else rm -f .python; fi
echo "$REL" > .release
say "ready: $("$PY" --version), $(tc -V 2>/dev/null | head -1), $(nft -v 2>/dev/null)"
