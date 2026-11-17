"""MLX model lifecycle, scheduling and token streaming.

Everything that touches MLX runs on one dedicated thread (MLX streams are thread-local). That
thread pulls work from a queue in arrival order:

* control tasks (load, unload, benchmark, embeddings, transcription) run one at a time;
* a lone generation runs on the single-sequence path, which supports prompt caching,
  speculative decoding, structured output and vision;
* when several plain text generations are waiting, they run together through mlx-lm's
  BatchGenerator, and requests that arrive mid-batch join it (continuous batching).
"""

from __future__ import annotations

import gc
import json
import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import psutil

from .catalog import GB, ModelStore, estimate_required_bytes, gpu_memory_budget
from .domain import ChatMessage, ParseError, RepoId, ResponseFormat, SamplingParams, ToolCall
from .settings import EngineSettings
from .structured import GrammarFactory, instruction
from .textproc import StopMatcher, TextRouter, parse_tool_calls, router_for

log = logging.getLogger("bridle.runtime")


class ModelUnavailable(Exception):
    """No usable model for the request (none loaded, not downloaded, or wrong kind)."""

    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


class ContextTooLong(Exception):
    pass


class MemoryGuardRefused(Exception):
    def __init__(self, message: str, check: dict):
        super().__init__(message)
        self.check = check


class TemplateError(Exception):
    pass


# ---- model handles ------------------------------------------------------------------------------
@dataclass
class DraftHandle:
    repo: str
    model: Any
    size_bytes: int


@dataclass
class ModelHandle:
    repo: str
    path: str
    kind: str                      # text | vision
    model: Any
    tokenizer: Any
    size_bytes: int
    context_length: int | None
    load_seconds: float
    processor: Any = None          # vision models
    config: dict = field(default_factory=dict)
    draft: DraftHandle | None = None
    loaded_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    revision: str = ""             # pinned Hub revision; parts disk prompt-cache keys
    _grammar: GrammarFactory | None = None

    def grammar(self) -> GrammarFactory:
        if self._grammar is None:
            self._grammar = GrammarFactory(self.tokenizer)
        return self._grammar

    def describe(self) -> dict:
        return {
            "repo": self.repo, "kind": self.kind, "size_bytes": self.size_bytes,
            "context_length": self.context_length, "load_seconds": self.load_seconds,
            "loaded_at": self.loaded_at, "last_used": self.last_used,
            "draft": None if self.draft is None else {"repo": self.draft.repo, "size_bytes": self.draft.size_bytes},
            "supports_tools": bool(getattr(self.tokenizer, "has_tool_calling", False)),
            "supports_reasoning": bool(getattr(self.tokenizer, "has_thinking", False)),
        }


@dataclass
class AuxHandle:
    """Embedding or speech model (kept separately from the chat model)."""
    repo: str
    kind: str                      # embedding | speech
    model: Any
    tokenizer: Any
    size_bytes: int
    last_used: float = field(default_factory=time.time)


@dataclass(frozen=True)
class GenEvent:
    """A content/reasoning delta, or (when finish_reason is set) the final event with usage."""
    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    prompt_tps: float = 0.0
    generation_tps: float = 0.0
    peak_memory_gb: float = 0.0
    draft_accepted: int = 0
    batched: bool = False


@dataclass
class GenJob:
    lm: ModelHandle
    prompt: str
    params: SamplingParams
    cancel: threading.Event
    emit: Callable[[object], None]
    tools: tuple[dict, ...] = ()
    response_format: ResponseFormat | None = None
    images: tuple[str, ...] = ()
    use_cache: bool = True
    allow_draft: bool = True
    ignore_eos: bool = False
    raw_text: bool = False         # /v1/completions: no reasoning/tool routing
    cache_partition: str = ""      # prompt-cache isolation per client (key id / local)

    def batchable(self, settings: EngineSettings) -> bool:
        return (settings.batching and self.lm.kind == "text" and self.lm.draft is None
                and self.response_format is None and not self.images and not self.ignore_eos)


@dataclass
class _Task:
    fn: Callable[[], Any]
    future: Future


@dataclass
class RuntimeMetrics:
    requests_total: int = 0
    requests_failed: int = 0
    tokens_generated: int = 0
    last_generation_tps: float = 0.0
    last_prompt_tps: float = 0.0
    active: int = 0
    batched_requests: int = 0
    cached_prompt_tokens: int = 0
    draft_accepted_tokens: int = 0


# ---- helpers -------------------------------------------------------------------------------------
def _mx():
    import mlx.core as mx
    return mx


def mlx_memory() -> dict[str, int]:
    mx = _mx()
    return {"active_bytes": int(mx.get_active_memory()), "peak_bytes": int(mx.get_peak_memory())}


def _context_length(path: Path) -> int | None:
    cfg = _read_config(path)
    text_cfg = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else {}
    for key in ("max_position_embeddings", "max_seq_len", "n_positions"):
        for source in (cfg, text_cfg):
            value = source.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return None


