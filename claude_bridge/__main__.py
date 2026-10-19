from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m claude_bridge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8399)
    p.add_argument("--log-level", default="info")
    p.add_argument(
        "--check",
        action="store_true",
        help="Verify credentials, then exit.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from claude_bridge.credentials import CredentialsError, resolve_provider
    from claude_bridge.server import build_app

    provider = resolve_provider()
    try:
        provider.preflight()
    except CredentialsError as e:
        print(f"[bridge] credentials error: {e}", file=sys.stderr)
        return 2

    tp = provider.token_prefix() or "?"
    print(f"[bridge] credentials OK (token prefix: {tp}...)")
    if args.check:
        return 0

    import uvicorn

    print(f"[bridge] listening on http://{args.host}:{args.port}")
    print("[bridge] point clients at:")
    print(f"           export ZORO_LLM_BASE_URL=http://{args.host}:{args.port}/v1")
    print("           export ZORO_LLM_API_KEY=${ZORO_CC_BRIDGE_SECRET:-zoro-cc-stub}")
    uvicorn.run(
        build_app(provider),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
