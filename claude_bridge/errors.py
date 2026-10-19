from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping


TRANSIENT_RETRY_AFTER_THRESHOLD = 60

ErrorKind = Literal["transient", "cap", "auth", "client", "unknown"]


class UpstreamError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_type: str | None = None,
        retry_after_s: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.retry_after_s = retry_after_s
        self.request_id = request_id


@dataclass
class Classification:
    kind: ErrorKind
    status_code: int
    retry_after_s: float | None
    reset_at_unix: float | None
    error_type: str | None
    message: str
    request_id: str | None


def _parse_int(headers: Mapping[str, str], key: str) -> int | None:
    val = headers.get(key) or headers.get(key.lower())
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_reset(headers: Mapping[str, str], key: str) -> float | None:
    val = headers.get(key) or headers.get(key.lower())
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        norm = val.rstrip()
        if norm.endswith("Z"):
            norm = norm[:-1] + "+00:00"
        return datetime.fromisoformat(norm).timestamp()
    except (TypeError, ValueError):
        return None


def extract_retry_after(headers: Mapping[str, str]) -> float | None:
    explicit = _parse_int(headers, "Retry-After")
    if explicit is not None and explicit >= 0:
        return float(explicit)
    now = time.time()
    for key in (
        "anthropic-ratelimit-unified-tokens-reset",
        "anthropic-ratelimit-unified-requests-reset",
        "anthropic-ratelimit-tokens-reset",
        "anthropic-ratelimit-requests-reset",
    ):
        r = _parse_reset(headers, key)
        if r is not None:
            delta = r - now
            if delta > 0:
                return delta
    return None


def _extract_reset_at(headers: Mapping[str, str]) -> float | None:
    for key in (
        "anthropic-ratelimit-unified-tokens-reset",
        "anthropic-ratelimit-unified-requests-reset",
        "anthropic-ratelimit-tokens-reset",
        "anthropic-ratelimit-requests-reset",
    ):
        v = _parse_reset(headers, key)
        if v is not None:
            return v
    ra = extract_retry_after(headers)
    if ra is not None:
        return time.time() + ra
    return None


def _decode_body(body: bytes | str | None) -> tuple[str | None, str | None, str | None]:
    if body is None:
        return None, None, None
    if isinstance(body, (bytes, bytearray)):
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None, None, None
    else:
        text = body
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None, text[:200] if text else None, None
    if not isinstance(obj, dict):
        return None, None, None
    err = obj.get("error") or {}
    if isinstance(err, dict):
        return err.get("type"), err.get("message"), obj.get("request_id")
    return None, str(err)[:200], obj.get("request_id")


def classify_anthropic_error(
    status_code: int,
    body: bytes | str | None,
    headers: Mapping[str, str] | None = None,
) -> Classification:
    headers = headers or {}
    err_type, message, request_id = _decode_body(body)
    retry_after = extract_retry_after(headers)
    reset_at = _extract_reset_at(headers)
    message = message or err_type or f"HTTP {status_code}"

    def make(kind: ErrorKind) -> Classification:
        return Classification(
            kind=kind,
            status_code=status_code,
            retry_after_s=retry_after,
            reset_at_unix=reset_at,
            error_type=err_type,
            message=message,
            request_id=request_id,
        )

    if status_code in (401, 403):
        return make("auth")

    if status_code == 429:
        tokens_remaining = _parse_int(
            headers, "anthropic-ratelimit-unified-tokens-remaining"
        )
        if tokens_remaining is None:
            tokens_remaining = _parse_int(
                headers, "anthropic-ratelimit-tokens-remaining"
            )
        is_cap = False
        if retry_after is not None and retry_after >= TRANSIENT_RETRY_AFTER_THRESHOLD:
            is_cap = True
        if tokens_remaining == 0:
            is_cap = True
        _msg = (message or "").lower()
        if any(s in _msg for s in (
            "usage limit", "5-hour", "5 hour", "weekly limit", "subscription",
            "exceeded your", "rate limit will reset",
        )):
            is_cap = True
        return make("cap" if is_cap else "transient")

    if status_code == 529:
        return make("transient")

    if 500 <= status_code < 600:
        return make("transient")

    if status_code == 400:
        return make("client")

    if 400 <= status_code < 500:
        return make("client")

    return make("unknown")


_STATUS_TO_OPENAI_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}


def to_openai_error_body(
    status_code: int,
    message: str,
    type_: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    err_type = type_ or _STATUS_TO_OPENAI_TYPE.get(status_code, "api_error")
    return {
        "error": {
            "message": message,
            "type": err_type,
            "code": code,
        }
    }
