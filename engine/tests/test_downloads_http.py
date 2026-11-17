"""DownloadManager against a local HTTP server standing in for the Hugging Face Hub."""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from bridle_engine import downloads as dl
from bridle_engine.catalog import ModelStore
from bridle_engine.domain import RepoId

WEIGHTS = bytes(range(256)) * 4096  # 1 MiB
CONFIG = b'{"model_type": "llama"}'
FILES = {"config.json": CONFIG, "model.safetensors": WEIGHTS}


class Handler(BaseHTTPRequestHandler):
    corrupt = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1]
        data = FILES.get(name)
        if data is None:
            self.send_error(404)
            return
        if Handler.corrupt and name == "model.safetensors":
            data = b"\0" * len(data)
        start = 0
        rng = self.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            self.send_response(206)
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def hub(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    siblings = [
        SimpleNamespace(rfilename="config.json", size=len(CONFIG), lfs=None),
        SimpleNamespace(rfilename="model.safetensors", size=len(WEIGHTS),
                        lfs=SimpleNamespace(sha256=hashlib.sha256(WEIGHTS).hexdigest(), size=len(WEIGHTS))),
        SimpleNamespace(rfilename="README.md", size=3, lfs=None),
    ]

    class FakeApi:
        def __init__(self, endpoint=None, token=None):
            pass

        def model_info(self, repo, files_metadata=False):
            return SimpleNamespace(sha="rev123", siblings=siblings)

    monkeypatch.setattr(dl, "HfApi", FakeApi)
    Handler.corrupt = False
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def wait(job, timeout=20):
    end = time.time() + timeout
    while job.state in dl.ACTIVE_STATES and time.time() < end:
        time.sleep(0.05)
    return job


def test_download_resumes_and_verifies(tmp_path, hub):
    store = ModelStore(tmp_path)
    repo = RepoId.parse("mlx-community/Tiny")
    partial = store.partial_path_for(repo)
    partial.mkdir(parents=True)
    (partial / "model.safetensors").write_bytes(WEIGHTS[:300_000])  # interrupted earlier
    done = []
    job = wait(dl.DownloadManager(store, endpoint=hub, token="", on_complete=done.append).start(repo))
    assert job.state == "completed", job.error
    assert job.done_bytes == job.total_bytes == len(WEIGHTS) + len(CONFIG)
    local = store.get(repo)
    assert local is not None and local.revision == "rev123"
    manifest = json.loads((store.path_for(repo) / ".bridle.json").read_text())
    assert manifest["file_hashes"]["model.safetensors"] == hashlib.sha256(WEIGHTS).hexdigest()
    assert manifest["file_hashes"]["config.json"] == hashlib.sha256(CONFIG).hexdigest()
    assert (store.path_for(repo) / "model.safetensors").read_bytes() == WEIGHTS
    assert not partial.exists() and done == [repo]


def test_download_rejects_corrupt_weights(tmp_path, hub):
    Handler.corrupt = True
    store = ModelStore(tmp_path)
    job = wait(dl.DownloadManager(store, endpoint=hub, token="").start(RepoId.parse("mlx-community/Tiny")))
    assert job.state == "failed" and "sha256 mismatch" in job.error
    assert store.get(RepoId.parse("mlx-community/Tiny")) is None


def test_redownload_replaces_existing_model(tmp_path, hub):
    store = ModelStore(tmp_path)
    repo = RepoId.parse("mlx-community/Tiny")
    manager = dl.DownloadManager(store, endpoint=hub, token="")
    assert wait(manager.start(repo)).state == "completed"
    assert wait(manager.start(repo)).state == "completed"
    assert (store.path_for(repo) / "model.safetensors").read_bytes() == WEIGHTS
    assert store.get(repo) is not None
    assert not list(tmp_path.glob("mlx-community/*.old-*"))


def test_duplicate_start_returns_same_job(tmp_path, hub):
    manager = dl.DownloadManager(ModelStore(tmp_path), endpoint=hub, token="")
    repo = RepoId.parse("mlx-community/Tiny")
    a, b = manager.start(repo), manager.start(repo)
    assert a.id == b.id or wait(a).state == "completed"
    wait(a)
