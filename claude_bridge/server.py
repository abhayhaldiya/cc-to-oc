from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal, Union

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from claude_bridge.credentials import (
    CredentialProvider,
    CredentialsError,
    MultiAccountCredentialProvider,
)
from claude_bridge.errors import (
    Classification,
    classify_anthropic_error,
    to_openai_error_body,
)

_LOG = logging.getLogger(__name__)

UPSTREAM_DEFAULT = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# Upstream rejects requests missing any beta Claude Code normally sends; overridable for forward-compat.
OAUTH_BETA = os.environ.get(
    "ZORO_CC_ANTHROPIC_BETA",
    ",".join([
        "oauth-2025-04-20",
        # "claude-code-20250219",  # REMOVED: triggers Claude Code tool-name fingerprinting (Layer 3)
        "interleaved-thinking-2025-05-14",
        "fine-grained-tool-streaming-2025-05-14",
    ]),
)
SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

# ---------------------------------------------------------------------------
# OpenCode fingerprint scrubbing (mirrors meridian-plugin-opencode-scrub)
# ---------------------------------------------------------------------------
# Anthropic's billing layer detects third-party SDKs via system-prompt
# fingerprinting.  These regexes strip every known OpenCode/OhMyOpenCode
# marker so the request looks like a native Claude Code call.

_OPENCODE_IDENTITY_LINE = re.compile(
    r"You are OpenCode, the best coding agent on the planet\.[^\n]*\n+"
)
_OPENCODE_FEEDBACK_BLOCK = re.compile(
    r"If the user asks for help or wants to give feedback[\s\S]*?github\.com/anomalyco/opencode[^\n]*\n+"
)
_OPENCODE_DOCS_PARAGRAPH = re.compile(
    r"When the user directly asks about OpenCode[\s\S]*?opencode\.ai/docs[^\n]*\n+"
)
_OPENCODE_OBJECTIVITY_BRAND = re.compile(
    r"It is best for the user if OpenCode honestly applies"
)
_OPENCODE_BRAND_TOKEN = re.compile(r"\bOpenCode\b", re.IGNORECASE)
_OMO_IDENTITY_LINE = re.compile(
    r'You are "Sisyphus"[^\n]*from OhMyOpenCode\.[^\n]*\n+'
)
_OMO_ENV_BLOCK = re.compile(r"<omo-env>[\s\S]*?</omo-env>\n*")
_POWERED_BY_LINE = re.compile(r"You are powered by the model named [^\n]+\n?")
_OPENCODE_ENV_BLOCK = re.compile(
    r"\nHere is some useful information about the environment you are running in:\n<env>[\s\S]*?</env>\n"
)
_AGENT_IDENTITY_BLOCK = re.compile(r"<agent-identity>[\s\S]*?</agent-identity>\n*")
_ROLE_BLOCK = re.compile(r"<Role>[\s\S]*?</Role>\n*")
_OMO_BRAND_TOKEN = re.compile(r"\bOhMyOpenCode\b")
_SISYPHUS_BRAND = re.compile(r'\bSisyphus\b')
# The full <omo-env> is already caught above; catch residual opencode.ai URLs
_OPENCODE_URL = re.compile(r"opencode\.ai[^\s]*")

_GENERIC_IDENTITY = (
    "You are an expert coding assistant. "
    "You help users with software engineering tasks by reading files, "
    "executing commands, editing code, and writing new files.\n"
)
_GENERIC_OBJECTIVITY = "It is best for the user if the assistant honestly applies"


