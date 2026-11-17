"""Filesystem locations and server configuration."""

from __future__ import annotations

import os
import platform
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BUNDLE_ID = "com.bridle.app"
DEFAULT_PORT = 49813


def support_dir() -> Path:
    """Per-user data directory (mirrors the macOS app's Application Support folder)."""
    override = os.environ.get("BRIDLE_HOME")
    if override:
        return Path(override).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / BUNDLE_ID
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "bridle"


def default_models_dir() -> Path:
    return support_dir() / "models"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    models_dir: Path = field(default_factory=default_models_dir)
    # settings.json, api_keys.json, presets.json, requests.sqlite3, benchmarks.json, tls/, crash/
    data_dir: Path = field(default_factory=support_dir)
    # Bearer key required on /v1/* when set. Empty means open (fine for loopback-only).
    api_key: str = ""
    # Token the desktop app uses for /control/*. Control calls must also come from loopback.
    control_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Load a downloaded model automatically when a request names it.
    auto_load: bool = True
    # Model to load at startup ("owner/name"), optional.
    preload: str = ""
    # Directory of Aria's bundled helper model (empty = Aria unavailable in this build).
    aria_model_path: str = ""
    # Directory of the bundled Onyx Whisper speech model (empty = not in this build).
    onyx_model_path: str = ""
    # Extra browser origins allowed to call the API (e.g. "http://localhost:3000"). Requests that
    # carry any other Origin header are refused, so web pages can't drive the local API.
    allowed_origins: tuple[str, ...] = ()

    def ensure_dirs(self) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
