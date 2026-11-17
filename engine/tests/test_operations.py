"""Operations behavior and permission boundaries without inference or network access."""
from dataclasses import dataclass
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from bridle_engine.catalog import ModelStore
from bridle_engine.keys import KeyStore
from bridle_engine.operations import register_operations_routes
from bridle_engine.settings import SettingsStore


@dataclass
class Sample:
    cpu_percent: float = 2


@pytest.fixture
def ops(tmp_path):
    submitted = []
    engine = SimpleNamespace(
        config=SimpleNamespace(data_dir=tmp_path),
        runtime=SimpleNamespace(submit_generation=submitted.append, status=lambda: {"metrics": {"active": False}}),
        settings_store=SettingsStore(tmp_path / "settings.json"),
        sampler=SimpleNamespace(sample=lambda: Sample()),
        keys=KeyStore(tmp_path / "keys.json"),
        store=ModelStore(tmp_path / "models"),
        endpoint=lambda: "http://127.0.0.1:49813", tls_fingerprint="fingerprint")
    app = FastAPI()
    class FakeVault:
        storage = "test vault"
        def __init__(self):
            self.values = {}
        def put(self, identifier, secret):
            self.values[identifier] = secret
        def get(self, identifier):
            return self.values[identifier]
        def delete(self, identifier):
            self.values.pop(identifier, None)
    operations = register_operations_routes(app, engine, vault=FakeVault())
    return TestClient(app), operations, engine, submitted


def post(client, path, data=None):
    return client.post("/control/operations" + path, json=data or {})


def test_request_tracking_cancellation_and_completion(ops):
    client, operations, engine, submitted = ops
    emitted = []
    job = SimpleNamespace(lm=SimpleNamespace(repo="owner/model"), cancel=threading.Event(), emit=emitted.append)
    engine.runtime.submit_generation(job)
    first = client.get("/control/operations").json()["requests"][0]
    assert first["status"] == "queued"
    assert post(client, f'/requests/{first["id"]}/cancel').status_code == 200
    assert job.cancel.is_set()
    event = SimpleNamespace(text="hello", completion_tokens=2, generation_tps=12, peak_memory_gb=1)
    job.emit(event)
    job.emit(None)
    result = operations.snapshot()["requests"][0]
    assert result["status"] == "cancelled" and result["tokens"] == 2
    assert result["first_token_at"] and result["finished_at"]
    assert emitted == [event, None]
    assert post(client, f'/requests/{first["id"]}/cancel').status_code == 404


def test_failure_and_bounded_terminal_history(ops):
    _, operations, engine, _ = ops
    for _ in range(105):
        job = SimpleNamespace(lm=None, cancel=threading.Event(), emit=lambda item: None)
        engine.runtime.submit_generation(job)
        job.emit(RuntimeError("private prompt must not be persisted"))
        job.emit(None)
    records = operations.snapshot()["requests"]
    assert len(records) <= 101
    assert all(record["status"] == "failed" for record in records)
    assert "private prompt" not in json.dumps(records)


def test_profiles_apply_supported_settings(ops):
    client, operations, engine, _ = ops
    response = client.put("/control/operations/profile", json={"name": "quiet"})
    assert response.status_code == 200
    assert engine.settings_store.current.batching is False
    assert engine.settings_store.current.max_batch_size == 1
    assert response.json()["thermal_control"] is False
    assert client.put("/control/operations/profile", json={"name": "invented"}).status_code == 400
    assert operations.snapshot()["cockpit"]["power_watts"] is None


def test_granted_text_tools_require_unchanged_preview_and_revoke(ops, tmp_path):
    client, _, _, _ = ops
    folder = tmp_path / "workspace"
    folder.mkdir()
    (folder / "note.txt").write_text("before")
    grant = post(client, "/grants", {"path": str(folder), "write": True}).json()
    data = {"grant_id": grant["id"], "path": "note.txt", "content": "after"}
    assert post(client, "/tools/read", data).json()["content"] == "before"
    assert post(client, "/tools/commit-write", data).status_code == 400
    approval = post(client, "/tools/preview-write", data).json()["approval"]
    altered = dict(data, content="different", approval=approval)
    assert post(client, "/tools/commit-write", altered).status_code == 409
    approval = post(client, "/tools/preview-write", data).json()["approval"]
    assert post(client, "/tools/commit-write", dict(data, approval=approval)).status_code == 200
    assert (folder / "note.txt").read_text() == "after"
    assert post(client, "/tools/commit-write", dict(data, approval=approval)).status_code == 409
    assert post(client, "/tools/read", dict(data, path="../keys.json")).status_code == 400
    (folder / "link.txt").symlink_to(tmp_path / "keys.json")
    assert post(client, "/tools/read", dict(data, path="link.txt")).status_code == 403
    client.delete("/control/operations/grants/" + grant["id"])
    assert post(client, "/tools/read", data).status_code == 403


