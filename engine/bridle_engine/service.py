"""Engine container and the request pipeline shared by the OpenAI and Ollama APIs."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from . import __version__
from .catalog import ModelStore
from .config import ServerConfig
from .discovery import Discovery
from .domain import ChatMessage, ChatRequest, ParseError, RepoId, SamplingParams
from .downloads import DownloadManager
from .history import RequestHistory, RequestRecord
from .jobs import BenchmarkStore, CompareManager
from .keys import ApiKey, KeyStore
from .logbuffer import LogBuffer
from .presets import PRESET_PREFIX, Preset, PresetStore
from .runtime import GenEvent, GenJob, InferenceRuntime, ModelHandle
from .settings import SettingsStore
from .system_info import MachineInfo, StatsSampler, collect_machine_info, lan_address

log = logging.getLogger("bridle.service")


@dataclass
class ClientInfo:
    name: str                 # key name, "local", or "anonymous"
    remote: str
    key: ApiKey | None = None
    reserved_tokens: int = 0  # daily-limit hold, settled when the request finishes
    local: bool = True

    @property
    def can_load_model(self) -> bool:
        """Local callers and the shared key may load; named remote keys only if granted."""
        return self.local or self.key is None or self.key.allow_model_load


@dataclass
class Engine:
    config: ServerConfig
    settings_store: SettingsStore
    store: ModelStore
    runtime: InferenceRuntime
    downloads: DownloadManager
    machine: MachineInfo
    sampler: StatsSampler
    logs: LogBuffer
    keys: KeyStore
    history: RequestHistory
    presets: PresetStore
    benchmarks: BenchmarkStore
    compare: CompareManager
    bound_port: int = 0
    tls_port: int | None = None
    tls_fingerprint: str = ""
    discovery: Discovery | None = None
    started_at: float = field(default_factory=time.time)
    mesh: Any = None   # MeshSession (this Mac's rank), attached by api.create_app

    @property
    def settings(self):
        return self.settings_store.current

    @staticmethod
    def build(config: ServerConfig, logs: LogBuffer, downloads: DownloadManager | None = None) -> "Engine":
        config.ensure_dirs()
        home = config.data_dir
        settings_store = SettingsStore(home / "settings.json")
        store = ModelStore(config.models_dir)
        runtime = InferenceRuntime(store, settings_store.current,
                                   aria_model_path=config.aria_model_path,
                                   onyx_model_path=config.onyx_model_path,
                                   prompt_cache_dir=home / "prompt-cache")
        return Engine(
            config=config,
            settings_store=settings_store,
            store=store,
            runtime=runtime,
            downloads=downloads or DownloadManager(store),
            machine=collect_machine_info(config.models_dir),
            sampler=StatsSampler(config.models_dir),
            logs=logs,
            keys=KeyStore(home / "api_keys.json"),
            history=RequestHistory(home / "requests.sqlite3"),
            presets=PresetStore(home / "presets.json"),
            benchmarks=BenchmarkStore(home / "benchmarks.json"),
            compare=CompareManager(runtime),
            bound_port=config.port,
        )

    @property
    def home(self):
        return self.config.data_dir

    @property
    def shared_on_network(self) -> bool:
        return self.config.host in ("0.0.0.0", "::")

    def endpoint(self) -> str:
        if self.shared_on_network and self.tls_port:
            return self.https_endpoint()
        host = self.config.host
        if self.shared_on_network:
            host = lan_address() or "127.0.0.1"
        return f"http://{host}:{self.bound_port}"

    def https_endpoint(self) -> str | None:
        if not self.tls_port:
            return None
        host = (lan_address() or "127.0.0.1") if self.shared_on_network else "127.0.0.1"
        return f"https://{host}:{self.tls_port}"

    def bonjour_props(self) -> dict[str, str]:
        lm = self.runtime.loaded
        return {"version": __version__, "chip": self.machine.chip,
                "memory": str(self.machine.memory_total_bytes), "tb": str(self.machine.thunderbolt_generation or ""),
                "model": lm.repo if lm else "", "path": "/v1", "tls": str(self.tls_port or "")}

    def start_discovery(self) -> None:
        if not self.settings.bonjour:
            return
        self.discovery = Discovery(self.machine.hostname, self.bound_port, self.bonjour_props())
        self.discovery.start(lan_address() if self.shared_on_network else None)

    def features(self) -> dict[str, bool]:
        def has(mod: str) -> bool:
            import importlib.util
            return importlib.util.find_spec(mod) is not None

        return {"vision": has("mlx_vlm"), "embeddings": has("mlx_embeddings"), "speech": has("mlx_audio"),
                "structured_output": has("llguidance"), "bonjour": has("zeroconf"), "tls": has("cryptography")}


# ---- presets ----------------------------------------------------------------------------------------
def apply_preset(req: ChatRequest, presets: PresetStore) -> tuple[ChatRequest, Preset | None]:
    model = req.model
    if not model:
        return req, None
    name = model[len(PRESET_PREFIX):] if model.startswith(PRESET_PREFIX) else None
    if name is None and "/" not in model:
        name = model if presets.get(model) else None
    if name is None:
        return req, None
    preset = presets.get(name)
    if preset is None:
        raise ParseError(f"no preset named {name!r}", "model")
    messages = req.messages
    if preset.system_prompt and not any(m.role == "system" for m in messages):
        messages = (ChatMessage("system", preset.system_prompt),) + messages
    p = req.params
    params = SamplingParams(
        max_tokens=p.max_tokens if "max_tokens" in p.explicit or preset.max_tokens is None else preset.max_tokens,
        temperature=p.temperature if "temperature" in p.explicit or preset.temperature is None else preset.temperature,
        top_p=p.top_p if "top_p" in p.explicit or preset.top_p is None else preset.top_p,
        stop=p.stop, seed=p.seed, explicit=p.explicit)
    return req.with_changes(model=preset.model or None, messages=messages, params=params), preset


# ---- generation bridge ---------------------------------------------------------------------------------
async def stream_job(rt: InferenceRuntime, lm: ModelHandle, prompt: str, params: SamplingParams,
                     **job_kw: Any) -> AsyncIterator[GenEvent]:
    """Runs a generation on the MLX thread and yields its events on the event loop. Closing the
    iterator (e.g. client disconnect) cancels the generation."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def post(item: object) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:  # loop closed
            cancel.set()

    rt.submit_generation(GenJob(lm, prompt, params, cancel, post, **job_kw))
    try:
        while True:
            item = await q.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        cancel.set()


