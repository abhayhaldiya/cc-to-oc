# Portable Claude Bridge

A self-contained local proxy that lets OpenCode (or any OpenAI/Anthropic-style
client) use a Claude Code / Claude Max subscription. Move this folder anywhere —
another directory, another Mac/Linux box, a USB drive — and it still works. It
depends only on `python3` and its own local `venv`.

## What it does

- Exposes a local server that speaks both the Anthropic (`/v1/messages`) and
  OpenAI (`/v1/chat/completions`) APIs and forwards to `api.anthropic.com` using
  your Claude OAuth tokens.
- Rotates between multiple Claude accounts (a "pool") to survive rate limits.
- Disguises third-party tool names as MCP tools (`mcp__oc__*`) so the full
  OpenCode toolset reaches Claude without tripping Anthropic's "extra usage"
  billing gate — this is what makes live tool-call progress work.

## One-time setup

```bash
cd claude-bridge-portable
./setup.sh          # creates ./venv and installs deps (needs internet once)
```

## Add credentials

Put one JSON file per Claude account in `creds/`. On macOS, capture your current
login from the Keychain:

```bash
security find-generic-password -s 'Claude Code-credentials' -w > creds/creds1.json
```

Add more as `creds/creds2.json`, `creds/creds3.json`, … (one per account for
rate-limit rotation). Each file looks like:

```json
{
  "claudeAiOauth": {
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1760000000000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max"
  }
}
```

> Creds are git-ignored and never leave this folder. Keep them private.

## Run

```bash
./run.sh                 # foreground, port 8738
./run.sh --background    # detached, logs to ./bridge.log
./run.sh --logs          # follow the log
./run.sh --check         # health probe
./run.sh --stop          # stop this port
./run.sh --stop-all      # stop every instance
./run.sh --restart       # restart this port
./run.sh --port=8750     # a different port
./run.sh --smooth        # enable text-stream smoothing (default off)
```

The banner prints the `--base-url` and `--api-key` to point your client at, e.g.:

```
--base-url http://127.0.0.1:8738/v1
--api-key  <printed secret>
```

## Point OpenCode at it

In your OpenCode config (`~/.config/opencode/opencode.json`):

```json
{
  "provider": {
    "anthropic": {
      "options": {
        "baseURL": "http://127.0.0.1:8738/v1",
        "apiKey": "<the secret printed by run.sh, or set via --secret=>"
      }
    }
  }
}
```

Tip: pin a fixed secret so your client config stays stable:
`./run.sh --secret=my-fixed-secret`.

## Options / env vars

| Env var | Default | Meaning |
|---|---|---|
| `CREDS_DIR` | `./creds` | Where to read `creds*.json` from |
| `ZORO_CC_SMOOTH_STREAM` | `0` | `1` = re-chunk text into word-groups (smoother TUI streaming) |
| `ZORO_CC_SMOOTH_WORDS` | `2` | Words per emitted text piece when smoothing |
| `ZORO_CC_SMOOTH_DELAY_MS` | `12` | Delay between smoothed pieces (ms) |

> Note on smoothing: it only affects text rendering on the client and adds
> negligible latency. On a brand-new session the first message may still render
> in one block (an OpenCode TUI cold-start quirk, not the bridge); later messages
> stream smoothly.

## Files

```
claude-bridge-portable/
├── claude_bridge/   # the proxy (Python package)
├── creds/           # your OAuth token files (git-ignored)
├── setup.sh         # one-time venv + deps
├── run.sh           # start/stop/logs launcher
├── venv/            # created by setup.sh
└── bridge.log       # created when run with --background
```

## Requirements

- `python3` 3.10+ (that's it — `setup.sh` installs the rest into `./venv`).
- macOS or Linux. Keychain write-back is macOS-only and auto-skipped elsewhere.
