"""Persistent engine settings (support_dir/settings.json), editable at runtime via /control/settings."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .domain import ParseError

log = logging.getLogger("bridle.settings")

# Settings that only take effect after an engine restart.
RESTART_REQUIRED = frozenset({"tls", "tls_port", "bonjour", "ollama_api"})


@dataclass
class EngineSettings:
    idle_unload_minutes: int = 30      # free model memory after this much idle time; 0 = never
    batching: bool = True              # serve concurrent requests together (continuous batching)
    max_batch_size: int = 8
    prompt_cache: bool = True          # reuse KV cache for shared prompt prefixes
    memory_guard: bool = True          # refuse loads that won't fit in free memory
    num_draft_tokens: int = 3          # speculative decoding look-ahead
    log_content: bool = False          # store prompt/response text in request history
    local_requires_key: bool = False   # if API keys exist, also require them from this Mac
    bonjour: bool = True               # advertise on the LAN (only when sharing)
    tls: bool = False                  # also serve HTTPS with a self-signed certificate
    tls_port: int = 0                  # 0 = HTTP port + 1
    ollama_api: bool = True            # serve the Ollama-compatible /api/* routes
    prompt_cache_disk: bool = True     # persist prompt KV caches to disk across restarts
    prompt_cache_disk_mb: int = 2048   # cap for the on-disk prompt cache
    fast_model: str = ""               # optional second resident model (owner/name) for small/fast calls
    max_concurrent_requests: int = 16  # inference slots admitted at once
    queue_wait_seconds: int = 30       # how long excess inference requests queue before 429
    allow_remote_images: bool = True   # vision may fetch http(s) image URLs (public addresses only)
    storage_quota_gb: int = 0          # model-store soft quota in GB; 0 = no quota

    def to_json(self) -> dict:
        return asdict(self)


_LIMITS: dict[str, tuple[int, int]] = {
    "idle_unload_minutes": (0, 24 * 60),
    "max_batch_size": (1, 64),
    "num_draft_tokens": (1, 16),
    "tls_port": (0, 65535),
    "prompt_cache_disk_mb": (64, 16384),
    "max_concurrent_requests": (1, 64),
    "queue_wait_seconds": (0, 600),
    "storage_quota_gb": (0, 100000),
}


def parse_settings_patch(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ParseError("settings patch must be a JSON object")
    known = {f.name: f.type for f in fields(EngineSettings)}
    patch: dict[str, Any] = {}
    for key, value in body.items():
        if key not in known:
            raise ParseError(f"unknown setting {key!r}", key)
        if known[key] in ("bool", bool):
            if not isinstance(value, bool):
                raise ParseError(f"{key} must be a boolean", key)
        elif known[key] in ("str", str):
            if not isinstance(value, str) or len(value) > 200:
                raise ParseError(f"{key} must be a string (max 200 chars)", key)
            if key == "fast_model" and value:
                from .domain import RepoId
                RepoId.parse(value)  # must be owner/name (raises ParseError)
        else:
            lo, hi = _LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ParseError(f"{key} must be an integer in [{lo}, {hi}]", key)
        patch[key] = value
    return patch


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.current = EngineSettings()
        self.load_error = ""
        if path.is_file():
            try:
                saved = json.loads(path.read_text())
                patch = parse_settings_patch({k: v for k, v in saved.items()
                                              if k in {f.name for f in fields(EngineSettings)}})
                for k, v in patch.items():
                    setattr(self.current, k, v)
            except (OSError, json.JSONDecodeError, ParseError) as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"
                backup = path.with_name(path.name + f".corrupt-{int(time.time())}")
                try:
                    path.rename(backup)
                    log.error("settings %s are corrupt (%s); preserved as %s and using defaults",
                              path, self.load_error, backup)
                except OSError:
                    log.error("settings %s are corrupt (%s); using defaults", path, self.load_error)

    def update(self, patch: dict[str, Any]) -> tuple[EngineSettings, list[str]]:
        with self._lock:
            for k, v in patch.items():
                setattr(self.current, k, v)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)  # 0600 from creation
            tmp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(self.current.to_json(), indent=2))
                tmp.replace(self.path)
            finally:
                tmp.unlink(missing_ok=True)
        return self.current, sorted(k for k in patch if k in RESTART_REQUIRED)