def _read_config(path: Path) -> dict:
    try:
        cfg = json.loads((path / "config.json").read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def model_kind(path: Path) -> str:
    """text | vision | embedding | speech, from config.json and file layout."""
    cfg = _read_config(path)
    mtype = str(cfg.get("model_type", "")).lower()
    archs = " ".join(cfg.get("architectures") or []).lower()
    if "whisper" in mtype or "whisper" in archs or mtype in ("parakeet", "parakeet_tdt", "canary", "moonshine"):
        return "speech"
    if (path / "modules.json").exists() or (path / "config_sentence_transformers.json").exists() \
            or mtype in ("bert", "xlm-roberta", "modernbert") or "embedding" in path.name.lower():
        return "embedding"
    if "vision_config" in cfg:
        return "vision"
    return "text"


def encode_prompt(tokenizer: Any, prompt: str) -> list[int]:
    bos = getattr(tokenizer, "bos_token", None)
    return list(tokenizer.encode(prompt, add_special_tokens=not (bos and prompt.startswith(bos))))


def _common_prefix(a: list[int], b: list[int]) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class PromptCacheStore:
    """Keeps the KV cache of the most recent conversation per (model, draft-mode) so a follow-up
    request that shares a prefix only processes the new tokens.

    With a disk directory, the latest entry per key is also persisted
    (mlx_lm save/load_prompt_cache) so long harness system prompts survive
    engine restarts and model reloads. Disk failures are always non-fatal."""

    def __init__(self, disk_dir: "Path | None" = None, settings: Any = None) -> None:
        self._entries: dict[tuple, tuple[list[int], list]] = {}
        self._disk_dir = disk_dir
        self._settings = settings
        self._last_disk_save: dict[tuple, float] = {}

    # ---- disk layer ------------------------------------------------------------------
    def _disk_enabled(self) -> bool:
        return self._disk_dir is not None and getattr(self._settings, "prompt_cache_disk", False)

    def _disk_path(self, key: tuple):
        import hashlib
        digest = hashlib.sha256(repr(key).encode()).hexdigest()[:32]
        return self._disk_dir / f"{digest}.safetensors"  # type: ignore[union-attr]

    def _disk_load(self, key: tuple) -> tuple[list[int], list] | None:
        if not self._disk_enabled():
            return None
        try:
            from mlx_lm.models.cache import load_prompt_cache

            path = self._disk_path(key)
            if not path.is_file():
                return None
            cache, metadata = load_prompt_cache(str(path), return_metadata=True)
            tokens = json.loads((metadata or {}).get("bridle_tokens") or "[]")
            if not isinstance(tokens, list) or not tokens:
                return None
            return tokens, cache
        except Exception:  # noqa: BLE001 - disk cache is an optimisation, never fatal
            log.debug("prompt cache disk load failed", exc_info=True)
            return None

    _DISK_MIN_TOKENS = 1024      # below this, re-prefill is cheaper than the write
    _DISK_MIN_INTERVAL = 60.0    # at most one disk write per key per minute

    def _disk_save(self, key: tuple, tokens: list[int], cache: list) -> None:
        if not self._disk_enabled() or len(tokens) < self._DISK_MIN_TOKENS:
            return
        now = time.monotonic()
        if now - self._last_disk_save.get(key, 0.0) < self._DISK_MIN_INTERVAL:
            return  # long conversations update disk at most once a minute
        self._last_disk_save[key] = now
        try:
            from mlx_lm.models.cache import save_prompt_cache

            self._disk_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            path = self._disk_path(key)
            save_prompt_cache(str(path), cache, {"bridle_tokens": json.dumps(tokens)})
            self._disk_prune(keep=path)
        except Exception:  # noqa: BLE001
            log.debug("prompt cache disk save failed", exc_info=True)

    def _disk_prune(self, keep: "Path | None" = None) -> None:
        cap = int(getattr(self._settings, "prompt_cache_disk_mb", 2048)) * 1024 * 1024
        files = sorted(self._disk_dir.glob("*.safetensors"),  # type: ignore[union-attr]
                       key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= cap:
                break
            if keep is not None and path == keep:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)

    def disk_usage(self) -> dict:
        if self._disk_dir is None or not self._disk_dir.is_dir():
            return {"files": 0, "bytes": 0}
        files = list(self._disk_dir.glob("*.safetensors"))
        return {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}

    # ---- RAM layer -------------------------------------------------------------------
    @staticmethod
    def _reuse(tokens: list[int], cache: list, ids: list[int]) -> tuple[list, int] | None:
        from mlx_lm.models.cache import can_trim_prompt_cache, trim_prompt_cache

        n = _common_prefix(tokens, ids)
        if n == len(ids):
            n -= 1  # always feed at least one token
        if n > 0:
            trim = len(tokens) - n
            if trim == 0:
                return cache, n
            if can_trim_prompt_cache(cache) and trim_prompt_cache(cache, trim) == trim:
                return cache, n
        return None

    def fetch(self, key: tuple, make: Callable[[], list], ids: list[int]) -> tuple[list, int]:
        entry = self._entries.pop(key, None)
        if entry is not None:
            reused = self._reuse(entry[0], entry[1], ids)
            if reused is not None:
                return reused
        disk = self._disk_load(key)
        if disk is not None:
            reused = self._reuse(disk[0], disk[1], ids)
            if reused is not None:
                log.info("prompt cache: reused %d tokens from disk", reused[1])
                return reused
        return make(), 0

    def store(self, key: tuple, all_tokens: list[int], cache: list, main_layers: int) -> None:
        from mlx_lm.models.cache import can_trim_prompt_cache, trim_prompt_cache

        main_off = getattr(cache[0], "offset", None) if cache else None
        if not isinstance(main_off, int):
            return
        n = main_off
        if len(cache) > main_layers:  # speculative: main + draft caches must agree
            draft_off = getattr(cache[main_layers], "offset", None)
            if not isinstance(draft_off, int):
                return
            n = min(main_off, draft_off)
            for part, off in ((cache[:main_layers], main_off), (cache[main_layers:], draft_off)):
                if off > n and not (can_trim_prompt_cache(part) and trim_prompt_cache(part, off - n) == off - n):
                    return
        if n > len(all_tokens) or n == 0:
            return
        self._entries[key] = (all_tokens[:n], cache)
        self._disk_save(key, all_tokens[:n], cache)

    def clear(self) -> None:
        self._entries.clear()


class OutputPipeline:
    """stop sequences -> reasoning/tool routing -> GenEvents, shared by single and batch paths."""

    def __init__(self, job: GenJob, tokenizer: Any):
        self.stop = StopMatcher(job.params.stop)
        self.router: TextRouter = (TextRouter(None, None) if job.raw_text
                                   else router_for(tokenizer, job.prompt, bool(job.tools)))
        self.tool_bodies: list[str] = []
        self.stopped = False

    def push(self, text: str) -> list[GenEvent]:
        out, hit = self.stop.feed(text)
        events = self._route(self.router.feed(out))
        if hit:
            self.stopped = True
            events += self._route(self.router.flush())
        return events

    def finish(self) -> list[GenEvent]:
        if self.stopped:
            return []
        return self._route(self.router.feed(self.stop.flush())) + self._route(self.router.flush())

    def _route(self, segments: list[tuple[str, str]]) -> list[GenEvent]:
        events = []
        for kind, text in segments:
            if kind == "content":
                events.append(GenEvent(text=text))
            elif kind == "reasoning":
                events.append(GenEvent(reasoning=text))
            else:
                self.tool_bodies.append(text)
        return events


# ---- the runtime ---------------------------------------------------------------------------------
ARIA_REPO = "mlx-community/Qwen3-0.6B-4bit"
ONYX_REPO = "wabibito/Onyx-Whisper-Tiny"


class InferenceRuntime:
    def __init__(self, store: ModelStore, settings: EngineSettings | None = None,
                 aria_model_path: str = "", onyx_model_path: str = "",
                 prompt_cache_dir: Path | None = None):
        self.store = store
        self.settings = settings or EngineSettings()
        self.aria_model_path = aria_model_path
        self.onyx_model_path = onyx_model_path
        self.metrics = RuntimeMetrics()
        self._q: queue.Queue = queue.Queue()
        self._deferred: deque = deque()
        self._lock = threading.Lock()
        self._loaded: ModelHandle | None = None
        self._aria: ModelHandle | None = None     # Aria's always-on helper slot
        self._fast: ModelHandle | None = None     # optional second resident (small/fast) model
        self._fast_error = ""
        self._onyx: AuxHandle | None = None       # bundled speech, resident once loaded
        self._aux: dict[str, AuxHandle] = {}       # kind -> handle
        self._pcache = PromptCacheStore(disk_dir=prompt_cache_dir, settings=self.settings)
        self.state = "empty"                        # empty | loading | ready | error
        self.loading_repo = ""
        self.error = ""
        self.last_unloaded: tuple[str, str | None] | None = None
        self.unloaded_reason = ""
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="mlx", daemon=True).start()
        threading.Thread(target=self._idle_watch, name="idle-unload", daemon=True).start()

    # ---- queue plumbing ---------------------------------------------------------------------
    def _submit(self, fn: Callable[[], Any]) -> Future:
        fut: Future = Future()
        self._q.put(_Task(fn, fut))
        return fut

    def _next_item(self) -> Any:
        if self._deferred:
            return self._deferred.popleft()
        return self._q.get()

    def _drain_into_deferred(self) -> None:
        while True:
            try:
                self._deferred.append(self._q.get_nowait())
            except queue.Empty:
                return

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._next_item()
            if isinstance(item, _Task):
                if item.future.set_running_or_notify_cancel():
                    try:
                        item.future.set_result(item.fn())
                    except BaseException as exc:  # noqa: BLE001 - delivered through the future
                        item.future.set_exception(exc)
            elif isinstance(item, GenJob):
                self._drain_into_deferred()
                if item.batchable(self.settings) and self._deferred and isinstance(self._deferred[0], GenJob) \
                        and self._deferred[0].lm is item.lm and self._deferred[0].batchable(self.settings):
                    self._run_batch(item)
                else:
                    self._run_job(item, self._run_single)

    @property
    def queue_depth(self) -> int:
        return self._q.qsize() + len(self._deferred)

    # ---- lifecycle ----------------------------------------------------------------------------
    @property
    def loaded(self) -> ModelHandle | None:
        return self._loaded

    def check_fit(self, repo: RepoId, draft: RepoId | None = None) -> dict:
        """Memory-guard verdict for loading repo (+ draft) right now."""
        main = self.store.get(repo)
        if main is None:
            raise ModelUnavailable(f"{repo} is not downloaded")
        size = main.size_bytes
        if draft is not None:
            d = self.store.get(draft)
            if d is None:
                raise ModelUnavailable(f"draft model {draft} is not downloaded")
            size += d.size_bytes
        need = estimate_required_bytes(size)
        vm = psutil.virtual_memory()
        freed = 0
        if self._loaded is not None:
            freed = self._loaded.size_bytes + (self._loaded.draft.size_bytes if self._loaded.draft else 0)
        available = vm.available + freed
        budget = gpu_memory_budget(vm.total)
        if need > budget:
            verdict, reason = "too_large", (f"needs about {need / GB:.1f} GB but this Mac's GPU can use "
                                            f"about {budget / GB:.1f} GB")
        elif need > available:
            verdict, reason = "insufficient_free", (f"needs about {need / GB:.1f} GB but only "
                                                    f"{available / GB:.1f} GB is free; quit other apps or unload models")
        else:
            verdict, reason = "ok", ""
        return {"repo": str(repo), "draft": str(draft) if draft else None, "required_bytes": need,
                "available_bytes": available, "budget_bytes": budget, "verdict": verdict, "reason": reason}

    def load(self, repo: RepoId, draft: RepoId | None = None, force: bool = False) -> ModelHandle:
        return self.load_in_background(repo, draft, force).result()

    def load_in_background(self, repo: RepoId, draft: RepoId | None = None, force: bool = False) -> Future:
        local = self.store.get(repo)
        if local is None:
            raise ModelUnavailable(f"{repo} is not downloaded")
        kind = model_kind(Path(local.path))
        if kind in ("embedding", "speech"):
            raise ModelUnavailable(f"{repo} is a {kind} model; it is loaded automatically by "
                                   f"{'/v1/embeddings' if kind == 'embedding' else '/v1/audio/transcriptions'}")
        if draft is not None and kind != "text":
            raise ModelUnavailable("speculative decoding is only available for text models")
        if self.settings.memory_guard and not force:
            check = self.check_fit(repo, draft)
            if check["verdict"] != "ok":
                raise MemoryGuardRefused(f"{repo} {check['reason']}. Load anyway with force.", check)
        with self._lock:
            self.state, self.loading_repo, self.error = "loading", str(repo), ""
        return self._submit(lambda: self._load_on_mlx_thread(repo, draft, kind))

    def _load_on_mlx_thread(self, repo: RepoId, draft: RepoId | None, kind: str) -> ModelHandle:
        current = self._loaded
        if current is not None and current.repo == str(repo) and \
                (current.draft.repo if current.draft else None) == (str(draft) if draft else None):
            with self._lock:
                self.state, self.loading_repo = "ready", ""
            current.last_used = time.time()
            return current
        local = self.store.get(repo)
        try:
            if local is None:
                raise ModelUnavailable(f"{repo} is not downloaded")
            self._unload_on_mlx_thread()
            start = time.perf_counter()
            mx = _mx()
            path = Path(local.path)
            if kind == "vision":
                from .vision import load_vision_model

                model, processor, tokenizer = load_vision_model(path)
            else:
                from mlx_lm import load as mlx_load

                model, tokenizer = mlx_load(local.path)
                processor = None
            if not mx.metal.is_available():
                # CPU-only hosts (Linux/CI): MLX's half-precision quantized kernels are
                # 10-50x slower on CPU than float32, so upcast activations and scales.
                model.set_dtype(mx.float32)
                log.warning("Metal unavailable; running %s on CPU in float32", repo)
            draft_handle = self._load_draft(draft, tokenizer) if draft else None
            handle = ModelHandle(str(repo), local.path, kind, model, tokenizer, local.size_bytes,
                                 _context_length(path), round(time.perf_counter() - start, 2),
                                 processor=processor, config=_read_config(path), draft=draft_handle,
                                 revision=local.revision)
        except Exception as exc:  # noqa: BLE001 - surface any loader failure to the UI
            with self._lock:
                self.state, self.loading_repo, self.error = "error", "", f"failed to load {repo}: {exc}"
            log.exception("load failed: %s", repo)
            raise ModelUnavailable(self.error) from exc
        with self._lock:
            self._loaded, self.state, self.loading_repo = handle, "ready", ""
            self.last_unloaded, self.unloaded_reason = None, ""
        log.info("loaded %s%s in %.1fs", repo, f" with draft {draft}" if draft else "", handle.load_seconds)
        return handle

    def _load_draft(self, draft: RepoId, main_tokenizer: Any) -> DraftHandle:
        from mlx_lm import load as mlx_load

        local = self.store.get(draft)
        if local is None:
            raise ModelUnavailable(f"draft model {draft} is not downloaded")
        model, tok = mlx_load(local.path)
        main_vocab = main_tokenizer.vocab_size if hasattr(main_tokenizer, "vocab_size") else None
        if main_vocab is not None and getattr(tok, "vocab_size", main_vocab) != main_vocab:
            raise ModelUnavailable(f"draft model {draft} uses a different tokenizer "
                                   f"({tok.vocab_size} vs {main_vocab} tokens); pick a smaller model of the same family")
        if not _mx().metal.is_available():
            model.set_dtype(_mx().float32)
        return DraftHandle(str(draft), model, local.size_bytes)

    def unload(self, reason: str = "") -> None:
        self._submit(lambda: self._unload_on_mlx_thread(reason)).result()

    def _unload_on_mlx_thread(self, reason: str = "") -> None:
        if self._loaded is None:
            return
        handle = self._loaded
        with self._lock:
            self._loaded = None
            self.state = "empty"
            if reason:
                self.last_unloaded = (handle.repo, handle.draft.repo if handle.draft else None)
                self.unloaded_reason = reason
        self._pcache.clear()
        del handle
        gc.collect()
        _mx().clear_cache()
        log.info("unloaded model%s", f" ({reason})" if reason else "")

    # ---- fast slot (optional second resident model for small/background calls) ----------
    @property
    def fast_loaded(self) -> ModelHandle | None:
        return self._fast

    def fast_status(self) -> dict:
        return {"configured": self.settings.fast_model or None,
                "loaded": self._fast is not None,
                "repo": self._fast.repo if self._fast else None,
                "error": self._fast_error}

    def ensure_fast(self) -> ModelHandle:
        """Load settings.fast_model on the MLX thread (blocking; worker threads only)."""
        try:
            handle = self._submit(self._load_fast_on_mlx_thread).result()
            self._fast_error = ""
            return handle
        except Exception as exc:  # noqa: BLE001 - recorded for status, then re-raised
            self._fast_error = str(exc)
            raise

    def _load_fast_on_mlx_thread(self) -> ModelHandle:
        repo_raw = self.settings.fast_model
        if not repo_raw:
            raise ModelUnavailable("no fast model is configured", 503)
        repo = RepoId.parse(repo_raw)
        if self._loaded is not None and self._loaded.repo == str(repo):
            return self._loaded  # already resident as the main model
        if self._fast is not None and self._fast.repo == str(repo):
            self._fast.last_used = time.time()
            return self._fast
        local = self.store.get(repo)
        if local is None:
            raise ModelUnavailable(f"fast model {repo} is not downloaded")
        if model_kind(Path(local.path)) != "text":
            raise ModelUnavailable(f"fast model {repo} must be a text model")
        if self.settings.memory_guard:
            need = estimate_required_bytes(local.size_bytes)
            available = psutil.virtual_memory().available  # main model stays resident
            if need > available:
                raise ModelUnavailable(
                    f"fast model {repo} needs about {need / GB:.1f} GB next to the main model "
                    f"but only {available / GB:.1f} GB is free", 409)
        from mlx_lm import load as mlx_load

        self._fast = None
        start = time.perf_counter()
        model, tokenizer = mlx_load(local.path)
        if not _mx().metal.is_available():
            model.set_dtype(_mx().float32)
        handle = ModelHandle(str(repo), local.path, "text", model, tokenizer, local.size_bytes,
                             _context_length(Path(local.path)),
                             round(time.perf_counter() - start, 2),
                             config=_read_config(Path(local.path)), revision=local.revision)
        self._fast = handle
        log.info("loaded fast model %s in %.1fs (second slot)", repo, handle.load_seconds)
        return handle

    def _drop_fast_on_mlx_thread(self) -> None:
        if self._fast is not None:
            self._fast = None
            gc.collect()
            _mx().clear_cache()
            log.info("unloaded fast model (idle)")

    def resolve(self, requested: str | None, auto_load: bool) -> ModelHandle:
        fast_raw = self.settings.fast_model
        if requested and fast_raw and requested in (fast_raw, fast_raw.split("/", 1)[-1]):
            return self.ensure_fast()
        current = self._loaded
        if requested is None or (current and requested in (current.repo, current.repo.split("/", 1)[1])):
            if current is not None:
                current.last_used = time.time()
                return current
            if auto_load and self.last_unloaded:
                repo, draft = self.last_unloaded
                log.info("reloading %s after idle unload", repo)
                return self.load(RepoId.parse(repo), RepoId.parse(draft) if draft else None)
            raise ModelUnavailable("no model is loaded; load one in the app or pass a downloaded model id", 503)
        try:
            repo = RepoId.parse(requested)
        except ParseError as exc:
            raise ModelUnavailable(str(exc)) from exc
        if self.store.get(repo) is None:
            raise ModelUnavailable(f"model {requested!r} is not downloaded")
        if not auto_load:
            raise ModelUnavailable(f"model {requested!r} is not loaded (auto-load is off)")
        return self.load(repo)

    def _idle_watch(self) -> None:
        while not self._stop.wait(10):
            minutes = self.settings.idle_unload_minutes
            now = time.time()
            if minutes <= 0 or self.metrics.active or self.queue_depth:
                continue
            lm = self._loaded
            if lm is not None and now - lm.last_used > minutes * 60:
                log.info("unloading %s after %d idle minutes", lm.repo, minutes)
                self._submit(lambda: self._unload_on_mlx_thread("idle"))
            for kind, aux in list(self._aux.items()):
                if now - aux.last_used > minutes * 60:
                    self._submit(lambda k=kind: self._drop_aux(k))
            fast = self._fast
            if fast is not None and now - fast.last_used > minutes * 60:
                self._submit(self._drop_fast_on_mlx_thread)

    def _drop_aux(self, kind: str) -> None:
        if self._aux.pop(kind, None) is not None:
            gc.collect()
            _mx().clear_cache()
            log.info("unloaded %s model (idle)", kind)

    # ---- prompts -------------------------------------------------------------------------------
    def chat_prompt(self, lm: ModelHandle, messages: tuple[ChatMessage, ...], tools: tuple[dict, ...] = (),
                    response_format: ResponseFormat | None = None) -> str:
        dicts = [m.as_template_dict() for m in messages]
        if response_format is not None:
            note = instruction(response_format)
            if dicts and dicts[0]["role"] == "system":
                dicts[0] = {**dicts[0], "content": f"{dicts[0]['content']}\n\n{note}".strip()}
            else:
                dicts.insert(0, {"role": "system", "content": note})
        if lm.kind == "vision":
            from .vision import render_vision_prompt

            return render_vision_prompt(lm, dicts, sum(len(m.images) for m in messages))
        tok = lm.tokenizer
        if not getattr(tok, "chat_template", None):
            lines = [f"{d['role'].capitalize()}: {d['content']}" for d in dicts]
            return "\n".join(lines) + "\nAssistant:"
        try:
            return tok.apply_chat_template(dicts, tools=list(tools) or None, add_generation_prompt=True,
                                           tokenize=False)
        except Exception as exc:  # jinja2 errors, e.g. a template that rejects system messages
            raise TemplateError(f"{lm.repo}'s chat template rejected these messages: {exc}") from exc

    # ---- generation ----------------------------------------------------------------------------
    def submit_generation(self, job: GenJob) -> None:
        """Queue a generation. job.emit receives GenEvents, then an Exception if one occurred,
        then None. Called on the MLX thread."""
        self._q.put(job)

    def generate(self, lm: ModelHandle, prompt: str, params: SamplingParams, cancel: threading.Event,
                 **job_kw: Any) -> Iterator[GenEvent]:
        """Blocking iterator over one generation (terminal chat, benchmarks, compare, tests)."""
        q: queue.Queue = queue.Queue()
        self.submit_generation(GenJob(lm, prompt, params, cancel, q.put, **job_kw))
        try:
            while True:
                item = q.get()
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            cancel.set()

    def _run_job(self, job: GenJob, runner: Callable[[GenJob], None]) -> None:
        with self._lock:
            self.metrics.active += 1
            self.metrics.requests_total += 1
        try:
            if not job.cancel.is_set():
                runner(job)
        except Exception as exc:  # noqa: BLE001 - forwarded to the caller
            with self._lock:
                self.metrics.requests_failed += 1
            job.emit(exc)
        finally:
            with self._lock:
                self.metrics.active -= 1
            job.emit(None)

    def _check_current(self, lm: ModelHandle) -> None:
        if self._loaded is not lm and self._aria is not lm and self._fast is not lm:
            raise ModelUnavailable(f"{lm.repo} was unloaded before this request ran")
        lm.last_used = time.time()

    # ---- Aria (bundled helper in its own slot; never evicts the main model) --------
    @property
    def aria_available(self) -> bool:
        if not self.aria_model_path:
            return False
        path = Path(self.aria_model_path)
        return (path / "config.json").is_file() and any(path.glob("*.safetensors"))

    def aria_status(self) -> dict:
        return {"available": self.aria_available, "loaded": self._aria is not None,
                "repo": ARIA_REPO, "model_path": self.aria_model_path}

    def ensure_aria(self) -> ModelHandle:
        """Load Aria on the MLX thread (blocking; call from a worker thread)."""
        return self._submit(self._load_aria_on_mlx_thread).result()

    def _load_aria_on_mlx_thread(self) -> ModelHandle:
        if self._aria is not None:
            self._aria.last_used = time.time()
            return self._aria
        if not self.aria_available:
            raise ModelUnavailable("Aria's bundled model is not present in this build", 404)
        from mlx_lm import load as mlx_load

        path = Path(self.aria_model_path)
        start = time.perf_counter()
        try:
            model, tokenizer = mlx_load(str(path))
            if not _mx().metal.is_available():
                model.set_dtype(_mx().float32)
        except Exception as exc:  # noqa: BLE001 - surfaced as Aria's error state
            raise ModelUnavailable(f"Aria could not start: {exc}", 500) from exc
        size = sum(f.stat().st_size for f in path.glob("*") if f.is_file())
        handle = ModelHandle(ARIA_REPO, str(path), "text", model, tokenizer, size,
                             _context_length(path), round(time.perf_counter() - start, 2),
                             config=_read_config(path))
        self._aria = handle
        log.info("loaded Aria (%s) in %.1fs", ARIA_REPO, handle.load_seconds)
        return handle

    def repair_aria(self) -> ModelHandle:
        def drop_and_load() -> ModelHandle:
            self._aria = None
            gc.collect()
            return self._load_aria_on_mlx_thread()
        return self._submit(drop_and_load).result()

    # ---- Onyx Whisper (bundled speech; resident after first transcription) ----------
    @property
    def onyx_available(self) -> bool:
        if not self.onyx_model_path:
            return False
        path = Path(self.onyx_model_path)
        return (path / "config.json").is_file() and any(path.glob("*.safetensors"))

    def onyx_status(self) -> dict:
        return {"available": self.onyx_available, "loaded": self._onyx is not None,
                "repo": ONYX_REPO}

    def _onyx_handle_on_mlx_thread(self) -> AuxHandle:
        if self._onyx is not None:
            self._onyx.last_used = time.time()
            return self._onyx
        if not self.onyx_available:
            raise ModelUnavailable("the bundled speech model is not present in this build", 404)
        from .audio import load_speech_model

        path = Path(self.onyx_model_path)
        model, tokenizer = load_speech_model(path)
        size = sum(f.stat().st_size for f in path.glob("*") if f.is_file())
        self._onyx = AuxHandle(ONYX_REPO, "speech", model, tokenizer, size)
        log.info("loaded bundled speech model %s", ONYX_REPO)
        return self._onyx

    def transcribe_bundled(self, audio_path: str, **options) -> dict:
        """Transcribe with the bundled Onyx model, loading it on first use."""
        from .audio import transcribe_file

        def task() -> dict:
            handle = self._onyx_handle_on_mlx_thread()
            return transcribe_file(handle.model, audio_path, **options)

        return self._submit(task).result()

    def _finalize(self, job: GenJob, pipeline: OutputPipeline, finish: str, final: GenEvent) -> GenEvent:
        tool_calls: tuple[ToolCall, ...] = ()
        if pipeline.tool_bodies:
            tool_calls = tuple(parse_tool_calls(getattr(job.lm.tokenizer, "tool_parser", None),
                                                pipeline.tool_bodies, job.tools))
            if tool_calls and finish == "stop":
                finish = "tool_calls"
        with self._lock:
            m = self.metrics
            m.tokens_generated += final.completion_tokens
            m.cached_prompt_tokens += final.cached_tokens
            m.draft_accepted_tokens += final.draft_accepted
            if final.generation_tps:
                m.last_generation_tps = final.generation_tps
            if final.prompt_tps:
                m.last_prompt_tps = final.prompt_tps
        return GenEvent(tool_calls=tool_calls, finish_reason=finish, prompt_tokens=final.prompt_tokens,
                        cached_tokens=final.cached_tokens, completion_tokens=final.completion_tokens,
                        prompt_tps=final.prompt_tps, generation_tps=final.generation_tps,
                        peak_memory_gb=final.peak_memory_gb, draft_accepted=final.draft_accepted,
                        batched=final.batched)

    def _run_single(self, job: GenJob) -> None:
        lm = job.lm
        self._check_current(lm)
        if lm.kind == "vision":
            from .vision import run_vision

            run_vision(self, job)
            return
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import make_prompt_cache
        from mlx_lm.sample_utils import make_sampler

        mx = _mx()
        tok = lm.tokenizer
        ids = encode_prompt(tok, job.prompt)
        if lm.context_length and len(ids) >= lm.context_length:
            raise ContextTooLong(f"prompt is {len(ids)} tokens; {lm.repo} supports {lm.context_length}")
        use_draft = lm.draft is not None and job.allow_draft and job.response_format is None
        processors: list = []
        if job.response_format is not None:
            processors.append(lm.grammar().processor(job.response_format))
        if job.ignore_eos:
            processors.append(_eos_blocker(tok))

        cache, reused, key = None, 0, None
        if self.settings.prompt_cache and job.use_cache:
            # Revision in the key: re-downloading a repo must never resurrect
            # KV state computed by different weights.
            key = (lm.repo, lm.revision, use_draft, job.cache_partition)

            def make() -> list:
                c = make_prompt_cache(lm.model)
                return c + make_prompt_cache(lm.draft.model) if use_draft else c

            cache, reused = self._pcache.fetch(key, make, ids)
        feed = ids[reused:]
        kw: dict[str, Any] = {
            "max_tokens": job.params.max_tokens,
            "sampler": make_sampler(temp=job.params.temperature,
                                    top_p=job.params.top_p if job.params.top_p < 1.0 else 0.0),
        }
        if processors:
            kw["logits_processors"] = processors
        if cache is not None:
            kw["prompt_cache"] = cache
        if use_draft:
            kw["draft_model"] = lm.draft.model
            kw["num_draft_tokens"] = self.settings.num_draft_tokens
        if job.params.seed is not None:
            mx.random.seed(job.params.seed)
        mx.reset_peak_memory()

        pipeline = OutputPipeline(job, tok)
        generated: list[int] = []
        last, finish, accepted = None, None, 0
        gen = stream_generate(lm.model, tok, feed, **kw)
        try:
            for resp in gen:
                last = resp
                generated.append(int(resp.token))
                if getattr(resp, "from_draft", False):
                    accepted += 1
                if job.cancel.is_set():
                    finish = "cancelled"
                    break
                for ev in pipeline.push(resp.text):
                    job.emit(ev)
                if pipeline.stopped:
                    finish = "stop"
                    break
                if resp.finish_reason:
                    finish = resp.finish_reason
        finally:
            gen.close()
        if finish != "cancelled":
            for ev in pipeline.finish():
                job.emit(ev)
        if cache is not None and key is not None:
            self._pcache.store(key, ids + generated, cache, len(lm.model.layers))
        job.emit(self._finalize(job, pipeline, finish or "length", GenEvent(
            prompt_tokens=len(ids), cached_tokens=reused,
            completion_tokens=last.generation_tokens if last else 0,
            prompt_tps=round(last.prompt_tps, 1) if last else 0.0,
            generation_tps=round(last.generation_tps, 1) if last else 0.0,
            peak_memory_gb=round(last.peak_memory, 2) if last else 0.0,
            draft_accepted=accepted)))

    def _run_batch(self, first: GenJob) -> None:
        """Continuous batching: serve `first` plus queued compatible jobs together, admitting
        newcomers between decode steps."""
        from mlx_lm.generate import BatchGenerator
        from mlx_lm.sample_utils import make_sampler

        lm = first.lm
        tok = lm.tokenizer
        gen = BatchGenerator(lm.model, stop_tokens=[[t] for t in tok.eos_token_ids],
                             completion_batch_size=self.settings.max_batch_size,
                             prefill_batch_size=min(8, self.settings.max_batch_size))

        @dataclass
        class Seq:
            job: GenJob
            ids: list[int]
            pipeline: OutputPipeline
            detok: Any
            started: float
            first_token_at: float | None = None
            tokens: int = 0

        active: dict[int, Seq] = {}

        def admit(job: GenJob) -> None:
            with self._lock:
                self.metrics.active += 1
                self.metrics.requests_total += 1
                self.metrics.batched_requests += 1
            try:
                self._check_current(job.lm)
                ids = encode_prompt(tok, job.prompt)
                if lm.context_length and len(ids) >= lm.context_length:
                    raise ContextTooLong(f"prompt is {len(ids)} tokens; {lm.repo} supports {lm.context_length}")
                sampler = make_sampler(temp=job.params.temperature,
                                       top_p=job.params.top_p if job.params.top_p < 1.0 else 0.0)
                (uid,) = gen.insert([ids], max_tokens=[job.params.max_tokens], samplers=[sampler])
                active[uid] = Seq(job, ids, OutputPipeline(job, tok), tok.detokenizer, time.perf_counter())
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.metrics.requests_failed += 1
                    self.metrics.active -= 1
                job.emit(exc)
                job.emit(None)

        def close(uid: int, finish: str) -> None:
            seq = active.pop(uid)
            if finish != "cancelled":
                seq.detok.finalize()
                tail = seq.detok.last_segment
                events = seq.pipeline.push(tail) if tail and not seq.pipeline.stopped else []
                events += seq.pipeline.finish()
                for ev in events:
                    seq.job.emit(ev)
            now = time.perf_counter()
            gen_time = max(now - (seq.first_token_at or now), 1e-6)
            prefill_time = max((seq.first_token_at or now) - seq.started, 1e-6)
            seq.job.emit(self._finalize(seq.job, seq.pipeline, finish, GenEvent(
                prompt_tokens=len(seq.ids), completion_tokens=seq.tokens,
                prompt_tps=round(len(seq.ids) / prefill_time, 1),
                generation_tps=round(max(seq.tokens - 1, 0) / gen_time, 1), batched=True)))
            with self._lock:
                self.metrics.active -= 1
            seq.job.emit(None)

        admit(first)
        pending_order_break = False
        try:
            while True:
                # Admit compatible newcomers (strict arrival order: stop at the first incompatible item).
                if not pending_order_break:
                    self._drain_into_deferred()
                    while self._deferred and len(active) < self.settings.max_batch_size:
                        nxt = self._deferred[0]
                        if isinstance(nxt, GenJob) and nxt.lm is lm and nxt.batchable(self.settings):
                            self._deferred.popleft()
                            if nxt.cancel.is_set():
                                nxt.emit(None)
                                continue
                            admit(nxt)
                        else:
                            pending_order_break = True
                            break
                for uid in [u for u, s in active.items() if s.job.cancel.is_set()]:
                    gen.remove([uid])
                    close(uid, "cancelled")
                if not active:
                    break
                _, responses = gen.next()
                for r in responses:
                    seq = active.get(r.uid)
                    if seq is None:
                        continue
                    if r.finish_reason == "stop":       # EOS: token not part of the output
                        close(r.uid, "stop")
                        continue
                    seq.tokens += 1
                    if seq.first_token_at is None:
                        seq.first_token_at = time.perf_counter()
                    seq.detok.add_token(int(r.token))
                    for ev in seq.pipeline.push(seq.detok.last_segment):
                        seq.job.emit(ev)
                    if seq.pipeline.stopped:
                        gen.remove([r.uid])
                        close(r.uid, "stop")
                    elif r.finish_reason:
                        close(r.uid, r.finish_reason)
        except Exception as exc:  # noqa: BLE001 - fail every sequence still in flight
            log.exception("batch generation failed")
            for uid in list(active):
                seq = active.pop(uid)
                with self._lock:
                    self.metrics.requests_failed += 1
                    self.metrics.active -= 1
                seq.job.emit(exc)
                seq.job.emit(None)
        finally:
            gen.close()

    # ---- auxiliary models (embeddings, speech) ---------------------------------------------------
    def _aux_handle(self, kind: str, repo: RepoId) -> AuxHandle:
        """Runs on the MLX thread."""
        current = self._aux.get(kind)
        if current is not None and current.repo == str(repo):
            current.last_used = time.time()
            return current
        local = self.store.get(repo)
        if local is None:
            raise ModelUnavailable(f"{repo} is not downloaded")
        actual = model_kind(Path(local.path))
        if actual != kind:
            raise ModelUnavailable(f"{repo} is a {actual} model, not a{'n' if kind == 'embedding' else ''} {kind} model")
        self._aux.pop(kind, None)
        gc.collect()
        if kind == "embedding":
            from .embeddings import load_embedding_model

            model, tokenizer = load_embedding_model(Path(local.path))
        else:
            from .audio import load_speech_model

            model, tokenizer = load_speech_model(Path(local.path))
        handle = AuxHandle(str(repo), kind, model, tokenizer, local.size_bytes)
        self._aux[kind] = handle
        log.info("loaded %s model %s", kind, repo)
        return handle

    def embed(self, repo: RepoId, texts: list[str]) -> tuple[list[list[float]], int]:
        from .embeddings import embed_texts

        def task() -> tuple[list[list[float]], int]:
            h = self._aux_handle("embedding", repo)
            return embed_texts(h.model, h.tokenizer, texts)

        return self._submit(task).result()

    def transcribe(self, repo: RepoId, audio_path: str, **options: Any) -> dict:
        from .audio import transcribe_file

        def task() -> dict:
            h = self._aux_handle("speech", repo)
            return transcribe_file(h.model, audio_path, **options)

        return self._submit(task).result()

    def run_on_mlx_thread(self, fn: Callable[[], Any]) -> Any:
        return self._submit(fn).result()

    # ---- reporting ------------------------------------------------------------------------------
    def memory(self) -> dict[str, int] | None:
        try:
            return mlx_memory()
        except Exception:  # noqa: BLE001 - counters are informational
            return None

    def status(self) -> dict:
        lm = self._loaded
        with self._lock:
            m = self.metrics
            return {
                "state": self.state,
                "loading_repo": self.loading_repo,
                "error": self.error,
                "loaded": None if lm is None else lm.describe(),
                "unloaded_reason": self.unloaded_reason,
                "last_unloaded": self.last_unloaded[0] if self.last_unloaded else None,
                "aux": {k: {"repo": h.repo, "size_bytes": h.size_bytes} for k, h in self._aux.items()},
                "fast": self.fast_status(),
                "prompt_cache_disk": self._pcache.disk_usage(),
                "metrics": {
                    "requests_total": m.requests_total, "requests_failed": m.requests_failed,
                    "tokens_generated": m.tokens_generated, "last_generation_tps": m.last_generation_tps,
                    "last_prompt_tps": m.last_prompt_tps, "queue_depth": self.queue_depth,
                    "active": m.active > 0, "active_requests": m.active,
                    "batched_requests": m.batched_requests, "cached_prompt_tokens": m.cached_prompt_tokens,
                    "draft_accepted_tokens": m.draft_accepted_tokens,
                },
            }


def _eos_blocker(tokenizer: Any) -> Callable:
    """Logits processor that forbids EOS so benchmarks always generate the requested length."""
    import numpy as np

    eos = sorted(getattr(tokenizer, "eos_token_ids", []) or [])
    cached: dict[int, Any] = {}

    def proc(tokens: Any, logits: Any) -> Any:
        if not eos:
            return logits
        width = logits.shape[-1]
        if width not in cached:
            m = np.zeros(width, dtype=np.float32)
            m[[e for e in eos if e < width]] = -np.inf
            cached[width] = _mx().array(m)
        return logits + cached[width].astype(logits.dtype)

    return proc
