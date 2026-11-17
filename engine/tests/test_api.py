"""API tests. Auth/validation run everywhere. Generation tests run against real models when
BRIDLE_TEST_MODELS_DIR points at a models dir containing them, e.g.:
    python -m bridle_engine --models-dir DIR download mlx-community/Qwen2.5-0.5B-Instruct-4bit
    python -m bridle_engine --models-dir DIR download mlx-community/all-MiniLM-L6-v2-bf16
"""

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridle_engine.api import create_app
from bridle_engine.config import ServerConfig
from bridle_engine.logbuffer import LogBuffer
from bridle_engine.service import Engine

TEST_MODEL = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
EMBED_MODEL = "mlx-community/all-MiniLM-L6-v2-bf16"
MODELS_DIR = os.environ.get("BRIDLE_TEST_MODELS_DIR")


def have(repo: str) -> bool:
    return bool(MODELS_DIR) and (Path(MODELS_DIR) / repo / ".bridle.json").exists()


needs_model = pytest.mark.skipif(not have(TEST_MODEL), reason="set BRIDLE_TEST_MODELS_DIR (needs the test model)")
CTL = {"authorization": "Bearer secret"}
LOCAL = frozenset({"testclient"})


def client_for(models_dir, data_dir, local=True, **cfg):
    config = ServerConfig(models_dir=Path(models_dir), data_dir=Path(data_dir), control_token="secret", **cfg)
    engine = Engine.build(config, LogBuffer())
    app = create_app(engine, control_hosts=LOCAL, loopback_hosts=LOCAL if local else frozenset())
    return TestClient(app, base_url="http://127.0.0.1:49813"), engine


# ---- auth & validation (no model) -------------------------------------------------------------------
def test_control_requires_token(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    assert c.get("/control/status").status_code == 401
    body = c.get("/control/status", headers=CTL).json()
    assert body["server"]["mode"] == "standalone" and body["model"]["state"] == "empty"
    assert set(body["features"]) >= {"vision", "embeddings", "speech", "structured_output"}


def test_control_rejects_non_loopback(tmp_path):
    config = ServerConfig(models_dir=tmp_path / "m", data_dir=tmp_path, control_token="secret")
    c = TestClient(create_app(Engine.build(config, LogBuffer())))  # TestClient host isn't loopback
    assert c.get("/control/status", headers=CTL).status_code == 403


def test_legacy_api_key(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path, local=False, api_key="k")
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"authorization": "Bearer k"}).status_code == 200
    assert c.get("/health").status_code == 200


def test_named_keys_rate_limits_and_local_exemption(tmp_path):
    c, engine = client_for(tmp_path / "m", tmp_path, local=False)
    assert c.get("/v1/models").status_code == 200          # no keys yet: open
    created = c.post("/control/keys", headers=CTL, json={"name": "phone", "rpm_limit": 2}).json()
    secret, key_id = created["secret"], created["key"]["id"]
    assert "hash" not in created["key"]
    assert c.get("/v1/models").status_code == 401          # keys exist: required off-machine
    auth = {"authorization": f"Bearer {secret}"}
    assert c.get("/v1/models", headers=auth).status_code == 200
    assert c.get("/api/tags", headers=auth).status_code == 200
    r = c.get("/v1/models", headers=auth)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.post(f"/control/keys/{key_id}/revoke", headers=CTL).status_code == 200
    assert c.get("/v1/models", headers=auth).status_code == 200  # revoked key = no active keys = open again
    local, _ = client_for(tmp_path / "m", tmp_path, local=True)
    assert local.get("/v1/models").status_code == 200       # this Mac is exempt by default


