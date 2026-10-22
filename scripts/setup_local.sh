#!/usr/bin/env bash
#
# One-shot local setup for the HRMS: virtualenv + dependencies + PWA icons.
#
#   bash scripts/setup_local.sh
#
# On macOS this also applies a real fix: if the base interpreter is signed with
# the hardened runtime (Apple-silicon python.org/Homebrew builds and the
# DSH-bundled runtime), the OS refuses to load the ad-hoc-signed C extensions
# that PyPI wheels ship:
#
#   ImportError: ... mapping process and mapped file (non-platform) have
#   different Team IDs
#
# The cure is to copy the interpreter into the venv and re-sign that copy with
# the `com.apple.security.cs.disable-library-validation` entitlement. Nothing
# outside `.venv/` is touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${HRMS_BOOTSTRAP_PYTHON:-${FACEAPI_BOOTSTRAP_PYTHON:-$(command -v python3 || true)}}"
if [ -z "$PYTHON_BIN" ]; then
  echo "error: no python3 found. Install Python 3.10+ first." >&2
  exit 1
fi
echo "==> base interpreter: $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

if [ ! -d .venv ]; then
  echo "==> creating .venv"
  "$PYTHON_BIN" -m venv .venv
fi

echo "==> installing dependencies"
./.venv/bin/python3 -m pip install --quiet --upgrade pip
./.venv/bin/python3 -m pip install --quiet -r requirements-dev.txt

# ---------------------------------------------------------------- macOS fix
if [ "$(uname -s)" = "Darwin" ]; then
  if ./.venv/bin/python3 -c "import fastapi, pydantic_core" >/dev/null 2>&1; then
    echo "==> macOS: C extensions load fine, no re-signing needed"
  else
    echo "==> macOS: re-signing the venv interpreter (library validation workaround)"
    REAL_PY="$(./.venv/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.executable))')"
    PLIST="$(mktemp -t hrms-entitlements)"
    cat >"$PLIST" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
</dict>
</plist>
PLIST_EOF

    cp -f "$REAL_PY" .venv/bin/python3.signed
    codesign --force --sign - --entitlements "$PLIST" .venv/bin/python3.signed >/dev/null 2>&1
    rm -f "$PLIST"

    rm -f .venv/bin/python .venv/bin/python3 .venv/bin/python3.12
    mv .venv/bin/python3.signed .venv/bin/python3
    ln -s python3 .venv/bin/python
    ln -s python3 .venv/bin/python3.12

    if ./.venv/bin/python3 -c "import fastapi, pydantic_core" >/dev/null 2>&1; then
      echo "    fixed: fastapi + pydantic now import"
    else
      echo "    warning: could not fix automatically — see README → Troubleshooting" >&2
    fi
  fi
fi

# ------------------------------------------------------------------- extras
echo "==> generating PWA icons"
./.venv/bin/python3 cli.py make-icons

echo "==> creating the database and the first logins"
./.venv/bin/python3 cli.py init

echo
echo "==> environment check"
./.venv/bin/python3 cli.py doctor || true

echo
echo "Done. Next steps:"
echo "  1. start the Face API:   cd ../faceapi && FACEAPI_AUTH_ENABLED=false ./.venv/bin/python3 cli.py serve"
echo "  2. start the HRMS:       ./.venv/bin/python3 cli.py serve"
echo "  3. open                  http://127.0.0.1:8100/dashboard   (HR)"
echo "                           http://127.0.0.1:8100/kiosk      (gate scanner)"
echo "  optional demo data:      ./.venv/bin/python3 cli.py seed-demo --employees 12"
