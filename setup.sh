#!/usr/bin/env bash
# setup.sh — one-time setup for the portable claude bridge.
# Creates a local venv inside this folder and installs the runtime deps.
# Safe to re-run; it reuses the venv if present.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/venv"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ and retry (or set PYTHON=/path/to/python3)." >&2
  exit 1
fi

VPY="$VENV/bin/python3"

if [ -d "$VENV" ] && ! "$VPY" -c "import sys" >/dev/null 2>&1; then
  echo "── Existing venv is broken (moved/copied from another machine); rebuilding ──"
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  echo "── Creating venv at $VENV ──"
  "$PY" -m venv "$VENV"
fi

echo "── Upgrading pip ──"
"$VPY" -m pip install --quiet --upgrade pip

echo "── Installing runtime deps ──"
"$VPY" -m pip install --quiet fastapi "uvicorn[standard]" httpx curl_cffi

echo ""
echo "✅ Setup complete."
echo "   Next:"
echo "     1) Put your Claude OAuth creds file(s) in:  $HERE/creds/creds1.json"
echo "        (capture with: security find-generic-password -s 'Claude Code-credentials' -w > creds/creds1.json)"
echo "     2) Start the bridge:  ./run.sh"
