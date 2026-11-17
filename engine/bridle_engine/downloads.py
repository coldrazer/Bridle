"""Resumable, verified model downloads from the Hugging Face Hub with live progress."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError, RepositoryNotFoundError

from . import __version__
from .catalog import MANIFEST, ModelStore
from .domain import RepoId

log = logging.getLogger("bridle.downloads")

ALLOWED_SUFFIXES = (".json", ".safetensors", ".txt", ".model", ".tiktoken", ".jinja")
SKIP_FILES = {"README.md", ".gitattributes"}
CHUNK = 1 << 20
MAX_ATTEMPTS = 4
ACTIVE_STATES = ("queued", "resolving", "downloading", "verifying")


class DownloadCancelled(Exception):
    pass


class DownloadFailed(Exception):
    pass


@dataclass(frozen=True)
class RemoteFile:
    name: str
    size: int
    sha256: str | None  # present for LFS files


@dataclass
class DownloadJob:
    repo: RepoId
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "queued"
    total_bytes: int = 0
    done_bytes: int = 0
    current_file: str = ""
    revision: str = ""
    error: str = ""
    bytes_per_second: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_json(self, store: ModelStore) -> dict:
        return {
            "id": self.id,
            "repo": str(self.repo),
            "state": self.state,
            "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes,
            "fraction": (self.done_bytes / self.total_bytes) if self.total_bytes else 0.0,
            "current_file": self.current_file,
            "revision": self.revision,
            "error": self.error,
            "bytes_per_second": round(self.bytes_per_second),
            "destination": str(store.path_for(self.repo)),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def select_files(siblings: list) -> list[RemoteFile]:
    """Top-level weight, config and tokenizer files only (skips docs, images, originals)."""
    files: list[RemoteFile] = []
    for s in siblings:
        name = s.rfilename
        if "/" in name or name in SKIP_FILES or not name.endswith(ALLOWED_SUFFIXES):
            continue
        lfs = getattr(s, "lfs", None)
        sha = getattr(lfs, "sha256", None) if lfs is not None else None
        size = s.size if s.size is not None else (getattr(lfs, "size", None) or 0)
        files.append(RemoteFile(name, int(size), sha))
    names = {f.name for f in files}
    if "config.json" not in names or not any(n.endswith(".safetensors") for n in names):
        raise DownloadFailed("repository has no config.json + .safetensors weights (not an MLX model?)")
    return files


class DownloadManager:
    def __init__(self, store: ModelStore, endpoint: str | None = None, token: str | None = None,
                 on_complete: Callable[[RepoId], None] | None = None, max_parallel: int = 2):
        self.store = store
        self.endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
        self.token = token if token is not None else (os.environ.get("HF_TOKEN") or _cached_hf_token())
        self.on_complete = on_complete
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max_parallel)

    # ---- public API -------------------------------------------------------------------
    def start(self, repo: RepoId) -> DownloadJob:
        with self._lock:
            for job in self._jobs.values():
                if job.repo == repo and job.state in ACTIVE_STATES:
                    return job
            job = DownloadJob(repo)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), name=f"download-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or job.state not in ACTIVE_STATES:
            return False
        job.cancel_event.set()
        return True

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[DownloadJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def clear_finished(self) -> None:
        with self._lock:
            self._jobs = {k: j for k, j in self._jobs.items() if j.state in ACTIVE_STATES}

    # ---- worker -----------------------------------------------------------------------
    def _run(self, job: DownloadJob) -> None:
        with self._slots:
            try:
                self._download(job)
                job.state = "completed"
                log.info("download complete: %s (%.2f GB)", job.repo, job.total_bytes / 1e9)
                if self.on_complete:
                    self.on_complete(job.repo)
            except DownloadCancelled:
                job.state = "cancelled"
                log.info("download cancelled: %s", job.repo)
            except RepositoryNotFoundError:
                job.state, job.error = "failed", f"{job.repo} was not found on Hugging Face (or is gated/private)"
            except (DownloadFailed, HfHubHTTPError, httpx.HTTPError, OSError) as exc:
                job.state, job.error = "failed", str(exc) or type(exc).__name__
                log.error("download failed: %s: %s", job.repo, job.error)
            finally:
                job.finished_at = time.time()
                job.bytes_per_second = 0.0

    def _download(self, job: DownloadJob) -> None:
        job.state = "resolving"
        from . import egress
        egress.record(self.endpoint, f"model download {job.repo}")
        info = HfApi(endpoint=self.endpoint, token=self.token or None).model_info(str(job.repo), files_metadata=True)
        job.revision = info.sha or "main"
        files = select_files(info.siblings or [])
        job.total_bytes = sum(f.size for f in files)

        partial = self.store.partial_path_for(job.repo)
        partial.mkdir(parents=True, exist_ok=True)
        revision_file = partial / ".revision"
        previous_revision = revision_file.read_text() if revision_file.exists() else ""
        if previous_revision and previous_revision != job.revision:
            shutil.rmtree(partial)
            partial.mkdir(parents=True)
        revision_file.write_text(job.revision)
        already = sum(min((partial / f.name).stat().st_size, f.size) for f in files if (partial / f.name).exists())
        free = shutil.disk_usage(partial).free
        if job.total_bytes - already > free - 1_000_000_000:
            raise DownloadFailed(
                f"not enough disk space: need {(job.total_bytes - already) / 1e9:.1f} GB, "
                f"{free / 1e9:.1f} GB free")

        job.state = "downloading"
        completed_bytes = 0
        headers = {"user-agent": f"bridle-engine/{__version__}"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        timeout = httpx.Timeout(30.0, read=120.0)
        file_hashes: dict[str, str] = {}
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            for f in files:
                self._check_cancel(job)
                job.current_file = f.name
                url = f"{self.endpoint}/{job.repo}/resolve/{job.revision}/{quote(f.name)}"
                file_hashes[f.name] = self._fetch_file(client, url, partial / f.name, f, job, completed_bytes)
                completed_bytes += f.size
                job.done_bytes = completed_bytes

        job.state, job.current_file = "verifying", ""
        for f in files:
            verifier = hashlib.sha256()
            with (partial / f.name).open("rb") as fh:
                for block in iter(lambda: fh.read(CHUNK), b""):
                    verifier.update(block)
            if verifier.hexdigest() != file_hashes[f.name]:
                raise DownloadFailed(f"{f.name}: sha256 changed during verification")
        manifest = {
            "repo": str(job.repo),
            "revision": job.revision,
            "size_bytes": job.total_bytes,
            "files": [f.name for f in files],
            "file_hashes": file_hashes,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (partial / MANIFEST).write_text(json.dumps(manifest, indent=2))
        final = self.store.path_for(job.repo)
        final.parent.mkdir(parents=True, exist_ok=True)
        backup = final.with_name(final.name + f".old-{uuid.uuid4().hex[:12]}") if final.exists() else None
        if backup is not None:
            final.rename(backup)
        try:
            partial.rename(final)
        except BaseException:
            if backup is not None and backup.exists() and not final.exists():
                backup.rename(final)
            raise
        if backup is not None:
            shutil.rmtree(backup)

    def _fetch_file(self, client: httpx.Client, url: str, dest: Path, f: RemoteFile,
                    job: DownloadJob, base: int) -> str:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._fetch_once(client, url, dest, f, job, base)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise DownloadFailed(f"{f.name}: network error after {attempt} attempts: {exc}") from exc
                log.warning("%s: %s (retry %d/%d)", f.name, exc, attempt, MAX_ATTEMPTS - 1)
                time.sleep(min(2 ** attempt, 10))
                self._check_cancel(job)

    def _fetch_once(self, client: httpx.Client, url: str, dest: Path, f: RemoteFile,
                    job: DownloadJob, base: int) -> str:
        have = dest.stat().st_size if dest.exists() else 0
        if have and not f.sha256:
            # Without a content hash, length cannot establish revision identity.
            dest.unlink()
            have = 0
        if have > f.size:
            dest.unlink()
            have = 0
        hasher = hashlib.sha256()  # hashed even without a Hub hash, for the manifest
        if have:
            with dest.open("rb") as fh:
                for block in iter(lambda: fh.read(CHUNK), b""):
                    hasher.update(block)
        if have < f.size or f.size == 0:
            req_headers = {"range": f"bytes={have}-"} if have else {}
            with client.stream("GET", url, headers=req_headers) as resp:
                resp.raise_for_status()
                if have and resp.status_code != 206:
                    # Server ignored the range request: start this file over.
                    have = 0
                    hasher = hashlib.sha256()
                mode = "ab" if have else "wb"
                window_start, window_bytes = time.monotonic(), 0
                with dest.open(mode) as out:
                    for chunk in resp.iter_bytes(CHUNK):
                        self._check_cancel(job)
                        out.write(chunk)
                        hasher.update(chunk)
                        have += len(chunk)
                        window_bytes += len(chunk)
                        job.done_bytes = base + have
                        elapsed = time.monotonic() - window_start
                        if elapsed >= 0.5:
                            rate = window_bytes / elapsed
                            job.bytes_per_second = rate if not job.bytes_per_second else 0.7 * job.bytes_per_second + 0.3 * rate
                            window_start, window_bytes = time.monotonic(), 0
        size = dest.stat().st_size
        if f.size and size != f.size:
            raise DownloadFailed(f"{f.name}: size mismatch ({size} != {f.size})")
        if f.sha256 and hasher.hexdigest() != f.sha256:
            dest.unlink()
            raise DownloadFailed(f"{f.name}: sha256 mismatch; the partial file was removed, retry the download")
        return hasher.hexdigest()

    @staticmethod
    def _check_cancel(job: DownloadJob) -> None:
        if job.cancel_event.is_set():
            raise DownloadCancelled()


def _cached_hf_token() -> str | None:
    try:
        from huggingface_hub import get_token
        return get_token()
    except Exception:  # noqa: BLE001 - token lookup is best effort
        return None
