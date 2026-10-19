"""
Quick quota check for all creds in the portable bridge's creds dir.

Shows both 5-hour rate limit AND 7-day (weekly) quota.

Usage:
    python3 check_creds.py                 # from inside claude-bridge-portable/
    CREDS_DIR=/path/to/creds python3 check_creds.py

Creds are resolved from (first match wins):
    1) $CREDS_DIR                          (if set)
    2) <portable root>/creds               (portable default, matches run.sh)
    3) .signer                             (legacy repo layout fallback)

Stdlib-only (no third-party deps). All times shown in IST (Asia/Kolkata, UTC+5:30).
"""
import json
import glob
import hashlib
import datetime
import os
import urllib.request
import urllib.error

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")

HERE = os.path.dirname(os.path.abspath(__file__))
PORTABLE_ROOT = os.path.dirname(HERE)


def resolve_creds_dir():
    """Resolve the creds directory for the portable layout.

    This file lives in the claude_bridge/ package, so the portable root (which
    holds creds/, run.sh, setup.sh) is one level up. Order:
    $CREDS_DIR -> <portable root>/creds -> .signer (legacy fallback).
    Returns the first directory that exists and contains creds*.json, else
    the portable default so the error message points at the right place.
    """
    candidates = []
    env_dir = os.environ.get("CREDS_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates.append(os.path.join(PORTABLE_ROOT, "creds"))
    candidates.append(".signer")

    for d in candidates:
        if glob.glob(os.path.join(d, "creds*.json")):
            return d
    # nothing found: default to portable creds dir for a sensible error
    return os.path.join(PORTABLE_ROOT, "creds")


def probe_profile(tok):
    """Query /api/oauth/profile for account name and org info."""
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/profile",
        method="GET",
        headers={
            "Authorization": f"Bearer {tok}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            "User-Agent": "claude-cli/2.0",
        }
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception:
        return None


def probe_usage(cred_path):
    """Query /api/oauth/usage endpoint for both 5h and 7d data."""
    try:
        with open(cred_path) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return {"error": f"unreadable cred file: {e}"}
    tok = d.get("claudeAiOauth", {}).get("accessToken", "")
    if not tok:
        return {"error": "no accessToken"}

    exp_ms = d.get("claudeAiOauth", {}).get("expiresAt", 0)
    if exp_ms:
        exp_ist = datetime.datetime.fromtimestamp(
            exp_ms / 1000, tz=datetime.timezone.utc
        ).astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    else:
        exp_ist = "?"
    fp = hashlib.sha256(tok[:40].encode()).hexdigest()[:12]

    profile = probe_profile(tok)
    acct_name = None
    org_name = None
    if profile:
        acct = profile.get("account") or {}
        org = profile.get("organization") or {}
        acct_name = acct.get("full_name") or acct.get("display_name")
        org_name = org.get("name")

    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        method="GET",
        headers={
            "Authorization": f"Bearer {tok}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            "User-Agent": "claude-cli/2.0",
        }
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        code = resp.status
        body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = json.loads(e.read())
        except:
            body = None
    except Exception as e:
        code = "ERR"
        body = {"error": str(e)}

    return {"fp": fp, "expires": exp_ist, "code": code, "body": body,
            "acct_name": acct_name, "org_name": org_name}


def format_reset(reset_iso, now_ist):
    if not reset_iso:
        return "?"
    try:
        dt = datetime.datetime.fromisoformat(reset_iso).astimezone(IST)
        delta = dt - now_ist
        total_min = int(delta.total_seconds() / 60)
        if total_min < 60:
            return f"{dt.strftime('%H:%M IST')} (in {total_min}m)"
        elif total_min < 24 * 60:
            h, m = divmod(total_min, 60)
            return f"{dt.strftime('%H:%M IST')} (in {h}h{m}m)"
        else:
            days = total_min // (24 * 60)
            return f"{dt.strftime('%Y-%m-%d %H:%M IST')} (in {days}d)"
    except Exception:
        return reset_iso


def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== IST: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} ===\n")

    creds_dir = resolve_creds_dir()
    live_5h = 0
    live_7d = 0
    total_util_5h = 0.0
    total_util_7d = 0.0
    creds = sorted(glob.glob(os.path.join(creds_dir, "creds[0-9].json")))
    if not creds:
        creds = sorted(glob.glob(os.path.join(creds_dir, "creds*.json")))
    # skip backup / dead / tmp files, matching run.sh pool-building rules
    creds = [c for c in creds
             if not any(x in os.path.basename(c) for x in ("backup", "dead", ".tmp"))]

    print(f"=== creds dir: {creds_dir} ===")
    if not creds:
        print(f"  ❌ no creds*.json found in {creds_dir}")
        print("     Capture one: security find-generic-password -s 'Claude Code-credentials' -w > "
              f"{os.path.join(creds_dir, 'creds1.json')}")
        return
    print()

    for cred in creds:
        info = probe_usage(cred)
        if info.get("error"):
            print(f"  {cred}  ❌ {info['error']}")
            continue
        if info.get("code") != 200 or not isinstance(info.get("body"), dict):
            code = info.get("code", "?")
            print(f"  {cred}  fp={info.get('fp', '?')}  ❌ HTTP {code}")
            continue

        body = info["body"]
        fh = body.get("five_hour") or {}
        sd = body.get("seven_day")

        fh_util = fh.get("utilization")
        fh_reset = fh.get("resets_at")

        name_tag = ""
        if info.get("acct_name"):
            name_tag = f"  name={info['acct_name']}"
        if info.get("org_name"):
            name_tag += f"  org={info['org_name']}"

        print(f"  {cred}  fp={info['fp']}{name_tag}")
        print(f"    expires={info['expires']}")
        print(f"    5-hour:  {fh_util}%  reset={format_reset(fh_reset, now_ist)}")

        if sd is None:
            print(f"    7-day:   null (untracked / no weekly cap active)")
        else:
            sd_util = sd.get("utilization")
            sd_reset = sd.get("resets_at")
            print(f"    7-day:   {sd_util}%  reset={format_reset(sd_reset, now_ist)}")
            live_7d += 1
            total_util_7d += sd_util or 0

        if fh_util is not None:
            live_5h += 1
            total_util_5h += fh_util

    print()
    if live_5h:
        print(f"  --- 5-hour: {live_5h}/{len(creds)} accounts, avg util {total_util_5h/live_5h:.1f}% ---")
    if live_7d:
        print(f"  --- 7-day:  {live_7d}/{len(creds)} accounts tracked, avg util {total_util_7d/live_7d:.1f}% ---")


if __name__ == "__main__":
    main()
