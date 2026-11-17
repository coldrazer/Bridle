"""Production add-ons: doctor, egress, scorecards, storage, metrics,
support bundles, backup/restore, Responses API, queue + disk prompt cache."""

import json
import stat
import zipfile
from pathlib import Path

import pytest

from test_api import CTL, TEST_MODEL, client_for, have

needs_model = pytest.mark.skipif(not have(TEST_MODEL), reason="set BRIDLE_TEST_MODELS_DIR")


def test_doctor_reports_checks(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    assert c.get("/control/doctor").status_code == 401
    body = c.get("/control/doctor", headers=CTL).json()
    names = {row["name"] for row in body["checks"]}
    assert {"python", "platform", "disk", "model"} <= names
    assert any(n.startswith("harness:") for n in names)


def test_metrics_loopback_and_content(tmp_path):
    from fastapi.testclient import TestClient

    from bridle_engine.api import create_app
    from bridle_engine.config import ServerConfig
    from bridle_engine.logbuffer import LogBuffer
    from bridle_engine.service import Engine

    c, _ = client_for(tmp_path / "m", tmp_path)
    r = c.get("/metrics")
    assert r.status_code == 200 and "bridle_requests_total" in r.text
    config = ServerConfig(models_dir=tmp_path / "m2", data_dir=tmp_path, control_token="secret")
    remote = TestClient(create_app(Engine.build(config, LogBuffer())),
                         base_url="http://127.0.0.1:49813")
    assert remote.get("/metrics").status_code == 403  # testclient host is not loopback here


def test_egress_records_downloads_and_clears(tmp_path, monkeypatch):
    from bridle_engine import egress

    egress.LOG.clear()
    c, engine = client_for(tmp_path / "m", tmp_path)
    started = engine.downloads.start
    monkeypatch.setattr(engine.downloads, "_download", lambda job: egress.record(engine.downloads.endpoint, f"model download {job.repo}"))
    c.post("/control/downloads", headers=CTL, json={"repo": "a/b"})
    events = c.get("/control/egress", headers=CTL).json()["events"]
    assert any("model download" in e["purpose"] for e in events)
    assert c.request("DELETE", "/control/egress", headers=CTL).status_code == 200
    assert c.get("/control/egress", headers=CTL).json()["total"] == 0
    monkeypatch.setattr(engine.downloads, "start", started)


def test_storage_report_and_prune_without_quota(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    report = c.get("/control/storage", headers=CTL).json()
    assert report["total_bytes"] >= 0 and "prune_candidates" in report
    out = c.post("/control/storage/prune", headers=CTL, json={}).json()
    assert out["pruned"] == []


def test_support_bundle_and_backup_roundtrip(tmp_path):
    data = tmp_path / "data"
    c, engine = client_for(data / "models", data)
    c.put("/control/presets", headers=CTL, json={"name": "Coder", "model": "a/b"})
    bundle = c.post("/control/support-bundle", headers=CTL, json={}).json()
    assert Path(bundle["path"]).is_file()
    with zipfile.ZipFile(bundle["path"]) as archive:
        assert "status.json" in archive.namelist()

    exported = c.post("/control/backup/export", headers=CTL, json={"passphrase": "pw123456"}).json()
    assert exported["encrypted"] is True and Path(exported["path"]).stat().st_mode
    assert stat.S_IMODE(Path(exported["path"]).stat().st_mode) == 0o600
    # Wrong passphrase is a clean 400; the right one restores.
    bad = c.post("/control/backup/import", headers=CTL,
                 json={"path": exported["path"], "passphrase": "wrong"})
    assert bad.status_code == 400
    ok = c.post("/control/backup/import", headers=CTL,
                json={"path": exported["path"], "passphrase": "pw123456"})
    assert ok.status_code == 200 and ok.json()["restart_required"] is True
    plain = c.post("/control/backup/export", headers=CTL, json={}).json()
    assert plain["encrypted"] is False
    with zipfile.ZipFile(plain["path"]) as archive:
        assert "presets.json" in archive.namelist()
        assert not any("key" in Path(n).name and n.startswith("tls/") for n in archive.namelist())


def test_responses_rejects_unsupported_features():
    import pytest as _pytest

    from bridle_engine.domain import ParseError
    from bridle_engine.responses import to_openai_body

    with _pytest.raises(ParseError):
        to_openai_body({"input": "hi", "previous_response_id": "resp_1"})
    with _pytest.raises(ParseError):
        to_openai_body({"input": "hi", "background": True})
    with _pytest.raises(ParseError):
        to_openai_body({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]})
    out = to_openai_body({"input": "hi",
                          "tools": [{"type": "function", "name": "f", "parameters": {}}],
                          "tool_choice": "required"})
    assert out["tool_choice"] == "required"


def test_responses_translation_and_validation(tmp_path):
    from bridle_engine.responses import to_openai_body

    body = to_openai_body({
        "model": "a/b", "instructions": "Be terse.", "max_output_tokens": 999999,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                  {"type": "function_call", "call_id": "c1", "name": "ls", "arguments": "{}"},
                  {"type": "function_call_output", "call_id": "c1", "output": "done"}],
        "tools": [{"type": "function", "name": "ls", "parameters": {"type": "object"}}]})
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert body["max_tokens"] == 32768

    c, _ = client_for(tmp_path / "m", tmp_path)
    r = c.post("/v1/responses", json={"model": "a/b", "input": "hi"})
    assert r.status_code == 404 and "error" in r.json()


