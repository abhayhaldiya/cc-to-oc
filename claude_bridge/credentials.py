from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

try:
    from curl_cffi import requests as _curl_cffi_requests  # type: ignore
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _curl_cffi_requests = None  # type: ignore
    _CURL_CFFI_AVAILABLE = False

_LOG = logging.getLogger(__name__)

CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_ENDPOINT = "https://api.anthropic.com/v1/oauth/token"
REFRESH_LEEWAY_SECONDS = 60
_REFRESH_IMPERSONATE_PROFILE = "chrome124"
_REFRESH_USER_AGENT = "claude-cli/2.0.170 (external, cli)"
_REFRESH_BETA_HEADER = "oauth-2025-04-20,claude-code-20250219"

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CACHE_DIR = Path.home() / ".cache" / "zoro-harness"
_CACHE_PATH = _CACHE_DIR / "claude_creds.json"


class CredentialsError(RuntimeError):
    pass


@dataclass
class OAuthCredentials:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: list[str]
    subscription_type: Optional[str] = None

    @classmethod
    def from_claude_payload(cls, payload: dict) -> "OAuthCredentials":
        cc = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        cc = cc or payload
        try:
            return cls(
                access_token=cc["accessToken"],
                refresh_token=cc["refreshToken"],
                expires_at_ms=int(cc["expiresAt"]),
                scopes=list(cc.get("scopes") or []),
                subscription_type=cc.get("subscriptionType"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise CredentialsError(f"Malformed Claude Code credentials: {e}") from e

    def to_claude_payload(self) -> dict:
        return {
            "claudeAiOauth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at_ms,
                "scopes": self.scopes,
                "subscriptionType": self.subscription_type,
            }
        }

    def is_expired(self, leeway_seconds: int = REFRESH_LEEWAY_SECONDS) -> bool:
        return time.time() >= (self.expires_at_ms / 1000.0) - leeway_seconds


def _read_inline_env() -> Optional[str]:
    raw = os.environ.get("CLAUDE_CODE_CREDENTIALS")
    return raw if raw else None


def _read_credentials_file() -> Optional[str]:
    candidates: list[str] = []
    env_path = os.environ.get("ZORO_CC_CREDS_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append(str(Path.home() / ".claude" / ".credentials.json"))
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError as e:
                _LOG.debug("credentials file %s read failed: %s", p, e)
    return None


def _read_keychain_macos() -> Optional[str]:
    if platform.system() != "Darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.debug("keychain read failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def _read_cache_file() -> Optional[str]:
    if _CACHE_PATH.is_file():
        try:
            return _CACHE_PATH.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def load_credentials() -> OAuthCredentials:
    raw = (
        _read_inline_env()
        or _read_credentials_file()
        or _read_keychain_macos()
        or _read_cache_file()
    )
    if not raw:
        raise CredentialsError(
            "No Claude Code credentials found. Sign in via the `claude` CLI "
            "first, then verify with:\n"
            "  security find-generic-password -s 'Claude Code-credentials' -w"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialsError(f"Credentials are not valid JSON: {e}") from e
    return OAuthCredentials.from_claude_payload(payload)


class _RefreshResponse:
    __slots__ = ("status_code", "text", "_json")

    def __init__(self, status_code: int, text: str, body: dict | None) -> None:
        self.status_code = status_code
        self.text = text
        self._json = body

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


def _refresh_http_post(
    refresh_token: str, timeout: float
) -> _RefreshResponse:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
    }
    headers = {
        "Content-Type": "application/json",
        "anthropic-beta": _REFRESH_BETA_HEADER,
        "User-Agent": _REFRESH_USER_AGENT,
    }

    if _CURL_CFFI_AVAILABLE and _curl_cffi_requests is not None:
        r = _curl_cffi_requests.post(
            REFRESH_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=timeout,
            impersonate=_REFRESH_IMPERSONATE_PROFILE,
        )
        text = r.text if r.text is not None else ""
        body: Optional[dict] = None
        if r.status_code == 200:
            try:
                body = r.json()
            except Exception:
                body = None
        return _RefreshResponse(status_code=r.status_code, text=text, body=body)

    with httpx.Client(timeout=timeout) as client:
        h = client.post(REFRESH_ENDPOINT, json=payload, headers=headers)
    body_h: Optional[dict] = None
    if h.status_code == 200:
        try:
            body_h = h.json()
        except ValueError:
            body_h = None
    return _RefreshResponse(status_code=h.status_code, text=h.text, body=body_h)


def refresh_credentials(
    creds: OAuthCredentials,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    backoff_base: float = 1.0,
) -> OAuthCredentials:
    last_error: Optional[Exception] = None
    r: Optional[_RefreshResponse] = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = _refresh_http_post(creds.refresh_token, timeout=timeout)
        except (httpx.HTTPError, OSError) as e:
            last_error = e
            if attempt >= max_attempts:
                raise CredentialsError(
                    f"OAuth refresh network error after {attempt} attempts: {e}"
                ) from e
            time.sleep(backoff_base * (2 ** (attempt - 1)))
            continue

        if r.status_code == 200:
            break
        if 400 <= r.status_code < 500:
            raise CredentialsError(
                f"OAuth refresh failed (non-retryable): HTTP {r.status_code} {r.text[:200]}"
            )
        last_error = CredentialsError(
            f"OAuth refresh failed: HTTP {r.status_code} {r.text[:200]}"
        )
        if attempt >= max_attempts:
            raise last_error
        time.sleep(backoff_base * (2 ** (attempt - 1)))
    else:
        raise CredentialsError(
            f"OAuth refresh failed after {max_attempts} attempts: {last_error}"
        )

    assert r is not None
    try:
        body = r.json()
    except ValueError as e:
        raise CredentialsError(f"OAuth refresh returned non-JSON: {e}") from e

    access_token = body.get("access_token")
    if not access_token:
        raise CredentialsError(f"OAuth refresh missing access_token: {body}")
    refresh_token = body.get("refresh_token") or creds.refresh_token
    expires_in = int(body.get("expires_in", 3600))
    return OAuthCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=int(time.time() * 1000) + expires_in * 1000,
        scopes=creds.scopes,
        subscription_type=creds.subscription_type,
    )


def _atomic_write_creds(path: Path, creds: OAuthCredentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(creds.to_claude_payload()))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def write_cache(creds: OAuthCredentials) -> None:
    _atomic_write_creds(_CACHE_PATH, creds)


def _service_cache_path(service: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", service)
    return _CACHE_DIR / f"claude_creds_kc_{safe}.json"


def _keychain_write_back_enabled() -> bool:
    return os.environ.get("ZORO_CC_WRITE_BACK_KEYCHAIN", "").strip().lower() in (
        "1", "true", "yes",
    )


def _keychain_account_for_service(service: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-g"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    m = re.search(r'"acct"<blob>="([^"]*)"', r.stderr or "")
    return m.group(1) if m else None


def _keychain_write_back(service: str, creds: OAuthCredentials) -> bool:
    if platform.system() != "Darwin":
        return False
    account = _keychain_account_for_service(service)
    if account is None:
        return False
    payload = json.dumps(creds.to_claude_payload())
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-a", account, "-s", service, "-w", payload],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.warning("keychain write-back failed for %s: %s", service, e)
        return False
    return r.returncode == 0


def _warn_refresh_rotation(source: str, wrote_back: bool) -> None:
    if wrote_back:
        _LOG.info("Refreshed token written back to %s; claude CLI stays in sync", source)
        return
    _LOG.warning(
        "Bridge refreshed the OAuth token from %s but did NOT write the rotated "
        "refresh token back. The `claude` CLI sharing this credential may now be "
        "logged out. Set ZORO_CC_WRITE_BACK_KEYCHAIN=1 to keep Keychain in sync, "
        "or give the bridge its own dedicated account.",
        source,
    )


class CredentialProvider:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._creds: Optional[OAuthCredentials] = None
        self.last_cap_reset_at: Optional[float] = None

    def _load(self) -> OAuthCredentials:
        return load_credentials()

    def _persist_after_refresh(self, creds: OAuthCredentials) -> None:
        try:
            write_cache(creds)
        except OSError as e:
            _LOG.warning("Could not persist refreshed creds to cache: %s", e)
        wrote_back = (
            _keychain_write_back(_KEYCHAIN_SERVICE, creds)
            if _keychain_write_back_enabled()
            else False
        )
        _warn_refresh_rotation(f"keychain {_KEYCHAIN_SERVICE!r}", wrote_back)

    def preflight(self) -> None:
        with self._lock:
            if self._creds is None:
                self._creds = self._load()

    def get_access_token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds = self._load()
            creds = self._creds
        if not creds.is_expired():
            return creds.access_token
        with self._refresh_lock:
            with self._lock:
                creds = self._creds
            if creds is not None and not creds.is_expired():
                return creds.access_token
            if creds is None:
                with self._lock:
                    if self._creds is None:
                        self._creds = self._load()
                    creds = self._creds
                if not creds.is_expired():
                    return creds.access_token
            _LOG.info("Refreshing Claude Code OAuth token")
            new_creds = refresh_credentials(creds)
            self._persist_after_refresh(new_creds)
            with self._lock:
                self._creds = new_creds
            return new_creds.access_token

    def refresh(self) -> str:
        with self._refresh_lock:
            with self._lock:
                if self._creds is None:
                    self._creds = self._load()
                creds = self._creds
            _LOG.info("Forced refresh of Claude Code OAuth token")
            new_creds = refresh_credentials(creds)
            self._persist_after_refresh(new_creds)
            with self._lock:
                self._creds = new_creds
            return new_creds.access_token

    def force_reload(self) -> None:
        with self._lock:
            self._creds = None

    def token_prefix(self) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None

    def subscription_type(self) -> Optional[str]:
        with self._lock:
            return self._creds.subscription_type if self._creds else None


class _FileCredentialProvider(CredentialProvider):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path).expanduser()

    def _load(self) -> OAuthCredentials:
        if not self._path.is_file():
            raise CredentialsError(f"credentials file not found: {self._path}")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            raise CredentialsError(f"could not read {self._path}: {e}") from e
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CredentialsError(f"invalid JSON in {self._path}: {e}") from e
        return OAuthCredentials.from_claude_payload(payload)

    def _persist_after_refresh(self, creds: OAuthCredentials) -> None:
        try:
            _atomic_write_creds(self._path, creds)
        except OSError as e:
            _LOG.warning("Could not persist refreshed creds to %s: %s", self._path, e)


class _KeychainCredentialProvider(CredentialProvider):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def _load(self) -> OAuthCredentials:
        if platform.system() != "Darwin":
            raise CredentialsError("Keychain accounts only supported on macOS")
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", self._service, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise CredentialsError(f"keychain read failed for {self._service}: {e}") from e
        if r.returncode != 0 or not r.stdout.strip():
            raise CredentialsError(
                f"no keychain entry for service {self._service!r}: {r.stderr[:200]}"
            )
        try:
            payload = json.loads(r.stdout.strip())
        except json.JSONDecodeError as e:
            raise CredentialsError(f"invalid JSON in keychain {self._service}: {e}") from e
        return OAuthCredentials.from_claude_payload(payload)

    def _persist_after_refresh(self, creds: OAuthCredentials) -> None:
        try:
            _atomic_write_creds(_service_cache_path(self._service), creds)
        except OSError as e:
            _LOG.warning("Could not persist refreshed creds to cache: %s", e)
        wrote_back = (
            _keychain_write_back(self._service, creds)
            if _keychain_write_back_enabled()
            else False
        )
        _warn_refresh_rotation(f"keychain {self._service!r}", wrote_back)


@dataclass
class _AccountSlot:
    provider: CredentialProvider
    label: str
    exhausted_until: float = 0.0
    invalid: bool = False
    last_token: Optional[str] = None

    def is_available(self, now: Optional[float] = None) -> bool:
        if self.invalid:
            return False
        now = now if now is not None else time.time()
        return now >= self.exhausted_until


class MultiAccountCredentialProvider:
    def __init__(
        self,
        slots: list[_AccountSlot],
        state_path: Optional[Path] = None,
    ) -> None:
        if not slots:
            raise CredentialsError("MultiAccountCredentialProvider needs >= 1 slot")
        self._slots = slots
        self._lock = threading.Lock()
        self._last_used_index = 0
        self._cursor = 0
        self.last_cap_reset_at: Optional[float] = None
        if state_path is None:
            env_path = os.environ.get("ZORO_CC_POOL_STATE_PATH")
            state_path = Path(env_path) if env_path else None
        self._state_path = state_path
        self._last_heal_at: float = 0.0
        self._heal_cooldown_s: float = 30.0
        if self._state_path is not None:
            self._load_state()

    def _load_state(self) -> None:
        if self._state_path is None:
            return
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        with self._lock:
            for slot in self._slots:
                st = data.get(slot.label)
                if not isinstance(st, dict):
                    continue
                eu = st.get("exhausted_until")
                if isinstance(eu, (int, float)) and eu > now:
                    slot.exhausted_until = max(slot.exhausted_until, float(eu))
            cur = data.get("_cursor")
            if isinstance(cur, int) and 0 <= cur < len(self._slots):
                self._cursor = cur

    def _persist_state_locked(self) -> None:
        if self._state_path is None:
            return
        data: dict = {
            slot.label: {
                "exhausted_until": slot.exhausted_until,
                "invalid": slot.invalid,
            }
            for slot in self._slots
        }
        data["_cursor"] = self._cursor
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._state_path)
        except OSError as e:
            _LOG.debug("could not persist account pool state: %s", e)

    def preflight(self) -> None:
        errors = []
        any_ok = False
        for slot in self._slots:
            try:
                slot.provider.preflight()
                any_ok = True
            except CredentialsError as e:
                errors.append(f"{slot.label}: {e}")
        if not any_ok:
            raise CredentialsError(
                "no usable accounts in pool:\n  " + "\n  ".join(errors)
            )

    def _try_heal_slots_locked(self, now: float) -> bool:
        if now - self._last_heal_at < self._heal_cooldown_s:
            return False
        self._last_heal_at = now
        healed_any = False
        for slot in self._slots:
            if slot.invalid:
                slot.invalid = False
                healed_any = True
                _LOG.info(
                    "heal: cleared invalid flag on slot %s (retry via refresh path)",
                    slot.label,
                )
            if slot.exhausted_until > now + self._heal_cooldown_s * 2:
                slot.exhausted_until = 0.0
                healed_any = True
                _LOG.info(
                    "heal: cleared stale exhausted_until on slot %s",
                    slot.label,
                )
            try:
                slot.provider.force_reload()
            except Exception as e:
                _LOG.debug("heal: force_reload failed on slot %s: %s", slot.label, e)
        if healed_any:
            self._persist_state_locked()
        return healed_any

    def _select_slot_locked(self) -> tuple[_AccountSlot, int]:
        now = time.time()
        n = len(self._slots)
        for step in range(n):
            idx = (self._cursor + step) % n
            slot = self._slots[idx]
            if slot.is_available(now):
                self._cursor = (idx + 1) % n
                self._persist_state_locked()
                return slot, idx
        if self._try_heal_slots_locked(now):
            for step in range(n):
                idx = (self._cursor + step) % n
                slot = self._slots[idx]
                if slot.is_available(now):
                    self._cursor = (idx + 1) % n
                    self._persist_state_locked()
                    return slot, idx
        soonest = min(
            (s.exhausted_until for s in self._slots if not s.invalid),
            default=0.0,
        )
        delta = max(0.0, soonest - now)
        raise CredentialsError(
            f"all {n} accounts exhausted; soonest reset in {delta:.0f}s"
        )

    def get_access_token(self) -> str:
        with self._lock:
            slot, idx = self._select_slot_locked()
            self._last_used_index = idx
        try:
            token = slot.provider.get_access_token()
        except CredentialsError:
            with self._lock:
                slot.invalid = True
            return self.get_access_token()
        with self._lock:
            slot.last_token = token
        return token

    def refresh(self) -> str:
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                slot = self._slots[self._last_used_index]
            else:
                slot = self._slots[0]
        return slot.provider.refresh()

    def force_reload(self) -> None:
        with self._lock:
            for slot in self._slots:
                slot.provider.force_reload()

    def mark_exhausted(self, retry_after_s: float) -> None:
        until = time.time() + max(0.0, retry_after_s)
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                slot = self._slots[self._last_used_index]
                slot.exhausted_until = max(slot.exhausted_until, until)
                self._persist_state_locked()

    def mark_invalid(self) -> None:
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                slot = self._slots[self._last_used_index]
                slot.invalid = True
                self._persist_state_locked()

    def next_reset_at(self) -> Optional[float]:
        with self._lock:
            now = time.time()
            if any(s.is_available(now) for s in self._slots):
                return None
            future = [s.exhausted_until for s in self._slots if not s.invalid]
            return min(future) if future else None

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "label": s.label,
                    "token_prefix": s.provider.token_prefix(),
                    "invalid": s.invalid,
                    "exhausted_until": s.exhausted_until,
                    "available": s.is_available(),
                }
                for s in self._slots
            ]

    def token_prefix(self) -> Optional[str]:
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                return self._slots[self._last_used_index].provider.token_prefix()
        return None

    def subscription_type(self) -> Optional[str]:
        with self._lock:
            if 0 <= self._last_used_index < len(self._slots):
                return self._slots[self._last_used_index].provider.subscription_type()
        return None


def load_account_pool(spec: str) -> Optional[MultiAccountCredentialProvider]:
    if not spec:
        return None
    slots: list[_AccountSlot] = []
    for raw in spec.split(":"):
        entry = raw.strip()
        if not entry:
            continue
        if entry == "default":
            slots.append(_AccountSlot(
                provider=CredentialProvider(),
                label="default",
            ))
            continue
        if entry.startswith("keychain:"):
            service = entry[len("keychain:"):]
            slots.append(_AccountSlot(
                provider=_KeychainCredentialProvider(service),
                label=f"keychain:{service}",
            ))
            continue
        slots.append(_AccountSlot(
            provider=_FileCredentialProvider(Path(entry)),
            label=f"file:{entry}",
        ))
    if not slots:
        return None
    env_path = os.environ.get("ZORO_CC_POOL_STATE_PATH")
    state_path = Path(env_path) if env_path else (
        _CACHE_DIR / "pool_state.json"
    )
    return MultiAccountCredentialProvider(slots, state_path=state_path)


def resolve_provider() -> CredentialProvider | MultiAccountCredentialProvider:
    pool_spec = os.environ.get("ZORO_CC_ACCOUNT_POOL", "").strip()
    if pool_spec:
        pool = load_account_pool(pool_spec)
        if pool is not None:
            _LOG.info("Using multi-account pool with %d slots", len(pool.snapshot()))
            return pool
    return CredentialProvider()