class Recorder:
    """Accumulates one request's outcome for the history log and key accounting."""

    def __init__(self, engine: Engine, endpoint: str, client: ClientInfo, model: str, prompt_text: str):
        self.engine = engine
        self.client = client
        self.started = time.perf_counter()
        self.rec = RequestRecord(endpoint=endpoint, status=200, model=model, client=client.name,
                                 remote=client.remote, prompt=prompt_text)
        self.parts: list[str] = []
        self._done = False

    def observe(self, ev: GenEvent) -> None:
        if ev.text:
            self.parts.append(ev.text)
        if ev.finish_reason is not None:
            self.rec.prompt_tokens = ev.prompt_tokens
            self.rec.cached_tokens = ev.cached_tokens
            self.rec.completion_tokens = ev.completion_tokens
            self.rec.tps = ev.generation_tps
            self.rec.finish_reason = ev.finish_reason
            if ev.tool_calls:
                self.parts.append("\n[tool_calls] " + ", ".join(c.name for c in ev.tool_calls))

    def fail(self, status: int, error: str) -> None:
        self.rec.status, self.rec.error = status, error[:500]
        self.finish()

    def finish(self) -> None:
        if self._done:
            return
        self._done = True
        if self.rec.status == 200 and not self.rec.finish_reason and self.rec.endpoint not in (
                "/v1/embeddings", "/v1/audio/transcriptions"):
            self.rec.status, self.rec.finish_reason = 499, "cancelled"   # client went away mid-generation
        self.rec.duration_ms = int((time.perf_counter() - self.started) * 1000)
        self.rec.response = "".join(self.parts)
        try:
            self.engine.history.add(self.rec, self.engine.settings.log_content)
        except Exception as exc:  # noqa: BLE001 - history must never break a request
            log.warning("could not record request: %s", exc)
        if self.client.key is not None:
            actual = self.rec.prompt_tokens + self.rec.completion_tokens
            if self.client.reserved_tokens:
                self.engine.keys.settle_tokens(self.client.key.id, self.client.reserved_tokens, actual)
                self.client.reserved_tokens = 0
            else:
                self.engine.keys.record_tokens(self.client.key.id, actual)


def prompt_summary(messages: tuple[ChatMessage, ...]) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in messages)[-20_000:]


async def recorded(events: AsyncIterator[GenEvent], recorder: Recorder) -> AsyncIterator[GenEvent]:
    try:
        async for ev in events:
            recorder.observe(ev)
            yield ev
    except Exception as exc:
        recorder.fail(500, str(exc))
        raise
    finally:
        recorder.finish()


async def resolve_model(engine: Engine, model: str | None, allow_load: bool = True) -> ModelHandle:
    if engine.mesh is not None and engine.mesh.active:
        # While a mesh holds this Mac's memory, never auto-load a second model next to it.
        allow_load = False
    if not allow_load:
        loaded = engine.runtime.loaded
        if loaded is not None and (model is None or model in (loaded.repo, loaded.repo.split("/", 1)[1])):
            return loaded
        fast = engine.runtime.fast_loaded
        if fast is not None and model in (fast.repo, fast.repo.split("/", 1)[1]):
            return fast
        from .runtime import ModelUnavailable
        raise ModelUnavailable(f"model {model!r} is not loaded; this API key may not load models", 403)
    return await asyncio.to_thread(engine.runtime.resolve, model, engine.config.auto_load)


def repo_or_none(raw: str | None) -> RepoId | None:
    return RepoId.parse(raw) if raw else None
