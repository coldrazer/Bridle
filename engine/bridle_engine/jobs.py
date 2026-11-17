"""Long-running jobs: benchmarks and side-by-side model comparisons."""

from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .domain import ChatMessage, ParseError, RepoId, SamplingParams
from .runtime import InferenceRuntime, ModelHandle, encode_prompt

log = logging.getLogger("bridle.jobs")

_FILLER = ("The history of computing is a story of abstraction layered upon abstraction, from vacuum tubes "
           "to transistors, from machine code to high-level languages, and from single machines to networks. ")


# ---- benchmarks -----------------------------------------------------------------------------------
@dataclass
class BenchmarkResult:
    id: str
    repo: str
    draft: str | None
    chip: str
    prompt_tokens: int
    generated_tokens: int
    prompt_tps: float
    generation_tps: float
    time_to_first_token_ms: int
    peak_memory_gb: float
    load_seconds: float
    created_at: float


def parse_bench(body: Any) -> tuple[int, int]:
    body = body or {}
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    p, g = body.get("prompt_tokens", 512), body.get("generation_tokens", 128)
    for name, v, hi in (("prompt_tokens", p, 16384), ("generation_tokens", g, 4096)):
        if isinstance(v, bool) or not isinstance(v, int) or not 16 <= v <= hi:
            raise ParseError(f"{name} must be an integer in [16, {hi}]", name)
    return p, g


class BenchmarkStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        with self._lock:
            if not self.path.is_file():
                return []
            try:
                return json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                return []

    def add(self, result: BenchmarkResult) -> None:
        rows = self.list()
        rows.insert(0, asdict(result))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".benchmarks-", dir=self.path.parent)  # 0600 from creation
            tmp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(rows[:500], indent=2))
                tmp.replace(self.path)
            finally:
                tmp.unlink(missing_ok=True)

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()


def run_benchmark(rt: InferenceRuntime, lm: ModelHandle, chip: str, prompt_tokens: int,
                  generation_tokens: int) -> BenchmarkResult:
    """Measures prompt processing and generation speed on the loaded model. Uses a fresh KV cache
    (no prompt-cache hits) and forbids EOS so exactly generation_tokens are produced."""
    tok = lm.tokenizer
    unit = encode_prompt(tok, _FILLER)
    ids = (unit * (prompt_tokens // max(len(unit), 1) + 1))[:prompt_tokens]
    prompt = tok.decode(ids)
    params = SamplingParams(generation_tokens, 0.0, 1.0, (), 0)
    start = time.perf_counter()
    first_at = None
    final = None
    for ev in rt.generate(lm, prompt, params, threading.Event(), use_cache=False, ignore_eos=True):
        if first_at is None and (ev.text or ev.reasoning):
            first_at = time.perf_counter()
        if ev.finish_reason:
            final = ev
    if final is None:
        raise RuntimeError("benchmark produced no result")
    return BenchmarkResult(
        id=uuid.uuid4().hex[:10], repo=lm.repo, draft=lm.draft.repo if lm.draft else None, chip=chip,
        prompt_tokens=final.prompt_tokens, generated_tokens=final.completion_tokens,
        prompt_tps=final.prompt_tps, generation_tps=final.generation_tps,
        time_to_first_token_ms=int(((first_at or time.perf_counter()) - start) * 1000),
        peak_memory_gb=final.peak_memory_gb, load_seconds=lm.load_seconds, created_at=time.time())


# ---- compare --------------------------------------------------------------------------------------
@dataclass
class CompareRun:
    repo: str
    state: str = "pending"          # pending | loading | generating | done | failed
    text: str = ""
    reasoning: str = ""
    error: str = ""
    completion_tokens: int = 0
    generation_tps: float = 0.0
    prompt_tps: float = 0.0
    peak_memory_gb: float = 0.0
    seconds: float = 0.0


@dataclass
class CompareJob:
    prompt: str
    system: str
    params: SamplingParams
    runs: list[CompareRun]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: str = "running"          # running | done | cancelled
    created_at: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_json(self) -> dict:
        return {"id": self.id, "prompt": self.prompt, "system": self.system, "state": self.state,
                "created_at": self.created_at, "runs": [asdict(r) for r in self.runs]}


def parse_compare(body: Any) -> CompareJob:
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    models = body.get("models")
    if not isinstance(models, list) or not 2 <= len(models) <= 4:
        raise ParseError("models must list 2 to 4 downloaded model ids", "models")
    repos = [str(RepoId.parse(m)) for m in models]
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ParseError("prompt must be a non-empty string", "prompt")
    system = body.get("system") or ""
    if not isinstance(system, str):
        raise ParseError("system must be a string", "system")
    params = SamplingParams.parse({k: body[k] for k in ("max_tokens", "temperature", "top_p", "seed") if k in body},
                                  default_max_tokens=512)
    return CompareJob(prompt, system, params, [CompareRun(r) for r in repos])


class CompareManager:
    """Runs the same prompt through several models one after another (each is loaded in turn so
    the comparison never needs more memory than the largest model), then restores the original."""

    def __init__(self, rt: InferenceRuntime):
        self.rt = rt
        self._jobs: dict[str, CompareJob] = {}
        self._lock = threading.Lock()

    def start(self, job: CompareJob) -> CompareJob:
        with self._lock:
            if any(j.state == "running" for j in self._jobs.values()):
                raise ParseError("a comparison is already running")
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), name=f"compare-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> CompareJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[CompareJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:20]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state != "running":
            return False
        job.cancel.set()
        return True

    def _run(self, job: CompareJob) -> None:
        original = self.rt.loaded
        restore = (original.repo, original.draft.repo if original.draft else None) if original else None
        messages = ((ChatMessage("system", job.system),) if job.system else ()) + (ChatMessage("user", job.prompt),)
        try:
            for run in job.runs:
                if job.cancel.is_set():
                    break
                started = time.perf_counter()
                try:
                    run.state = "loading"
                    lm = self.rt.load(RepoId.parse(run.repo))
                    run.state = "generating"
                    prompt = self.rt.chat_prompt(lm, messages)
                    run_cancel = threading.Event()   # generate() sets its event when done; keep it per run
                    for ev in self.rt.generate(lm, prompt, job.params, run_cancel, use_cache=False, allow_draft=False):
                        if job.cancel.is_set():
                            break                     # closing the generator cancels this run
                        run.text += ev.text
                        run.reasoning += ev.reasoning
                        if ev.finish_reason:
                            run.completion_tokens = ev.completion_tokens
                            run.generation_tps = ev.generation_tps
                            run.prompt_tps = ev.prompt_tps
                            run.peak_memory_gb = ev.peak_memory_gb
                    run.state = "done"
                except Exception as exc:  # noqa: BLE001 - recorded per run
                    run.state, run.error = "failed", str(exc)
                run.seconds = round(time.perf_counter() - started, 2)
        finally:
            job.state = "cancelled" if job.cancel.is_set() else "done"
            if restore is not None:
                current = self.rt.loaded
                if current is None or current.repo != restore[0]:
                    try:
                        self.rt.load(RepoId.parse(restore[0]), RepoId.parse(restore[1]) if restore[1] else None,
                                     force=True)
                    except Exception as exc:  # noqa: BLE001
                        log.error("could not restore %s after comparison: %s", restore[0], exc)


def machine_chip() -> str:
    return platform.processor() or platform.machine()