def test_readonly_and_expired_write_approval(ops, tmp_path):
    client, operations, _, _ = ops
    grant = post(client, "/grants", {"path": str(tmp_path)}).json()
    data = {"grant_id": grant["id"], "path": "new.txt", "content": "x"}
    assert post(client, "/tools/preview-write", data).status_code == 403
    assert post(client, "/tools/read", dict(data, path="keys.exe")).status_code == 400


def test_pairing_is_host_confirmed_single_use_hashed_and_revocable(ops):
    client, operations, engine, _ = ops
    assert post(client, "/pairing/confirm", {"name": "phone", "code": "made-up"}).status_code == 403
    code = post(client, "/pairing").json()["code"]
    assert code not in operations.codes
    paired = post(client, "/pairing/confirm", {"name": "phone", "code": code}).json()
    assert engine.keys.authenticate(paired["secret"])
    assert paired["secret"] not in engine.keys.path.read_text()
    assert paired["secret"] not in operations.path.read_text()
    assert post(client, "/pairing/confirm", {"name": "phone", "code": code}).status_code == 403
    assert client.delete("/control/operations/devices/" + paired["device"]["id"]).status_code == 200
    assert engine.keys.authenticate(paired["secret"]) is None


def test_client_secret_created_once(ops):
    client, _, engine, _ = ops
    result = post(client, "/clients", {"name": "editor", "kind": "continue"}).json()
    assert result["config"]["base_url"].endswith("/v1")
    assert engine.keys.authenticate(result["secret"])
    assert "hash" not in result["key"]
    assert result["health"]["generation_tested"] is False


def test_mcp_requires_explicit_endpoint_and_explicit_call(ops, monkeypatch):
    client, _, _, _ = ops
    calls = []
    async def network(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        return {"jsonrpc": "2.0", "result": {"tools": []}}
    monkeypatch.setattr("bridle_engine.operations.bounded_json", network)
    assert post(client, "/mcp", {"name": "bad", "url": "http://remote.example"}).status_code == 400
    result = post(client, "/mcp", {"name": "local", "url": "http://127.0.0.1:1234/mcp"}).json()
    assert not calls
    response = post(client, f'/mcp/{result["id"]}/call', {"name": "hello", "arguments": {"x": 1}})
    assert response.status_code == 200
    assert calls[0][1]["method"] == "tools/call"
    assert calls[0][1]["params"] == {"name": "hello", "arguments": {"x": 1}}
    client.delete("/control/operations/mcp/" + result["id"])
    assert post(client, f'/mcp/{result["id"]}/tools').status_code == 404


def test_peer_requires_certificate_and_route_is_nonstreaming(ops, monkeypatch):
    client, operations, _, _ = ops
    assert post(client, "/peers", {"name": "peer", "url": "https://peer", "api_key": "secret", "ca_pem": "junk"}).status_code == 400
    operations.state["peers"]["approved"] = {"id": "approved", "url": "https://peer", "api_key": "secret", "ca_pem": "approved-pem"}
    calls = []
    async def network(url, payload, **kwargs):
        calls.append((url, payload, kwargs))
        return {"choices": []}
    monkeypatch.setattr("bridle_engine.operations.bounded_json", network)
    assert post(client, "/route", {"peer_id": "unknown", "request": {"messages": []}}).status_code == 403
    assert post(client, "/route", {"peer_id": "approved", "request": {"messages": [], "stream": True}}).status_code == 400
    assert not calls
    assert post(client, "/route", {"peer_id": "approved", "request": {"messages": []}}).status_code == 200
    assert calls[0][2]["ca_pem"] == "approved-pem"
    assert calls[0][2]["headers"] == {"Authorization": "Bearer secret"}
    assert "secret" not in json.dumps(operations.snapshot())


def test_static_health_reports_incomplete_model(ops):
    client, _, _, _ = ops
    result = post(client, "/health", {"repo": "owner/model"}).json()
    assert result["level"] == "static" and result["ok"] is False
    assert {check["name"] for check in result["checks"]} == {"download_manifest", "config", "weights"}


def healthy_model(engine):
    path = engine.store.root / "owner" / "model"
    path.mkdir(parents=True)
    (path / ".bridle.json").write_text(json.dumps({"repo": "owner/model", "size_bytes": 16}))
    (path / "config.json").write_text('{"model_type":"test"}')
    header = json.dumps({"test": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}}).encode()
    (path / "model.safetensors").write_bytes(len(header).to_bytes(8, "little") + header + b"x")
    return path


