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
#   ./run.sh --refresh            # pre-flight refresh of stale creds before start
#   ./run.sh --no-refresh         # skip pre-flight refresh (default; rely on in-bridge self-heal)
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
DO_REFRESH=0
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
    --refresh)         DO_REFRESH=1 ;;
    --no-refresh)      DO_REFRESH=0 ;;
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

if [ "$DO_REFRESH" -eq 1 ]; then
  echo "── Startup refresh: retry+backoff on stale creds in $CREDS_DIR ──"
  CREDS_DIR="$CREDS_DIR" "$VPY" <<'PY'
import glob, json, os, sys, time

try:
    from curl_cffi import requests as cf
except ImportError:
    print("  ⚠️  curl_cffi not installed; skipping refresh (pip install curl_cffi)")
    sys.exit(0)

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
URL = "https://api.anthropic.com/v1/oauth/token"
MAX_ATTEMPTS = 3
BACKOFF_S = 30.0
FRESH_HEADROOM_S = 600
UA = "claude-cli/2.0.170 (external, cli)"
BETA = "oauth-2025-04-20,claude-code-20250219"
CREDS_DIR = os.environ.get("CREDS_DIR", "creds")

def refresh_one(path):
    with open(path) as fh:
        d = json.load(fh)
    oauth = d.get("claudeAiOauth", {})
    rt = oauth.get("refreshToken", "")
    exp_ms = int(oauth.get("expiresAt", 0))

    if exp_ms > (time.time() + FRESH_HEADROOM_S) * 1000:
        ttl = (exp_ms - int(time.time() * 1000)) // 1000
        return ("FRESH", f"TTL {ttl}s (~{ttl/3600:.1f}h)")
    if not rt:
        return ("BROKEN", "no refresh_token")

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = cf.post(
                URL,
                json={"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID},
                headers={
                    "Content-Type": "application/json",
                    "anthropic-beta": BETA,
                    "User-Agent": UA,
                },
                impersonate="chrome124",
                timeout=20,
            )
        except Exception as e:
            last_err = f"network: {e}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_S)
            continue

        if r.status_code == 200:
            data = r.json()
            oauth["accessToken"] = data["access_token"]
            oauth["refreshToken"] = data.get("refresh_token", rt)
            oauth["expiresAt"] = int((time.time() + int(data.get("expires_in", 28800))) * 1000)
            d["claudeAiOauth"] = oauth
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(d, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return ("REFRESHED", f"new TTL {int(data.get('expires_in', 0))}s (~{int(data.get('expires_in', 0))/3600:.1f}h)")

        last_err = f"HTTP {r.status_code}"
        if r.status_code == 429 and attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_S)
            continue
        if 400 <= r.status_code < 500 and r.status_code != 429:
            return ("DEAD_OAUTH", last_err)
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_S)

    return ("BROKEN", last_err or "unknown")

files = sorted(f for f in glob.glob(os.path.join(CREDS_DIR, "creds*.json"))
               if "backup" not in f and "dead" not in f and ".tmp" not in f)
if not files:
    print("  no cred files found")
    sys.exit(0)

results = []
for f in files:
    label = os.path.basename(f)
    print(f"  {label}: refreshing ...", end="", flush=True)
    state, msg = refresh_one(f)
    icon = {"FRESH": "🟢", "REFRESHED": "✅", "DEAD_OAUTH": "🔴", "BROKEN": "⚠️"}.get(state, "❓")
    print(f"\r  {label}: {icon} {state:10s}  {msg}                                ")
    results.append(state)

fresh = sum(1 for s in results if s in ("FRESH", "REFRESHED"))
dead = sum(1 for s in results if s == "DEAD_OAUTH")
broken = sum(1 for s in results if s == "BROKEN")
print()
print(f"  Summary: {fresh} live, {broken} temp-broken (rate-limited, will heal), {dead} dead (need recapture)")
if dead > 0:
    print(f"  ⚠️  {dead} cred(s) need re-capture via: claude login && security find-generic-password -s 'Claude Code-credentials' -w > {os.path.join(CREDS_DIR, 'credsN.json')}")
if fresh == 0 and broken > 0:
    print(f"  ℹ️  Bridge will start anyway. In-bridge self-heal retries every 30s on requests.")
elif fresh == 0 and dead == len(files):
    print(f"  ❌ ALL creds dead. Bridge will start but ALL requests will fail 401 until you re-capture.")
PY
  echo ""
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