def test_validation_and_missing_model_errors(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    r = c.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 400 and r.json()["error"]["param"] == "messages"
    assert c.post("/v1/chat/completions", content=b"{not json",
                  headers={"content-type": "application/json"}).status_code == 400
    assert c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 503
    r = c.post("/v1/chat/completions", json={"model": "a/b", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    assert c.post("/control/models/load", headers=CTL, json={"repo": "a/b"}).status_code == 404
    assert c.post("/control/downloads", headers=CTL, json={"repo": "../x"}).status_code == 400
    assert c.patch("/control/settings", headers=CTL, json={"batching": "x"}).status_code == 400
    history = c.get("/control/requests", headers=CTL).json()
    assert any(r["status"] == 503 for r in history["requests"])     # failures are recorded too


def test_presets_and_settings_endpoints(tmp_path):
    c, engine = client_for(tmp_path / "m", tmp_path)
    r = c.put("/control/presets", headers=CTL, json={"name": "Terse", "system_prompt": "Be terse.", "temperature": 0})
    assert r.status_code == 200
    assert [p["name"] for p in c.get("/control/presets", headers=CTL).json()["presets"]] == ["Terse"]
    assert any(m["id"] == "preset:Terse" for m in c.get("/v1/models").json()["data"])
    r = c.patch("/control/settings", headers=CTL, json={"idle_unload_minutes": 0, "bonjour": False})
    assert r.json()["restart_required"] == ["bonjour"] and engine.runtime.settings.idle_unload_minutes == 0
    assert c.delete("/control/presets/Terse", headers=CTL).status_code == 200


def test_ollama_basics_without_model(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    assert c.get("/").text == "Ollama is running"
    assert c.get("/api/version").json()["version"]
    assert c.get("/api/tags").json() == {"models": []}
    assert c.get("/api/ps").json() == {"models": []}
    c.patch("/control/settings", headers=CTL, json={"ollama_api": False})
    assert c.get("/api/tags").status_code == 404


# ---- real model ------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if not have(TEST_MODEL):
        pytest.skip("set BRIDLE_TEST_MODELS_DIR (needs the test model)")
    c, engine = client_for(MODELS_DIR, tmp_path_factory.mktemp("data"))
    engine.runtime.load(__import__("bridle_engine.domain", fromlist=["RepoId"]).RepoId.parse(TEST_MODEL), force=True)
    return c, engine


def chat(c, content, **kw):
    body = {"messages": [{"role": "user", "content": content}], "temperature": 0, "max_tokens": 6, **kw}
    r = c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@needs_model
def test_real_generation_streaming_and_history(live):
    c, engine = live
    body = chat(c, "What is the capital of France? One word.")
    assert "paris" in body["choices"][0]["message"]["content"].lower()
    with c.stream("POST", "/v1/chat/completions", json={
            "temperature": 0, "max_tokens": 4, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "Say hi."}]}) as s:
        lines = [l for l in s.iter_lines() if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert any(ch.get("usage") for ch in chunks)
    rows = c.get("/control/requests", headers=CTL).json()["requests"]
    assert rows[0]["model"] == TEST_MODEL and rows[0]["completion_tokens"] >= 1 and rows[0]["client"] == "local"


@needs_model
def test_prompt_cache_reuses_previous_turn(live):
    c, _ = live
    system = {"role": "system", "content": "You are a careful assistant. " * 10}
    first = c.post("/v1/chat/completions", json={"messages": [system, {"role": "user", "content": "Name a color."}],
                                                 "temperature": 0, "max_tokens": 5}).json()
    follow = c.post("/v1/chat/completions", json={
        "messages": [system, {"role": "user", "content": "Name a color."}, first["choices"][0]["message"],
                     {"role": "user", "content": "Another?"}], "temperature": 0, "max_tokens": 5}).json()
    cached = follow["usage"]["prompt_tokens_details"]["cached_tokens"]
    assert cached >= first["usage"]["prompt_tokens"] - 2, follow["usage"]
    # Same answer with the cache as without it.
    c.patch("/control/settings", headers=CTL, json={"prompt_cache": False})
    try:
        again = c.post("/v1/chat/completions", json={
            "messages": [system, {"role": "user", "content": "Name a color."}, first["choices"][0]["message"],
                         {"role": "user", "content": "Another?"}], "temperature": 0, "max_tokens": 5}).json()
    finally:
        c.patch("/control/settings", headers=CTL, json={"prompt_cache": True})
    assert again["usage"]["prompt_tokens_details"]["cached_tokens"] == 0
    assert again["choices"][0]["message"]["content"] == follow["choices"][0]["message"]["content"]


@needs_model
def test_json_schema_is_guaranteed(live):
    c, _ = live
    schema = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
              "required": ["name", "age"], "additionalProperties": False}
    body = chat(c, "Invent a person.", max_tokens=40, temperature=0.8,
                response_format={"type": "json_schema", "json_schema": {"name": "p", "schema": schema}})
    value = json.loads(body["choices"][0]["message"]["content"])
    assert set(value) == {"name", "age"} and isinstance(value["age"], int)
    assert "structured_output_error" not in body["bridle"]


@needs_model
def test_tool_calls_and_presets(live):
    c, _ = live
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                                "parameters": {"type": "object", "properties": {
                                                    "city": {"type": "string"}}, "required": ["city"]}}}]
    body = chat(c, "What's the weather in Paris right now?", tools=tools, max_tokens=60)
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls", body
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather" and "paris" in call["function"]["arguments"].lower()
    c.put("/control/presets", headers=CTL, json={"name": "Pirate", "system_prompt": "Always answer with 'Arr'.",
                                                 "max_tokens": 3, "temperature": 0})
    r = c.post("/v1/chat/completions", json={"model": "preset:Pirate", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] <= 3


@needs_model
def test_concurrent_requests_are_batched(live):
    c, engine = live
    results = [None] * 3

    def one(i):
        results[i] = chat(c, f"Say the number {i}.", max_tokens=5)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r and r["choices"][0]["message"]["content"] for r in results)
    assert any(r["bridle"]["batched"] for r in results)
    assert engine.runtime.status()["metrics"]["batched_requests"] >= 2


