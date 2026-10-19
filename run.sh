#!/usr/bin/env bash
# run.sh — launch the portable claude bridge from THIS folder, wherever it lives.
#
# Usage:
#   ./run.sh                      # start in foreground (default port 8738)
#   ./run.sh --background         # start detached, logs to ./bridge.log
#   ./run.sh --logs               # tail -f ./bridge.log
#   ./run.sh --stop               # stop the bridge on --port
#   ./run.sh --stop-all           # stop every bridge started from any port
#   ./run.sh --check              # probe /healthz and exit
#   ./run.sh --restart            # kill the bridge on --port, then start
#   ./run.sh --port=<n>           # bind port (default 8738)
#   ./run.sh --secret=<str>       # bridge secret (default: auto-generated)
#   ./run.sh --smooth             # enable text streaming smoothing (default OFF)
#
# Creds: put creds*.json in ./creds/  (or set CREDS_DIR=/path/to/dir).
# The script is fully self-contained; move this folder anywhere and it still works.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/venv"
CREDS_DIR="${CREDS_DIR:-$HERE/creds}"
LOG_FILE="$HERE/bridge.log"
PID_FILE="$HERE/bridge.pid"

ACTION="start"
DO_BACKGROUND=0
DO_RESTART=0
DO_SMOOTH=0
SECRET=""
PORT=8738

for arg in "$@"; do
  case "$arg" in
    --background|--bg) DO_BACKGROUND=1 ;;
    --logs)            ACTION="logs" ;;
    --stop)            ACTION="stop" ;;
    --stop-all)        ACTION="stop-all" ;;
    --check)           ACTION="check" ;;
    --restart)         DO_RESTART=1 ;;
    --smooth)          DO_SMOOTH=1 ;;
    --port=*)          PORT="${arg#--port=}" ;;
    --secret=*)        SECRET="${arg#--secret=}" ;;
    -h|--help)
      awk '/^#!/ {next} /^#($| )/ {sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $arg (use -h for help)" >&2; exit 2 ;;
  esac
done

VPY="$VENV/bin/python3"
if [ ! -d "$VENV" ] || [ ! -x "$VPY" ]; then
  echo "❌ venv not found or broken (moved from another machine?). Run ./setup.sh first." >&2
  exit 1
fi
if ! "$VPY" -c "import fastapi, httpx, uvicorn" >/dev/null 2>&1; then
  echo "❌ venv is stale/incompatible (likely copied from another machine)." >&2
  echo "   Rebuild it:  rm -rf venv && ./setup.sh" >&2
  exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ "$ACTION" = "check" ]; then
  curl -s -m 3 "http://127.0.0.1:$PORT/healthz" | python3 -m json.tool 2>/dev/null && exit 0
  echo "❌ bridge not responding on 127.0.0.1:$PORT"; exit 1
fi

if [ "$ACTION" = "logs" ]; then
  [ -f "$LOG_FILE" ] || { echo "❌ no log at $LOG_FILE" >&2; exit 1; }
  exec tail -n 50 -f "$LOG_FILE"
fi

if [ "$ACTION" = "stop-all" ]; then
  PIDS=$(pgrep -f "claude_bridge" 2>/dev/null || true)
  [ -z "$PIDS" ] && { echo "no bridge running"; rm -f "$PID_FILE"; exit 0; }
  echo "── stopping ALL bridges: $(echo "$PIDS" | tr '\n' ' ')──"
  for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
  sleep 2
  for p in $PIDS; do kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null || true; done
  rm -f "$PID_FILE"; echo "✅ stopped"; exit 0
fi

if [ "$ACTION" = "stop" ]; then
  P=$(pgrep -f "claude_bridge --host 127.0.0.1 --port $PORT" 2>/dev/null | head -1 || true)
  [ -z "$P" ] && { echo "no bridge on port $PORT"; rm -f "$PID_FILE"; exit 0; }
  echo "── stopping PID $P (port $PORT) ──"
  kill -TERM "$P" 2>/dev/null || true; sleep 2
  kill -0 "$P" 2>/dev/null && kill -9 "$P" 2>/dev/null || true
  rm -f "$PID_FILE"; echo "✅ stopped"; exit 0
fi

EXISTING=$(pgrep -f "claude_bridge --host 127.0.0.1 --port $PORT" 2>/dev/null | head -1 || true)
if [ -n "$EXISTING" ]; then
  if [ "$DO_RESTART" -eq 1 ]; then
    echo "── restarting: killing PID $EXISTING on port $PORT ──"
    kill -TERM "$EXISTING" 2>/dev/null || true; sleep 2
    kill -0 "$EXISTING" 2>/dev/null && kill -9 "$EXISTING" 2>/dev/null || true
  else
    echo "❌ bridge already running on port $PORT (PID $EXISTING). Use --restart or --stop." >&2
    exit 1
  fi
fi

# Build the creds pool from CREDS_DIR
CREDS=()
for f in "$CREDS_DIR"/creds*.json; do
  [ -f "$f" ] || continue
  case "$f" in *backup*|*dead*|*.tmp) continue ;; esac
  CREDS+=("$f")
done
if [ "${#CREDS[@]}" -eq 0 ]; then
  echo "❌ no creds*.json in $CREDS_DIR" >&2
  echo "   Capture one: security find-generic-password -s 'Claude Code-credentials' -w > $CREDS_DIR/creds1.json" >&2
  exit 1
fi
POOL=$(printf "%s:" "${CREDS[@]}"); POOL="${POOL%:}"

[ -z "$SECRET" ] && SECRET=$(openssl rand -hex 16 2>/dev/null || echo "portable-bridge-secret")

echo "══════════════════════════════════════════════"
echo "  portable claude bridge"
echo "  port:   127.0.0.1:$PORT"
echo "  creds:  ${#CREDS[@]} file(s) from $CREDS_DIR"
echo "  secret: $SECRET"
echo "  smooth: $([ "$DO_SMOOTH" -eq 1 ] && echo on || echo off)"
echo "  client: --base-url http://127.0.0.1:$PORT/v1  --api-key $SECRET"
echo "══════════════════════════════════════════════"

export ZORO_CC_BRIDGE_SECRET="$SECRET"
export ZORO_CC_ACCOUNT_POOL="$POOL"
export ZORO_CC_WRITE_BACK_KEYCHAIN=0
[ "$DO_SMOOTH" -eq 1 ] && export ZORO_CC_SMOOTH_STREAM=1

export PYTHONPATH="$HERE"

if [ "$DO_BACKGROUND" -eq 1 ]; then
  nohup "$VPY" -m claude_bridge --host 127.0.0.1 --port "$PORT" >> "$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"
  echo "── started in background (PID $!) — logs: ./bridge.log ──"
  sleep 4
  curl -s -m 5 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 \
    && echo "✅ healthz OK on 127.0.0.1:$PORT" \
    || echo "⚠️  not answering yet — check ./run.sh --logs"
  exit 0
fi

exec "$VPY" -m claude_bridge --host 127.0.0.1 --port "$PORT"
