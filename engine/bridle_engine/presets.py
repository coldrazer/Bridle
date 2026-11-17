"""Named generation presets (model + system prompt + sampling defaults).

A request selects one with model="preset:<name>". Values the client sends explicitly win."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .domain import MAX_TOKENS_LIMIT, ParseError, RepoId

PRESET_PREFIX = "preset:"
log = logging.getLogger("bridle.presets")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,47}$")


@dataclass
class Preset:
    name: str
    model: str = ""             # empty = whatever model is loaded
    system_prompt: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None

    def to_json(self) -> dict:
        return asdict(self)


def parse_preset(body: Any) -> Preset:
    if not isinstance(body, dict):
        raise ParseError("preset must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ParseError("name must be 1-48 letters, digits, spaces, '-', '_' or '.'", "name")
    model = body.get("model") or ""
    if model:
        model = str(RepoId.parse(model))
    system = body.get("system_prompt") or ""
    if not isinstance(system, str) or len(system) > 20_000:
        raise ParseError("system_prompt must be a string (max 20000 chars)", "system_prompt")

    def num(key: str, lo: float, hi: float) -> float | None:
        v = body.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ParseError(f"{key} must be a number in [{lo}, {hi}]", key)
        return float(v)

    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
                                   or not 1 <= max_tokens <= MAX_TOKENS_LIMIT):
        raise ParseError(f"max_tokens must be an integer in [1, {MAX_TOKENS_LIMIT}]", "max_tokens")
    return Preset(name, model, system, num("temperature", 0, 2), num("top_p", 0, 1), max_tokens)


class PresetStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._presets: dict[str, Preset] = {}
        self.load_error = ""
        if path.is_file():
            try:
                for raw in json.loads(path.read_text()):
                    p = parse_preset(raw)
                    self._presets[p.name.lower()] = p
            except (OSError, json.JSONDecodeError, ParseError) as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"
                self._presets = {}
                backup = path.with_name(path.name + f".corrupt-{int(time.time())}")
                try:
                    path.rename(backup)
                    log.error("presets %s are corrupt (%s); preserved as %s and starting empty",
                              path, self.load_error, backup)
                except OSError:
                    log.error("presets %s are corrupt (%s); starting empty", path, self.load_error)

    def list(self) -> list[Preset]:
        with self._lock:
            return sorted(self._presets.values(), key=lambda p: p.name.lower())

    def get(self, name: str) -> Preset | None:
        with self._lock:
            return self._presets.get(name.lower())

    def put(self, preset: Preset) -> None:
        with self._lock:
            self._presets[preset.name.lower()] = preset
            self._save_locked()

    def delete(self, name: str) -> bool:
        with self._lock:
            if self._presets.pop(name.lower(), None) is None:
                return False
            self._save_locked()
            return True

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".presets-", dir=self.path.parent)  # 0600 from creation
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps([p.to_json() for p in self._presets.values()], indent=2))
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)