def test_health_explicit_generation_loads_only_after_file_checks(ops):
    client, _, engine, _ = ops
    calls = []
    engine.runtime.load = lambda repo: (calls.append(str(repo)) or SimpleNamespace(kind="text"))
    def generate(loaded, prompt, params, cancel, **kwargs):
        assert params.max_tokens == 8
        assert not cancel.is_set()
        yield SimpleNamespace(completion_tokens=8)
    engine.runtime.generate = generate
    assert post(client, "/health", {"repo": "owner/model", "generate": True}).json()["ok"] is False
    assert not calls
    path = healthy_model(engine)
    assert post(client, "/health", {"repo": "owner/model"}).json()["ok"] is True
    assert not calls
    result = post(client, "/health", {"repo": "owner/model", "generate": True}).json()
    assert result["ok"] and result["level"] == "generation"
    assert calls == ["owner/model"]
    (path / "model.safetensors").write_bytes(b"corrupt weights")
    assert post(client, "/health", {"repo": "owner/model"}).json()["ok"] is False


def test_mcp_initialization_preserves_session_and_sends_notification(ops, monkeypatch):
    client, _, _, _ = ops
    calls = []
    async def network(url, payload, **kwargs):
        calls.append((payload, kwargs))
        if payload.get("method") == "initialize":
            kwargs["response_headers"]["mcp-session-id"] = "session-secret"
        return {"jsonrpc": "2.0", "result": {}}
    monkeypatch.setattr("bridle_engine.operations.bounded_json", network)
    connection = post(client, "/mcp", {"name": "test", "url": "http://localhost:1234/mcp"}).json()
    assert post(client, f'/mcp/{connection["id"]}/initialize').status_code == 200
    assert [payload["method"] for payload, _ in calls] == ["initialize", "notifications/initialized"]
    assert calls[1][1]["headers"]["Mcp-Session-Id"] == "session-secret"
    assert post(client, f'/mcp/{connection["id"]}/tools').status_code == 200
    assert calls[-1][1]["headers"]["Mcp-Session-Id"] == "session-secret"
    assert "session-secret" not in json.dumps(client.get("/control/operations").json())


