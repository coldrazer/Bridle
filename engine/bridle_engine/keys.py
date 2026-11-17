"""Named API keys with per-key rate and daily token limits.

Only a salted SHA-256 hash of each key is stored (legacy unsalted hashes
upgrade to salted on first use); the plaintext is shown once at creation."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .domain import ParseError

KEY_PREFIX = "brdl_"
log = logging.getLogger("bridle.keys")


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class ApiKey:
    id: str
    name: str
    hint: str                       # "brdl_ab12…" for display
    hash: str
    created_at: float
    revoked: bool = False
    rpm_limit: int | None = None    # requests per minute
    daily_token_limit: int | None = None
    requests_total: int = 0
    tokens_total: int = 0
    tokens_today: int = 0
    usage_day: str = ""
    last_used_at: float | None = None
    allow_model_load: bool = False   # remote keys may not swap the resident model unless granted
    allow_mesh: bool = False         # may drive this Mac as a mesh member (/mesh/*): another Mac's coordinator

    def public(self) -> dict:
        d = asdict(self)
        d.pop("hash")
        return d


def _hash(plaintext: str) -> str:
    """Legacy unsalted digest; kept only to verify and upgrade pre-0.2 keys."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _salted_hash(plaintext: str, salt: str) -> str:
    return hashlib.sha256((salt + plaintext).encode()).hexdigest()


def _new_digest(plaintext: str) -> str:
    """Versioned salted digest for a new key: s1$<salt>$<sha256(salt + key)>."""
    salt = secrets.token_hex(16)
    return f"s1${salt}${_salted_hash(plaintext, salt)}"


def _seconds_until_daily_reset(now: float) -> int:
    """Seconds until the next LOCAL midnight, matching the date.today() reset."""
    tomorrow = date.today() + timedelta(days=1)
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day).timestamp()
    return max(1, int(midnight - now))


def parse_key_create(body: object) -> tuple[str, int | None, int | None, bool]:
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ParseError("name must be a non-empty string (max 80 chars)", "name")
    limits = []
    for k in ("rpm_limit", "daily_token_limit"):
        v = body.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            raise ParseError(f"{k} must be a positive integer or null", k)
        limits.append(v)
    allow = body.get("allow_model_load", False)
    if not isinstance(allow, bool):
        raise ParseError("allow_model_load must be a boolean", "allow_model_load")
    return name.strip(), limits[0], limits[1], allow


class KeyStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._keys: dict[str, ApiKey] = {}
        self._windows: dict[str, deque[float]] = {}
        self._dirty = False
        self.load_error = ""
        if path.is_file():
            try:
                for raw in json.loads(path.read_text()):
                    k = ApiKey(**raw)
                    self._keys[k.id] = k
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"
                self._keys = {}
                backup = path.with_name(path.name + f".corrupt-{int(time.time())}")
                try:
                    path.rename(backup)
                    log.error("api key store %s is corrupt (%s); preserved as %s and starting empty",
                              path, self.load_error, backup)
                except OSError:
                    log.error("api key store %s is corrupt (%s); starting empty", path, self.load_error)

    # ---- management -----------------------------------------------------------------------
    def create(self, name: str, rpm_limit: int | None = None, daily_token_limit: int | None = None,
               allow_model_load: bool = False, allow_mesh: bool = False) -> tuple[ApiKey, str]:
        plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
        key = ApiKey(id=secrets.token_hex(6), name=name, hint=plaintext[:10] + "…", hash=_new_digest(plaintext),
                     created_at=time.time(), rpm_limit=rpm_limit, daily_token_limit=daily_token_limit,
                     allow_model_load=allow_model_load, allow_mesh=allow_mesh)
        with self._lock:
            self._keys[key.id] = key
            self._save_locked()
        return key, plaintext

    def list(self) -> list[ApiKey]:
        with self._lock:
            return sorted(self._keys.values(), key=lambda k: k.created_at)

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            key = self._keys.get(key_id)
            if not key:
                return False
            key.revoked = True
            self._save_locked()
            return True

    def delete(self, key_id: str) -> bool:
        with self._lock:
            if self._keys.pop(key_id, None) is None:
                return False
            self._save_locked()
            return True

    def has_active_keys(self) -> bool:
        with self._lock:
            return any(not k.revoked for k in self._keys.values())

    # ---- request path -----------------------------------------------------------------------
    def authenticate(self, plaintext: str) -> ApiKey | None:
        if not plaintext:
            return None
        with self._lock:
            for key in self._keys.values():
                if key.revoked:
                    continue
                if key.hash.startswith("s1$"):
                    _, salt, _ = key.hash.split("$", 2)
                    candidate = f"s1${salt}${_salted_hash(plaintext, salt)}"
                    if hmac.compare_digest(key.hash, candidate):
                        return key
                elif hmac.compare_digest(key.hash, _hash(plaintext)):
                    # Legacy unsalted digest: upgrade in place to a salted one.
                    key.hash = _new_digest(plaintext)
                    self._dirty = True
                    return key
        return None

    def admit(self, key: ApiKey) -> None:
        """Count a request against the key's limits; raises RateLimited when over."""
        now = time.time()
        today = date.today().isoformat()
        with self._lock:
            if key.usage_day != today:
                key.usage_day, key.tokens_today = today, 0
            if key.daily_token_limit is not None and key.tokens_today >= key.daily_token_limit:
                tomorrow = _seconds_until_daily_reset(now)
                raise RateLimited(f"daily token limit reached for key '{key.name}'", tomorrow)
            if key.rpm_limit is not None:
                window = self._windows.setdefault(key.id, deque())
                while window and window[0] <= now - 60:
                    window.popleft()
                if len(window) >= key.rpm_limit:
                    raise RateLimited(f"rate limit of {key.rpm_limit} requests/minute reached for key '{key.name}'",
                                      max(1, int(60 - (now - window[0]))))
                window.append(now)
            key.requests_total += 1
            key.last_used_at = now
            self._dirty = True

    def reserve(self, key: ApiKey, tokens: int) -> int:
        """Hold up to `tokens` against today's limit until the request settles.

        The hold is capped at the remaining allowance so a key whose daily
        limit is smaller than one request's max_tokens can still make
        requests; actual usage settles afterward and may exceed the limit
        by that one in-flight request, never by a concurrent burst.
        """
        if tokens <= 0 or key.daily_token_limit is None:
            return 0
        today = date.today().isoformat()
        with self._lock:
            if key.usage_day != today:
                key.usage_day, key.tokens_today = today, 0
            remaining = key.daily_token_limit - key.tokens_today
            if remaining <= 0:
                raise RateLimited(f"daily token limit reached for key '{key.name}'",
                                  _seconds_until_daily_reset(time.time()))
            held = min(tokens, remaining)
            key.tokens_today += held
            self._dirty = True
        return held

    def settle_tokens(self, key_id: str, reserved: int, actual: int) -> None:
        today = date.today().isoformat()
        with self._lock:
            key = self._keys.get(key_id)
            if key:
                key.tokens_total += actual
                if key.usage_day != today:
                    # The hold belonged to yesterday's counter; today's starts at actual.
                    key.usage_day, key.tokens_today = today, actual
                else:
                    key.tokens_today += actual - reserved
                self._dirty = True

    def record_tokens(self, key_id: str, tokens: int) -> None:
        with self._lock:
            key = self._keys.get(key_id)
            if key:
                key.tokens_total += tokens
                key.tokens_today += tokens
                self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file 0600 from the first byte (no chmod-after-write window).
        fd, name = tempfile.mkstemp(prefix=".api_keys-", dir=self.path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps([asdict(k) for k in self._keys.values()], indent=2))
            tmp.chmod(0o600)
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)
        self._dirty = False