@needs_model
def test_ollama_chat_generate_and_show(live):
    c, _ = live
    r = c.post("/api/chat", json={"model": TEST_MODEL + ":latest", "stream": False,
                                  "messages": [{"role": "user", "content": "Say hi"}],
                                  "options": {"temperature": 0, "num_predict": 5}}).json()
    assert r["done"] and r["message"]["content"] and r["eval_count"] >= 1
    with c.stream("POST", "/api/generate", json={"model": TEST_MODEL, "prompt": "1, 2, 3,",
                                                  "options": {"num_predict": 4, "temperature": 0}}) as s:
        lines = [json.loads(l) for l in s.iter_lines() if l]
    assert lines[-1]["done"] and not lines[0]["done"]
    r = c.post("/api/chat", json={"model": TEST_MODEL, "stream": False, "format": "json",
                                  "messages": [{"role": "user", "content": "Give a JSON object with a key a."}],
                                  "options": {"num_predict": 20}}).json()
    json.loads(r["message"]["content"])
    show = c.post("/api/show", json={"model": TEST_MODEL}).json()
    assert "completion" in show["capabilities"] and "tools" in show["capabilities"]
    assert c.get("/api/ps").json()["models"][0]["name"].startswith(TEST_MODEL)


@needs_model
def test_benchmark_memory_guard_and_idle_reload(live, monkeypatch):
    c, engine = live
    r = c.post("/control/benchmarks", headers=CTL, json={"prompt_tokens": 32, "generation_tokens": 16})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["generated_tokens"] == 16 and result["generation_tps"] > 0
    assert c.get("/control/benchmarks", headers=CTL).json()["benchmarks"][0]["id"] == result["id"]

    import bridle_engine.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "gpu_memory_budget", lambda total: 1)  # pretend a tiny GPU
    r = c.post("/control/models/load", headers=CTL, json={"repo": TEST_MODEL})
    assert r.status_code == 409 and r.json()["error"]["check"]["verdict"] == "too_large"
    monkeypatch.undo()

    engine.runtime.unload(reason="idle")
    assert engine.runtime.status()["unloaded_reason"] == "idle"
    body = chat(c, "Say ok.", max_tokens=3)        # auto-reloads the idle-unloaded model
    assert body["model"] == TEST_MODEL and engine.runtime.loaded is not None


@needs_model
def test_compare_runs_each_model_and_restores(live):
    c, engine = live
    r = c.post("/control/compare", headers=CTL, json={"models": [TEST_MODEL, TEST_MODEL], "prompt": "Say hi.",
                                                      "max_tokens": 4, "temperature": 0})
    assert r.status_code == 202
    job_id = r.json()["id"]
    import time
    for _ in range(240):
        job = c.get(f"/control/compare/{job_id}", headers=CTL).json()
        if job["state"] != "running":
            break
        time.sleep(0.5)
    assert job["state"] == "done", job
    assert all(run["state"] == "done" and run["text"] for run in job["runs"])
    assert job["runs"][0]["text"] == job["runs"][1]["text"]      # same model, temperature 0
    assert engine.runtime.loaded.repo == TEST_MODEL


@pytest.mark.skipif(not have(EMBED_MODEL), reason="needs the embedding test model")
def test_embeddings_endpoint(live):
    c, _ = live
    r = c.post("/v1/embeddings", json={"model": EMBED_MODEL, "input": ["a cat", "a kitten", "tax law"]}).json()
    v = [d["embedding"] for d in r["data"]]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert len(v[0]) == 384 and dot(v[0], v[1]) > dot(v[0], v[2])
    assert c.post("/v1/embeddings", json={"model": TEST_MODEL, "input": "x"}).status_code == 404