def test_bounded_transport_uses_verified_tls_no_proxy_or_redirect_and_sse(monkeypatch):
    import asyncio
    import httpx
    import ssl
    from bridle_engine.operations import bounded_json
    original = httpx.AsyncClient
    options = []
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"jsonrpc":"2.0","id":"one","result":{"tools":[]}}\n\n'
            raise AssertionError("must return matching event before reading forever")
    async def responder(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": "session"}, stream=Events())
    def factory(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(responder), **kwargs)
    monkeypatch.setattr("bridle_engine.operations.httpx.AsyncClient", factory)
    headers = {}
    result = asyncio.run(bounded_json("https://configured.example/mcp", {"id": "one"}, response_headers=headers))
    assert result["result"] == {"tools": []}
    assert headers["mcp-session-id"] == "session"
    assert options[0]["verify"].verify_mode == ssl.CERT_REQUIRED
    assert options[0]["verify"].check_hostname
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_bounded_transport_rejects_large_response(monkeypatch):
    import asyncio
    import httpx
    from fastapi import HTTPException
    from bridle_engine.operations import bounded_json, LIMIT
    original = httpx.AsyncClient
    async def responder(request):
        return httpx.Response(200, content=b"x" * (LIMIT + 1))
    monkeypatch.setattr("bridle_engine.operations.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(responder), **kwargs))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(bounded_json("https://configured.example/mcp", {"id": "one"}))
    assert caught.value.status_code == 502


def test_mcp_disable_blocks_calls_and_operations_file_is_private(ops):
    client, operations, _, _ = ops
    connection = post(client, "/mcp", {"name": "test", "url": "http://localhost:1234/mcp"}).json()
    assert operations.path.stat().st_mode & 0o777 == 0o600
    result = client.put("/control/operations/mcp/" + connection["id"], json={"enabled": False})
    assert result.status_code == 200 and result.json()["enabled"] is False
    assert post(client, f'/mcp/{connection["id"]}/call', {"name": "anything"}).status_code == 404


@pytest.mark.parametrize("candidate,status", [
    ({"action": "read", "path": "note.txt"}, 200),
    ({"action": "preview-write", "path": "note.txt", "content": "proposed only"}, 200),
    ({"action": "commit-write", "path": "note.txt", "content": "bad"}, 422),
    ({"action": "shell", "path": "."}, 422),
    ({"action": "read", "path": "../secret.txt"}, 400),
    ({"action": "read", "path": "/etc/passwd"}, 400),
])
def test_model_tool_proposals_never_execute(ops, tmp_path, candidate, status):
    client, operations, engine, _ = ops
    folder = tmp_path / "granted"
    folder.mkdir()
    (folder / "note.txt").write_text("unchanged")
    grant = post(client, "/grants", {"path": str(folder), "write": True}).json()
    engine.runtime.loaded = SimpleNamespace(kind="text", repo="owner/model")
    engine.runtime.chat_prompt = lambda loaded, messages, **kwargs: "prompt"
    def generate(loaded, prompt, params, cancel, **kwargs):
        assert params.max_tokens == 512 and kwargs["response_format"].type == "json_schema"
        yield SimpleNamespace(text=json.dumps(candidate))
    engine.runtime.generate = generate
    result = post(client, "/tools/propose", {"grant_id": grant["id"], "task": "do something with note.txt"})
    assert result.status_code == status
    if status == 200:
        assert result.json()["requires_approval"] is True
        assert result.json()["executed"] is False
        assert result.json()["proposal"]["grant_id"] == grant["id"]
    assert (folder / "note.txt").read_text() == "unchanged"
    assert not operations.approvals


def test_tool_propose_requires_model_and_writable_grant(ops, tmp_path):
    client, _, engine, _ = ops
    grant = post(client, "/grants", {"path": str(tmp_path), "write": False}).json()
    assert post(client, "/tools/propose", {"grant_id": grant["id"], "task": "read"}).status_code == 409
    engine.runtime.loaded = SimpleNamespace(kind="text", repo="owner/model")
    engine.runtime.chat_prompt = lambda *args, **kwargs: "prompt"
    engine.runtime.generate = lambda *args, **kwargs: iter([SimpleNamespace(text='{"action":"preview-write","path":"x.txt","content":"x"}')])
    assert post(client, "/tools/propose", {"grant_id": grant["id"], "task": "write"}).status_code == 403
    engine.runtime.generate = lambda *args, **kwargs: iter([SimpleNamespace(text="invalid JSON")])
    assert post(client, "/tools/propose", {"grant_id": grant["id"], "task": "read"}).status_code == 422


def test_peer_registration_stores_secret_in_injected_vault_only(ops):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone
    client, operations, _, _ = ops
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "peer.local")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    result = post(client, "/peers", {"name": "peer", "url": "https://peer.local", "api_key": "vault-secret", "ca_pem": pem})
    assert result.status_code == 200
    identifier = result.json()["id"]
    assert operations.vault.values[identifier] == "vault-secret"
    assert "vault-secret" not in operations.path.read_text()
    assert "vault-secret" not in json.dumps(client.get("/control/operations").json())
    assert client.delete("/control/operations/peers/" + identifier).status_code == 200
    assert identifier not in operations.vault.values


def test_vault_is_private_file_on_every_platform(tmp_path):
    from bridle_engine.operations import CredentialVault
    vault = CredentialVault(tmp_path)
    assert vault.storage == "private 0600 file"
    vault.put("peer", "secret")
    assert vault.path.stat().st_mode & 0o777 == 0o600
    assert vault.get("peer") == "secret"
    vault.delete("peer")
    assert json.loads(vault.path.read_text()) == {}