def test_queue_waits_instead_of_failing_fast(tmp_path):
    c, engine = client_for(tmp_path / "m", tmp_path)
    c.patch("/control/settings", headers=CTL,
            json={"max_concurrent_requests": 1, "queue_wait_seconds": 0})
    status = c.get("/control/status", headers=CTL).json()
    assert status["server"]["inference"]["limit"] == 1


def test_prompt_cache_disk_roundtrip(tmp_path):
    from mlx_lm import load as mlx_load
    from mlx_lm.models.cache import make_prompt_cache

    from bridle_engine.runtime import PromptCacheStore, encode_prompt
    from bridle_engine.settings import EngineSettings
    from test_api import MODELS_DIR

    if not have(TEST_MODEL):
        pytest.skip("fixture model unavailable")
    model, tokenizer = mlx_load(str(Path(MODELS_DIR) / TEST_MODEL))
    settings = EngineSettings()  # prompt_cache_disk defaults on
    store = PromptCacheStore(disk_dir=tmp_path / "pcache", settings=settings)
    store._DISK_MIN_TOKENS = 0  # tiny fixture prompts are below the production threshold
    from mlx_lm import stream_generate

    ids = encode_prompt(tokenizer, "The capital of France is")
    cache = make_prompt_cache(model)
    generated = [int(resp.token) for resp in stream_generate(model, tokenizer, ids, max_tokens=2, prompt_cache=cache)]
    store.store(("repo", False, "local"), ids + generated, cache, len(model.layers))
    assert store.disk_usage()["files"] == 1
    # A fresh store (simulating a restart) reuses the disk entry.
    fresh = PromptCacheStore(disk_dir=tmp_path / "pcache", settings=settings)
    cache2, reused = fresh.fetch(("repo", False, "local"), lambda: make_prompt_cache(model), ids)
    assert reused == len(ids) - 1 and cache2 is not None


@needs_model
def test_scorecard_and_fast_slot(tmp_path):
    from test_api import MODELS_DIR

    c, engine = client_for(MODELS_DIR, tmp_path)
    load = c.post("/control/models/load", headers=CTL, json={"repo": TEST_MODEL})
    assert load.status_code == 202
    import time
    for _ in range(100):
        if c.get("/control/status", headers=CTL).json()["model"]["state"] == "ready":
            break
        time.sleep(0.2)
    result = c.post("/control/scorecards/run", headers=CTL, json={}).json()
    assert result["total"] == 4 and 0 <= result["passed"] <= 4
    assert c.get("/control/scorecards", headers=CTL).json()["scorecards"][0]["repo"] == TEST_MODEL

    # Fast slot: configure the same fixture model as fast, ask for it explicitly.
    c.patch("/control/settings", headers=CTL, json={"fast_model": TEST_MODEL})
    handle = engine.runtime.ensure_fast()
    assert handle.repo == TEST_MODEL  # resident main model is reused, not double-loaded
    assert c.get("/control/status", headers=CTL).json()["model"]["fast"]["configured"] == TEST_MODEL


@needs_model
def test_responses_generation(tmp_path):
    from test_api import MODELS_DIR

    c, _ = client_for(MODELS_DIR, tmp_path)
    r = c.post("/v1/responses", json={"model": TEST_MODEL, "max_output_tokens": 16,
                                      "input": "Reply with the single word: hello"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["object"] == "response" and payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["type"] == "output_text"
    assert payload["usage"]["output_tokens"] > 0
    with c.stream("POST", "/v1/responses",
                  json={"model": TEST_MODEL, "input": "Say hi", "stream": True}) as resp:
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data:")]
    types = [e["type"] for e in events]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert "response.output_text.delta" in types and "response.content_part.done" in types