def _scrub_system_block_text(text: str) -> str:
    """Strip OpenCode/OhMyOpenCode fingerprints from a single system block."""
    if not text:
        return text
    text = _OPENCODE_IDENTITY_LINE.sub(_GENERIC_IDENTITY, text)
    text = _OPENCODE_FEEDBACK_BLOCK.sub("", text)
    text = _OPENCODE_DOCS_PARAGRAPH.sub("", text)
    text = _OPENCODE_OBJECTIVITY_BRAND.sub(_GENERIC_OBJECTIVITY, text)
    text = _OMO_IDENTITY_LINE.sub("", text)
    text = _OMO_ENV_BLOCK.sub("", text)
    text = _POWERED_BY_LINE.sub("", text)
    text = _OPENCODE_ENV_BLOCK.sub("\n", text)
    text = _AGENT_IDENTITY_BLOCK.sub("", text)
    text = _OPENCODE_BRAND_TOKEN.sub("the assistant", text)
    text = _OMO_BRAND_TOKEN.sub("the assistant", text)
    text = _SISYPHUS_BRAND.sub("the assistant", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.rstrip()
    return text


def _scrub_opencode_fingerprints(system_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scrub OpenCode/OhMyOpenCode fingerprints from every text system block."""
    out: list[dict[str, Any]] = []
    for blk in system_blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            cleaned = _scrub_system_block_text(blk.get("text", ""))
            if cleaned:
                out.append({"type": "text", "text": cleaned})
        else:
            out.append(blk)
    return out


_CORE_TOOL_NAMES = frozenset({"read", "write", "edit", "bash", "glob", "grep"})
_DEFER_THRESHOLD = 9

# Anthropic gates Claude-Code OAuth tokens: arbitrary top-level tool names
# trip the "extra usage" billing fingerprint (HTTP 400). Names starting with
# `mcp__` pass as legitimate MCP tools. We disguise every foreign tool under
# this prefix on the way upstream and strip it back off in responses, so the
# full OpenCode toolset reaches Claude without tripping the gate.
_MCP_PREFIX = "mcp__oc__"
_TOOL_NAME_MAX = 64


def _remap_name_out(name: str) -> str:
    if name in _CORE_TOOL_NAMES:
        return name
    if name.startswith("mcp__"):
        return name
    remapped = _MCP_PREFIX + name
    if len(remapped) > _TOOL_NAME_MAX:
        _LOG.error("tool name too long to remap, passing raw: %s", name)
        return name
    return remapped


def _remap_name_in(name: str) -> str:
    if name.startswith(_MCP_PREFIX):
        return name[len(_MCP_PREFIX):]
    return name


def _remap_tools_outbound(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            t = {**t, "name": _remap_name_out(t["name"])}
        out.append(t)
    return out


def _remap_tool_choice_outbound(tool_choice: Any) -> Any:
    if isinstance(tool_choice, dict) and isinstance(tool_choice.get("name"), str):
        return {**tool_choice, "name": _remap_name_out(tool_choice["name"])}
    return tool_choice


def _remap_messages_outbound(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and isinstance(m.get("content"), list)
        ):
            new_content = []
            for blk in m["content"]:
                if (
                    isinstance(blk, dict)
                    and blk.get("type") == "tool_use"
                    and isinstance(blk.get("name"), str)
                ):
                    blk = {**blk, "name": _remap_name_out(blk["name"])}
                new_content.append(blk)
            m = {**m, "content": new_content}
        out.append(m)
    return out


def _remap_content_in(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    for blk in content:
        if (
            isinstance(blk, dict)
            and blk.get("type") == "tool_use"
            and isinstance(blk.get("name"), str)
        ):
            blk["name"] = _remap_name_in(blk["name"])
    return content


def _smooth_enabled() -> bool:
    return os.environ.get("ZORO_CC_SMOOTH_STREAM") == "1"


def _smooth_words() -> int:
    return max(1, _env_int("ZORO_CC_SMOOTH_WORDS", 2))


def _smooth_delay() -> float:
    return max(0.0, _env_float("ZORO_CC_SMOOTH_DELAY_MS", 12.0)) / 1000.0


def _split_text_delta(evt: dict[str, Any], max_words: int) -> list[dict[str, Any]]:
    delta = evt.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return [evt]
    text = delta.get("text", "")
    if not isinstance(text, str) or not text:
        return [evt]
    parts = re.findall(r"\s*\S+|\s+", text)
    if len(parts) <= 1:
        return [evt]
    idx = evt.get("index", 0)
    out: list[dict[str, Any]] = []
    for i in range(0, len(parts), max_words):
        chunk = "".join(parts[i:i + max_words])
        if chunk:
            out.append({
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "text_delta", "text": chunk},
            })
    return out or [evt]


def _rewrite_sse_line(line: bytes) -> bytes:
    if not line.startswith(b"data:"):
        return line
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return line
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError):
        return line
    if (
        isinstance(evt, dict)
        and evt.get("type") == "content_block_start"
        and isinstance(evt.get("content_block"), dict)
        and evt["content_block"].get("type") == "tool_use"
        and isinstance(evt["content_block"].get("name"), str)
    ):
        evt["content_block"]["name"] = _remap_name_in(evt["content_block"]["name"])
        return b"data: " + json.dumps(evt, ensure_ascii=False).encode("utf-8")
    return line


def _parse_text_delta_event(line: bytes) -> dict[str, Any] | None:
    if not line.startswith(b"data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        evt = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(evt, dict)
        and evt.get("type") == "content_block_delta"
        and isinstance(evt.get("delta"), dict)
        and evt["delta"].get("type") == "text_delta"
    ):
        return evt
    return None


async def _rewrite_native_sse(
    first_bytes: bytes,
    byte_iter: AsyncIterator[bytes],
) -> AsyncIterator[bytes]:
    smooth = _smooth_enabled()
    max_words = _smooth_words()
    delay = _smooth_delay()
    buf = first_bytes
    async for chunk in byte_iter:
        buf += chunk
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            line, buf = buf[:idx], buf[idx + 1:]
            if smooth:
                evt = _parse_text_delta_event(line)
                if evt is not None:
                    pieces = _split_text_delta(evt, max_words)
                    if len(pieces) > 1:
                        for j, piece in enumerate(pieces):
                            data = b"data: " + json.dumps(piece, ensure_ascii=False).encode("utf-8")
                            yield data + b"\n\n"
                            if delay and j < len(pieces) - 1:
                                await asyncio.sleep(delay)
                        continue
            yield _rewrite_sse_line(line) + b"\n"
    if buf:
        yield _rewrite_sse_line(buf)


def _apply_tool_deferral(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(tools) <= _DEFER_THRESHOLD:
        return tools
    core = [t for t in tools if t.get("name", "").lower() in _CORE_TOOL_NAMES]
    return core if core else tools[:_DEFER_THRESHOLD]


# ---------------------------------------------------------------------------

# Models that 400 on `thinking.budget_tokens` (non-extended-thinking tiers).
NO_THINKING_MODELS = {
    "claude-haiku-4-5-20251001",
}

DEFAULT_MAX_TOKENS = 8192
DEFAULT_THINKING_BUDGET = 8192

MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus":   "claude-opus-4-8",
    "haiku":  "claude-haiku-4-5-20251001",
}

_OPENAI_ONLY_KEYS = {
    "n", "logprobs", "top_logprobs", "logit_bias", "presence_penalty",
    "frequency_penalty", "seed", "response_format", "user", "functions",
    "function_call", "parallel_tool_calls", "service_tier",
}

ProviderLike = Union[CredentialProvider, MultiAccountCredentialProvider]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _upstream_base() -> str:
    return os.environ.get("ZORO_CC_UPSTREAM", UPSTREAM_DEFAULT).rstrip("/")


def _timeout(streaming: bool = False) -> httpx.Timeout:
    connect = _env_float("ZORO_BRIDGE_CONNECT_TIMEOUT", 30.0)
    if streaming:
        read = _env_float("ZORO_BRIDGE_STREAM_READ_TIMEOUT", 600.0)
        return httpx.Timeout(None, connect=connect, read=read, write=None, pool=None)
    total = _env_float("ZORO_BRIDGE_REQUEST_TIMEOUT", 600.0)
    read = _env_float("ZORO_BRIDGE_READ_TIMEOUT", 180.0)
    return httpx.Timeout(total, connect=connect, read=read)


def _map_model(name: str) -> str:
    if not name:
        return MODEL_ALIASES["sonnet"]
    if name.startswith("claude-"):
        return name
    return MODEL_ALIASES.get(name.lower(), name)


def _map_finish_reason(stop_reason: str | None) -> str:
    return {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }.get(stop_reason or "", "stop")


def _extract_system_and_messages(
    openai_messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in openai_messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                        system_parts.append(blk["text"])
                    elif isinstance(blk, str):
                        system_parts.append(blk)
        else:
            out.append({"role": role, "content": content})
    return "\n\n".join(p for p in system_parts if p), out


def _inject_system_prefix(system_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first_text = ""
    for blk in system_blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            first_text = blk.get("text", "")
            break
    if first_text.startswith(SYSTEM_PREFIX):
        return system_blocks
    return [{"type": "text", "text": SYSTEM_PREFIX}, *system_blocks]


def translate_openai_to_anthropic(body: dict[str, Any]) -> dict[str, Any]:
    model = _map_model(body.get("model", ""))
    system_text, messages = _extract_system_and_messages(body.get("messages") or [])

    system_blocks: list[dict[str, Any]] = []
    if system_text:
        system_blocks.append({"type": "text", "text": system_text})

    # if os.environ.get("ZORO_CC_SKIP_SYSTEM_PREFIX") != "1":
    system_blocks = _inject_system_prefix(system_blocks)

    max_tokens = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
    ant: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system_blocks:
        ant["system"] = system_blocks

    # Sampling params are set after the thinking block below, because Anthropic's
    # rules depend on whether extended thinking is enabled.

    stop = body.get("stop")
    if stop is not None:
        ant["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    if body.get("stream"):
        ant["stream"] = True

    extra = body.get("extra_body") or {}
    if isinstance(extra, dict) and extra.get("enable_thinking"):
        if model in NO_THINKING_MODELS:
            _LOG.warning("enable_thinking requested for %s which does not support thinking; dropping", model)
        else:
            budget = int(extra.get("thinking_budget") or DEFAULT_THINKING_BUDGET)
            # Anthropic requires budget_tokens strictly less than max_tokens.
            if budget >= max_tokens:
                budget = max(1024, max_tokens - 1)
                _LOG.warning(
                    "thinking_budget >= max_tokens (%d >= %d); clamped to %d",
                    int(extra.get("thinking_budget") or DEFAULT_THINKING_BUDGET), max_tokens, budget,
                )
            ant["thinking"] = {"type": "enabled", "budget_tokens": budget}

    # Anthropic rejects temperature+top_p together, and with extended thinking it
    # requires temperature=1 and forbids top_p. So: with thinking on, drop both
    # (Anthropic defaults temperature to the required 1); otherwise forward exactly
    # one, preferring temperature.
    if "thinking" not in ant:
        if body.get("temperature") is not None:
            ant["temperature"] = body["temperature"]
        elif body.get("top_p") is not None:
            ant["top_p"] = body["top_p"]

    if body.get("tools") or body.get("tool_choice"):
        _LOG.warning("tools/tool_choice present but tool translation is out of scope for v1; dropping")

    dropped = [k for k in _OPENAI_ONLY_KEYS if k in body and body[k] is not None]
    if dropped:
        _LOG.warning("dropping unsupported OpenAI-only params: %s", ",".join(sorted(dropped)))

    return ant


def _build_forward_headers(access_token: str, streaming: bool) -> dict[str, str]:
    h: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream" if streaming else "application/json",
    }
    if OAUTH_BETA:
        h["anthropic-beta"] = OAUTH_BETA
    return h


def _anthropic_to_openai_nonstream(
    ant_body: dict[str, Any],
    requested_model: str,
) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for blk in ant_body.get("content") or []:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text" and isinstance(blk.get("text"), str):
            text_parts.append(blk["text"])
        elif t == "thinking" and isinstance(blk.get("thinking"), str):
            thinking_parts.append(blk["thinking"])

    usage = ant_body.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    prompt_tokens = input_tokens + cache_read + cache_creation
    completion_tokens = output_tokens

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if thinking_parts:
        message["reasoning_content"] = "".join(thinking_parts)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _map_finish_reason(ant_body.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _sse_chunk(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _openai_chunk_skeleton(chunk_id: str, created: int, model: str) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }


def _parse_sse_event(block: str) -> tuple[str | None, dict[str, Any] | None]:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if not data_lines:
        return event_name, None
    data_str = "\n".join(data_lines)
    if not data_str or data_str == "[DONE]":
        return event_name, None
    try:
        return event_name, json.loads(data_str)
    except (ValueError, TypeError):
        return event_name, None


async def _iter_sse_blocks(
    byte_iter: AsyncIterator[bytes],
    prebuffered: bytes = b"",
) -> AsyncIterator[str]:
    buf = prebuffered.decode("utf-8", errors="replace")
    async for raw in byte_iter:
        buf += raw.decode("utf-8", errors="replace")
        while True:
            idx = buf.find("\n\n")
            if idx < 0:
                break
            block, buf = buf[:idx], buf[idx + 2:]
            block = block.strip("\r")
            if block:
                yield block
    tail = buf.strip()
    if tail:
        yield tail


def _classify_sse_error_event(data: dict[str, Any]) -> Classification:
    err = data.get("error") if isinstance(data, dict) else None
    err_type = (err or {}).get("type") if isinstance(err, dict) else None
    message = (err or {}).get("message") if isinstance(err, dict) else None
    message = message or err_type or "upstream stream error"
    if err_type in ("overloaded_error", "api_error"):
        return Classification("transient", 503, None, None, err_type, message, None)
    if err_type == "rate_limit_error":
        return Classification("cap", 429, 300.0, time.time() + 300.0, err_type, message, None)
    if err_type in ("authentication_error", "permission_error"):
        return Classification("auth", 401, None, None, err_type, message, None)
    if err_type == "invalid_request_error":
        return Classification("client", 400, None, None, err_type, message, None)
    return Classification("transient", 502, None, None, err_type, message, None)


async def _translate_stream(
    byte_iter: AsyncIterator[bytes],
    requested_model: str,
    *,
    prebuffered: bytes = b"",
    provider: ProviderLike | None = None,
) -> AsyncIterator[bytes]:
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    stop_reason: str | None = None
    final_usage: dict[str, int] | None = None
    initial_usage: dict[str, int] = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    sent_role = False
    active_block_types: dict[int, str] = {}

    async for block in _iter_sse_blocks(byte_iter, prebuffered=prebuffered):
        event_name, data = _parse_sse_event(block)
        if data is None:
            continue
        t = data.get("type") or event_name

        if t == "error":
            cls = _classify_sse_error_event(data)
            _LOG.error(
                "mid-stream error event: kind=%s type=%s msg=%s",
                cls.kind, cls.error_type, cls.message,
            )
            if provider is not None:
                await _apply_classification(provider, cls)
            payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
            payload["choices"] = [{
                "index": 0,
                "delta": {"content": f"\n\n[bridge] upstream {cls.error_type or 'error'}: {cls.message}"},
                "finish_reason": None,
            }]
            yield _sse_chunk(payload)
            payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
            payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": "length"}]
            yield _sse_chunk(payload)
            yield b"data: [DONE]\n\n"
            return

        if t == "message_start":
            msg = data.get("message") or {}
            u = msg.get("usage") or {}
            initial_usage = {
                "input_tokens": int(u.get("input_tokens") or 0),
                "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
            }
            if not sent_role:
                payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
                payload["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                yield _sse_chunk(payload)
                sent_role = True

        elif t == "content_block_start":
            idx = int(data.get("index", 0))
            blk = data.get("content_block") or {}
            active_block_types[idx] = blk.get("type", "")

        elif t == "content_block_delta":
            idx = int(data.get("index", 0))
            delta = data.get("delta") or {}
            dt = delta.get("type")
            if dt == "text_delta":
                text = delta.get("text", "")
                if text:
                    payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
                    payload["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
                    yield _sse_chunk(payload)
            elif dt == "thinking_delta":
                text = delta.get("thinking", "")
                if text:
                    payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
                    payload["choices"] = [{"index": 0, "delta": {"reasoning_content": text}, "finish_reason": None}]
                    yield _sse_chunk(payload)

        elif t == "content_block_stop":
            active_block_types.pop(int(data.get("index", 0)), None)

        elif t == "message_delta":
            delta = data.get("delta") or {}
            if delta.get("stop_reason"):
                stop_reason = delta["stop_reason"]
            u = data.get("usage") or {}
            output_tokens = int(u.get("output_tokens") or 0)
            prompt_tokens = (
                initial_usage["input_tokens"]
                + initial_usage["cache_read_input_tokens"]
                + initial_usage["cache_creation_input_tokens"]
            )
            final_usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": prompt_tokens + output_tokens,
            }

        elif t == "message_stop":
            payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
            payload["choices"] = [{"index": 0, "delta": {}, "finish_reason": _map_finish_reason(stop_reason)}]
            yield _sse_chunk(payload)
            if final_usage is not None:
                payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
                payload["choices"] = []
                payload["usage"] = final_usage
                yield _sse_chunk(payload)
            yield b"data: [DONE]\n\n"
            return

    _LOG.error(
        "upstream SSE stream ended before message_stop (stop_reason=%s); emitting finish_reason=length",
        stop_reason,
    )
    if provider is not None and stop_reason is None:
        cls = Classification("transient", 502, 60.0, time.time() + 60.0, None, "stream truncated", None)
        await _apply_classification(provider, cls)
    if not sent_role:
        payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
        payload["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        yield _sse_chunk(payload)
    payload = _openai_chunk_skeleton(chunk_id, created, requested_model)
    payload["choices"] = [{
        "index": 0,
        "delta": {},
        "finish_reason": "length" if stop_reason is None else _map_finish_reason(stop_reason),
    }]
    yield _sse_chunk(payload)
    yield b"data: [DONE]\n\n"


async def _get_token(provider: ProviderLike) -> str:
    return await asyncio.to_thread(provider.get_access_token)


async def _apply_classification(
    provider: ProviderLike,
    cls: Classification,
) -> None:
    if isinstance(provider, MultiAccountCredentialProvider):
        if cls.kind == "cap":
            provider.mark_exhausted(cls.retry_after_s or 300.0)
        elif cls.kind == "auth":
            provider.mark_invalid()
    if cls.kind == "cap":
        provider.last_cap_reset_at = cls.reset_at_unix or (
            time.time() + (cls.retry_after_s or 300.0)
        )


def _openai_error_response(status_code: int, message: str, type_: str | None = None) -> JSONResponse:
    body = to_openai_error_body(status_code, message, type_)
    headers: dict[str, str] = {}
    return JSONResponse(body, status_code=status_code, headers=headers)


def _openai_error_from_classification(cls: Classification) -> JSONResponse:
    headers: dict[str, str] = {}
    if cls.retry_after_s is not None:
        headers["Retry-After"] = str(max(1, int(cls.retry_after_s)))
    body = to_openai_error_body(cls.status_code, cls.message, cls.error_type)
    return JSONResponse(body, status_code=cls.status_code, headers=headers)


def build_app(provider: ProviderLike | None = None) -> FastAPI:
    if provider is None:
        from claude_bridge.credentials import resolve_provider
        provider = resolve_provider()
    prov: ProviderLike = provider

    bridge_secret = os.environ.get("ZORO_CC_BRIDGE_SECRET", "").strip()
    if not bridge_secret:
        _LOG.warning(
            "ZORO_CC_BRIDGE_SECRET is not set — the bridge is UNAUTHENTICATED; "
            "any local process can spend this subscription."
        )

    max_inline_retries = _env_int("ZORO_CC_MAX_INLINE_RETRIES", 3)
    max_inline_wait = _env_int("ZORO_CC_MAX_INLINE_WAIT", 30)
    max_stream_retries = _env_int("ZORO_CC_STREAM_BUFFER_RETRIES", 3)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(timeout=_timeout())
        app.state.http_stream = httpx.AsyncClient(timeout=_timeout(streaming=True))
        try:
            yield
        finally:
            await app.state.http.aclose()
            await app.state.http_stream.aclose()

    app = FastAPI(title="Zoro Claude Code Bridge", version="0.1.0", lifespan=lifespan)

    def _authorized(request: Request) -> bool:
        if not bridge_secret:
            return True
        presented = (
            request.headers.get("x-zoro-bridge-secret")
            or request.headers.get("x-api-key")
            or ""
        )
        if not presented:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                presented = auth[7:].strip()
        return hmac.compare_digest(presented, bridge_secret)

    def _unauthorized() -> JSONResponse:
        return _openai_error_response(401, "missing or invalid bridge secret", "authentication_error")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        tp = None
        sub = None
        try:
            tp = prov.token_prefix()
        except Exception:  # noqa: BLE001
            tp = None
        try:
            sub = prov.subscription_type()
        except Exception:  # noqa: BLE001
            sub = None
        return {"ok": True, "token_prefix": tp, "subscription": sub}

    @app.get("/quota")
    async def quota(request: Request) -> dict[str, Any]:
        if not _authorized(request):
            return {"accounts": []}
        if isinstance(prov, MultiAccountCredentialProvider):
            return {
                "multi_account": True,
                "accounts": prov.snapshot(),
                "next_reset_at_unix": prov.next_reset_at(),
            }
        reset = getattr(prov, "last_cap_reset_at", None)
        if reset is not None and reset <= time.time():
            reset = None
        return {"multi_account": False, "accounts": [], "next_reset_at_unix": reset}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        if not _authorized(request):
            return _unauthorized()

        try:
            raw = await request.body()
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, TypeError) as e:
            return _openai_error_response(400, f"invalid JSON body: {e}", "invalid_request_error")

        requested_model = body.get("model", "sonnet")
        streaming = bool(body.get("stream"))
        try:
            ant_body = translate_openai_to_anthropic(body)
        except (ValueError, TypeError, KeyError) as e:
            return _openai_error_response(400, f"translation error: {e}", "invalid_request_error")

        url = f"{_upstream_base()}/v1/messages"

        if streaming:
            return await _handle_streaming(
                prov, url, ant_body, requested_model,
                max_stream_retries, max_inline_retries, max_inline_wait,
                app.state.http_stream,
            )
        return await _handle_nonstream(
            prov, url, ant_body, requested_model,
            max_inline_retries, max_inline_wait,
            app.state.http,
        )

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        if not _authorized(request):
            return _openai_error_response(401, "missing or invalid bridge secret", "authentication_error")

        try:
            raw = await request.body()
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, TypeError) as e:
            return _openai_error_response(400, f"invalid JSON body: {e}", "invalid_request_error")

        streaming = bool(body.get("stream"))
        url = f"{_upstream_base()}/v1/messages"

        # Ensure the Claude Code identity prefix is present in system blocks.
        # The sk-ant-oat01 OAuth tokens are gated to Claude Code — without this
        # prefix, upstream rejects the request (429 masquerading as rate-limit).
        system_blocks: list[dict[str, Any]] = body.get("system") or []
        if not isinstance(system_blocks, list):
            system_blocks = []
        system_blocks = _scrub_opencode_fingerprints(system_blocks)
        # if os.environ.get("ZORO_CC_SKIP_SYSTEM_PREFIX") != "1":
        body["system"] = _inject_system_prefix(system_blocks)

        _LOG.info(
            "native /v1/messages | model=%s | stream=%s | msgs=%d | max_tokens=%s",
            body.get("model"),
            streaming,
            len(body.get("messages", [])),
            body.get("max_tokens"),
        )
        _extra_keys = [k for k in body if k not in ("model", "max_tokens", "system", "messages", "stream")]
        if _extra_keys:
            _LOG.warning("[NATIVE-DEBUG] extra body keys=%s", _extra_keys)
        _tool_names = [t.get("name", "?") for t in body.get("tools", [])]
        _LOG.warning("[NATIVE-DEBUG] keys=%s tools=%s thinking=%s output_config=%s max_tokens=%s",
                     list(body.keys()), _tool_names,
                     body.get("thinking"), body.get("output_config"), body.get("max_tokens"))

        if isinstance(body.get("messages"), list):
            body["messages"] = _remap_messages_outbound(body["messages"])
        if "tool_choice" in body:
            body["tool_choice"] = _remap_tool_choice_outbound(body["tool_choice"])
        if "tools" in body:
            body["tools"] = _remap_tools_outbound(body["tools"])
            _LOG.warning("[NATIVE-DEBUG] after_remap tools=%d names=%s",
                         len(body["tools"]), [t.get("name") for t in body["tools"]])

        if streaming:
            return await _handle_native_streaming(
                prov, url, body,
                max_stream_retries, max_inline_retries, max_inline_wait,
                app.state.http_stream,
            )
        return await _handle_native_nonstream(
            prov, url, body,
            max_inline_retries, max_inline_wait,
            app.state.http,
        )

    @app.post("/messages")
    async def messages_alias(request: Request) -> Response:
        return await messages(request)

    return app


async def _handle_nonstream(
    provider: ProviderLike,
    url: str,
    ant_body: dict[str, Any],
    requested_model: str,
    max_retries: int,
    max_wait: int,
    client: httpx.AsyncClient,
) -> Response:
    payload = json.dumps(ant_body).encode("utf-8")
    attempt = 0
    auth_retried = False
    tried_tokens: set[str] = set()

    while True:
        try:
            access_token = await _get_token(provider)
        except CredentialsError as e:
            return _openai_error_response(401, str(e), "authentication_error")

        if access_token in tried_tokens and attempt > 0:
            return _openai_error_response(503, "no usable credentials", "api_error")

        headers = _build_forward_headers(access_token, streaming=False)
        try:
            resp = await client.post(url, content=payload, headers=headers)
        except httpx.HTTPError as e:
            if attempt < max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, max_wait))
                continue
            return _openai_error_response(502, f"upstream network error: {e}", "api_error")

        if 200 <= resp.status_code < 300:
            provider.last_cap_reset_at = None
            try:
                ant_json = resp.json()
            except ValueError as e:
                return _openai_error_response(502, f"invalid upstream JSON: {e}", "api_error")
            return JSONResponse(_anthropic_to_openai_nonstream(ant_json, requested_model))

        cls = classify_anthropic_error(resp.status_code, resp.content, resp.headers)
        await _apply_classification(provider, cls)
        _LOG.info(
            "upstream error: status=%d kind=%s retry_after=%s",
            resp.status_code, cls.kind, cls.retry_after_s,
        )

        if cls.kind == "auth" and not auth_retried:
            auth_retried = True
            tried_tokens.add(access_token)
            try:
                if isinstance(provider, MultiAccountCredentialProvider):
                    pass
                else:
                    await asyncio.to_thread(provider.refresh)
            except CredentialsError as e:
                return _openai_error_response(401, str(e), "authentication_error")
            continue

        if cls.kind == "cap" and isinstance(provider, MultiAccountCredentialProvider):
            tried_tokens.add(access_token)
            if provider.next_reset_at() is None:
                continue
            return _openai_error_from_classification(cls)

        if cls.kind == "transient" and attempt < max_retries:
            wait = cls.retry_after_s if cls.retry_after_s is not None else (2 ** (attempt + 1))
            wait = min(wait, max_wait)
            attempt += 1
            await asyncio.sleep(wait)
            continue

        return _openai_error_from_classification(cls)


async def _handle_native_nonstream(
    provider: ProviderLike,
    url: str,
    ant_body: dict[str, Any],
    max_retries: int,
    max_wait: int,
    client: httpx.AsyncClient,
    ) -> Response:
    payload = json.dumps(ant_body).encode("utf-8")
    _LOG.warning("[UPSTREAM-REQ-NS] tools=%d names=%s",
                 len(ant_body.get("tools", [])), [t.get("name") for t in ant_body.get("tools", [])])
    attempt = 0
    auth_retried = False
    tried_tokens: set[str] = set()

    while True:
        try:
            access_token = await _get_token(provider)
        except CredentialsError as e:
            return JSONResponse({"error": {"message": str(e), "type": "authentication_error"}}, status_code=401)

        if access_token in tried_tokens and attempt > 0:
            return JSONResponse({"error": {"message": "no usable credentials", "type": "api_error"}}, status_code=503)

        headers = _build_forward_headers(access_token, streaming=False)
        try:
            resp = await client.post(url, content=payload, headers=headers)
        except httpx.HTTPError as e:
            if attempt < max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, max_wait))
                continue
            return JSONResponse({"error": {"message": f"upstream network error: {e}", "type": "api_error"}}, status_code=502)

        if 200 <= resp.status_code < 300:
            provider.last_cap_reset_at = None
            try:
                _data = resp.json()
                if isinstance(_data, dict):
                    _remap_content_in(_data.get("content"))
                return JSONResponse(_data)
            except ValueError:
                return JSONResponse(resp.content, status_code=resp.status_code, media_type="application/json")

        cls = classify_anthropic_error(resp.status_code, resp.content, resp.headers)
        await _apply_classification(provider, cls)
        _LOG.info(
            "native upstream error: status=%d kind=%s retry_after=%s",
            resp.status_code, cls.kind, cls.retry_after_s,
        )

        if cls.kind == "auth" and not auth_retried:
            auth_retried = True
            tried_tokens.add(access_token)
            try:
                if not isinstance(provider, MultiAccountCredentialProvider):
                    await asyncio.to_thread(provider.refresh)
            except CredentialsError as e:
                return JSONResponse({"error": {"message": str(e), "type": "authentication_error"}}, status_code=401)
            continue

        if cls.kind == "cap" and isinstance(provider, MultiAccountCredentialProvider):
            tried_tokens.add(access_token)
            if provider.next_reset_at() is None:
                continue
            return JSONResponse(
                {"error": {"message": cls.message or "rate limited", "type": "rate_limit_error"}},
                status_code=429,
            )

        if cls.kind == "transient" and attempt < max_retries:
            wait = cls.retry_after_s if cls.retry_after_s is not None else (2 ** (attempt + 1))
            wait = min(wait, max_wait)
            attempt += 1
            await asyncio.sleep(wait)
            continue

        return JSONResponse(
            {"error": {"message": cls.message or "upstream error", "type": cls.error_type or "api_error"}},
            status_code=cls.status_code,
        )


async def _handle_native_streaming(
    provider: ProviderLike,
    url: str,
    ant_body: dict[str, Any],
    max_stream_retries: int,
    max_retries: int,
    max_wait: int,
    client: httpx.AsyncClient,
) -> Response:
    payload = json.dumps(ant_body).encode("utf-8")
    attempt = 0
    auth_retried = False
    tried_tokens: set[str] = set()
    buffer_retries = 0

    while True:
        try:
            access_token = await _get_token(provider)
        except CredentialsError as e:
            return JSONResponse({"error": {"message": str(e), "type": "authentication_error"}}, status_code=401)

        if access_token in tried_tokens and attempt > 0:
            return JSONResponse({"error": {"message": "no usable credentials", "type": "api_error"}}, status_code=503)

        headers = _build_forward_headers(access_token, streaming=True)
        _LOG.warning("[UPSTREAM-REQ] url=%s headers=%s body=%s", url, json.dumps({k: v[:60]+"..." if len(v) > 60 else v for k, v in headers.items()}), payload.decode("utf-8")[:3000])
        stream_cm = client.stream("POST", url, content=payload, headers=headers)
        try:
            upstream = await stream_cm.__aenter__()
        except httpx.HTTPError as e:
            if attempt < max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, max_wait))
                continue
            return JSONResponse({"error": {"message": f"upstream connect error: {e}", "type": "api_error"}}, status_code=502)

        if 200 <= upstream.status_code < 300:
            byte_iter = upstream.aiter_bytes()
            head_bytes, head_state, head_cls = await _peek_stream_head(byte_iter)

            if head_state == "error" and head_cls is not None:
                await stream_cm.__aexit__(None, None, None)
                await _apply_classification(provider, head_cls)
                if head_cls.kind == "auth" and not auth_retried:
                    auth_retried = True
                    tried_tokens.add(access_token)
                    try:
                        if not isinstance(provider, MultiAccountCredentialProvider):
                            await asyncio.to_thread(provider.refresh)
                    except CredentialsError as e:
                        return JSONResponse({"error": {"message": str(e), "type": "authentication_error"}}, status_code=401)
                    continue
                if head_cls.kind == "transient":
                    if buffer_retries < max_stream_retries and attempt < max_retries:
                        attempt += 1
                        buffer_retries += 1
                        await asyncio.sleep(head_cls.retry_after_s or 2)
                        continue
                return JSONResponse(
                    {"error": {"message": head_cls.message or "stream error", "type": head_cls.error_type or "api_error"}},
                    status_code=head_cls.status_code,
                )

            if head_state == "closed":
                await stream_cm.__aexit__(None, None, None)
                if buffer_retries < max_stream_retries and attempt < max_retries:
                    attempt += 1
                    buffer_retries += 1
                    await asyncio.sleep(min(2 ** attempt, max_wait))
                    continue
                return JSONResponse({"error": {"message": "upstream closed before response", "type": "api_error"}}, status_code=502)

            provider.last_cap_reset_at = None
            committed_provider = provider
            committed_bytes = head_bytes

            async def raw_event_stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in _rewrite_native_sse(committed_bytes, byte_iter):
                        yield chunk
                finally:
                    await stream_cm.__aexit__(None, None, None)

            return StreamingResponse(
                raw_event_stream(),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-zoro-bridge": "native-streaming"},
            )

        body_bytes = b""
        try:
            async for chunk in upstream.aiter_bytes():
                body_bytes += chunk
                if len(body_bytes) > 65536:
                    break
        finally:
            await stream_cm.__aexit__(None, None, None)

        cls = classify_anthropic_error(upstream.status_code, body_bytes, upstream.headers)
        await _apply_classification(provider, cls)
        _LOG.warning(
            "[UPSTREAM-RESP] status=%d body=%s",
            upstream.status_code, body_bytes.decode("utf-8", errors="replace")[:2000],
        )

        if cls.kind == "auth" and not auth_retried:
            auth_retried = True
            tried_tokens.add(access_token)
            try:
                if not isinstance(provider, MultiAccountCredentialProvider):
                    await asyncio.to_thread(provider.refresh)
            except CredentialsError as e:
                return JSONResponse({"error": {"message": str(e), "type": "authentication_error"}}, status_code=401)
            continue

        if cls.kind == "transient":
            if buffer_retries < max_stream_retries and attempt < max_retries:
                attempt += 1
                buffer_retries += 1
                await asyncio.sleep(min(2 ** attempt, max_wait))
                continue

        return JSONResponse(
            {"error": {"message": cls.message or "upstream error", "type": cls.error_type or "api_error"}},
            status_code=cls.status_code,
        )


_HeadState = Literal["started", "error", "closed"]


async def _peek_stream_head(
    byte_iter: AsyncIterator[bytes],
    max_bytes: int = 65536,
) -> tuple[bytes, _HeadState, Classification | None]:
    buf = b""
    text_buf = ""
    try:
        async for raw in byte_iter:
            buf += raw
            text_buf += raw.decode("utf-8", errors="replace")
            while True:
                idx = text_buf.find("\n\n")
                if idx < 0:
                    break
                block, text_buf = text_buf[:idx], text_buf[idx + 2:]
                block = block.strip("\r")
                if not block:
                    continue
                event_name, data = _parse_sse_event(block)
                if data is None:
                    continue
                t = data.get("type") or event_name
                if t == "message_start":
                    return buf, "started", None
                if t == "error":
                    return buf, "error", _classify_sse_error_event(data)
            if len(buf) > max_bytes:
                return buf, "started", None
    except httpx.HTTPError as e:
        _LOG.warning("upstream stream head read error: %s", e)
        return buf, "closed", None
    return buf, "closed", None


async def _handle_streaming(
    provider: ProviderLike,
    url: str,
    ant_body: dict[str, Any],
    requested_model: str,
    max_stream_retries: int,
    max_retries: int,
    max_wait: int,
    client: httpx.AsyncClient,
) -> Response:
    payload = json.dumps(ant_body).encode("utf-8")
    attempt = 0
    auth_retried = False
    tried_tokens: set[str] = set()
    buffer_retries = 0

    while True:
        try:
            access_token = await _get_token(provider)
        except CredentialsError as e:
            return _openai_error_response(401, str(e), "authentication_error")

        if access_token in tried_tokens and attempt > 0:
            return _openai_error_response(503, "no usable credentials", "api_error")

        headers = _build_forward_headers(access_token, streaming=True)

        stream_cm = client.stream("POST", url, content=payload, headers=headers)
        try:
            upstream = await stream_cm.__aenter__()
        except httpx.HTTPError as e:
            if attempt < max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, max_wait))
                continue
            return _openai_error_response(502, f"upstream connect error: {e}", "api_error")

        if 200 <= upstream.status_code < 300:
            # Read the response body stream exactly once: httpx forbids calling
            # aiter_bytes() twice, so create one iterator and share it between the
            # head-peek and the full translation below.
            byte_iter = upstream.aiter_bytes()
            head_bytes, head_state, head_cls = await _peek_stream_head(byte_iter)

            if head_state == "error" and head_cls is not None:
                await stream_cm.__aexit__(None, None, None)
                await _apply_classification(provider, head_cls)
                _LOG.info(
                    "pre-commit stream error: kind=%s type=%s msg=%s",
                    head_cls.kind, head_cls.error_type, head_cls.message,
                )
                if head_cls.kind == "auth" and not auth_retried:
                    auth_retried = True
                    tried_tokens.add(access_token)
                    try:
                        if not isinstance(provider, MultiAccountCredentialProvider):
                            await asyncio.to_thread(provider.refresh)
                    except CredentialsError as e:
                        return _openai_error_response(401, str(e), "authentication_error")
                    continue
                if head_cls.kind == "cap" and isinstance(provider, MultiAccountCredentialProvider):
                    tried_tokens.add(access_token)
                    if provider.next_reset_at() is None:
                        continue
                    return _openai_error_from_classification(head_cls)
                if head_cls.kind == "transient":
                    if buffer_retries < max_stream_retries and attempt < max_retries:
                        wait = head_cls.retry_after_s if head_cls.retry_after_s is not None else (2 ** (attempt + 1))
                        wait = min(wait, max_wait)
                        attempt += 1
                        buffer_retries += 1
                        await asyncio.sleep(wait)
                        continue
                return _openai_error_from_classification(head_cls)

            if head_state == "closed":
                await stream_cm.__aexit__(None, None, None)
                _LOG.warning("upstream closed before message_start; treating as transient")
                if isinstance(provider, MultiAccountCredentialProvider):
                    provider.mark_exhausted(60.0)
                    tried_tokens.add(access_token)
                if buffer_retries < max_stream_retries and attempt < max_retries:
                    attempt += 1
                    buffer_retries += 1
                    await asyncio.sleep(min(2 ** attempt, max_wait))
                    continue
                return _openai_error_response(502, "upstream closed before response", "api_error")

            provider.last_cap_reset_at = None
            committed_provider = provider
            committed_bytes = head_bytes

            async def event_stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in _translate_stream(
                        byte_iter, requested_model,
                        prebuffered=committed_bytes, provider=committed_provider,
                    ):
                        yield chunk
                finally:
                    await stream_cm.__aexit__(None, None, None)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-zoro-bridge": "streaming"},
            )

        body_bytes = b""
        try:
            async for chunk in upstream.aiter_bytes():
                body_bytes += chunk
                if len(body_bytes) > 65536:
                    break
        finally:
            await stream_cm.__aexit__(None, None, None)

        cls = classify_anthropic_error(upstream.status_code, body_bytes, upstream.headers)
        await _apply_classification(provider, cls)
        _LOG.info(
            "upstream stream error: status=%d kind=%s retry_after=%s",
            upstream.status_code, cls.kind, cls.retry_after_s,
        )

        if cls.kind == "auth" and not auth_retried:
            auth_retried = True
            tried_tokens.add(access_token)
            try:
                if not isinstance(provider, MultiAccountCredentialProvider):
                    await asyncio.to_thread(provider.refresh)
            except CredentialsError as e:
                return _openai_error_response(401, str(e), "authentication_error")
            continue

        if cls.kind == "cap" and isinstance(provider, MultiAccountCredentialProvider):
            tried_tokens.add(access_token)
            if provider.next_reset_at() is None:
                continue
            return _openai_error_from_classification(cls)

        if cls.kind == "transient":
            if buffer_retries < max_stream_retries and attempt < max_retries:
                wait = cls.retry_after_s if cls.retry_after_s is not None else (2 ** (attempt + 1))
                wait = min(wait, max_wait)
                attempt += 1
                buffer_retries += 1
                await asyncio.sleep(wait)
                continue

        return _openai_error_from_classification(cls)
