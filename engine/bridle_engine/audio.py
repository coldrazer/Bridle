"""Speech to text (Whisper, Parakeet, …) via the optional mlx-audio package. No ffmpeg needed:
mlx-audio decodes wav/mp3/flac/ogg/m4a itself."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from .runtime_errors import FeatureUnavailable


def load_speech_model(path: Path) -> tuple[Any, None]:
    try:
        from mlx_audio.stt.utils import load
    except ImportError as exc:
        raise FeatureUnavailable("speech support isn't installed; run `scripts/bridle extras speech` "
                                 "(installs mlx-audio)") from exc
    return load(str(path)), None


def transcribe_file(model: Any, audio_path: str, language: str | None = None, prompt: str | None = None,
                    temperature: float | None = None) -> dict:
    accepted = inspect.signature(model.generate).parameters
    kwargs: dict[str, Any] = {}
    if language and "language" in accepted:
        kwargs["language"] = language
    if prompt and "initial_prompt" in accepted:
        kwargs["initial_prompt"] = prompt
    if temperature is not None and "temperature" in accepted:
        kwargs["temperature"] = temperature
    out = model.generate(audio_path, **kwargs)
    segments = []
    for s in getattr(out, "segments", None) or []:
        if isinstance(s, dict):
            segments.append({"start": float(s.get("start", 0.0)), "end": float(s.get("end", 0.0)),
                             "text": str(s.get("text", "")).strip()})
    return {"text": str(getattr(out, "text", "")).strip(), "language": getattr(out, "language", None) or language,
            "segments": segments, "duration": segments[-1]["end"] if segments else None}


def _ts(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(result: dict) -> str:
    return "\n".join(f"{i}\n{_ts(s['start'], ',')} --> {_ts(s['end'], ',')}\n{s['text']}\n"
                     for i, s in enumerate(result["segments"], 1))


def to_vtt(result: dict) -> str:
    return "WEBVTT\n\n" + "\n".join(f"{_ts(s['start'], '.')} --> {_ts(s['end'], '.')}\n{s['text']}\n"
                                    for s in result["segments"])
