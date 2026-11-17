import httpx
import pytest
from bridle_engine.catalog import ModelStore
from bridle_engine.domain import RepoId
from bridle_engine.downloads import DownloadManager, DownloadJob, RemoteFile


def test_partial_name_is_separate_model(tmp_path):
    store = ModelStore(tmp_path)
    other = store.path_for(RepoId.parse('owner/model.partial'))
    other.mkdir(parents=True)
    (other / '.bridle.json').write_text('{}')
    assert not store.delete(RepoId.parse('owner/model'))
    assert other.exists()
    assert [m.repo for m in store.list()] == ['owner/model.partial']


@pytest.mark.parametrize('value', ['[]', 'null', '1'])
def test_invalid_config_does_not_break_catalog(tmp_path, value):
    p = tmp_path / 'owner/model'
    p.mkdir(parents=True)
    (p / '.bridle.json').write_text('{}')
    (p / 'config.json').write_text(value)
    assert ModelStore(tmp_path).list() == []


def test_nonhashed_resume_fetches_current_config(tmp_path):
    dest = tmp_path / 'config.json'
    dest.write_bytes(b'old')
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'new'))) as client:
        DownloadManager(ModelStore(tmp_path), token='')._fetch_once(client,
            'https://example.invalid/config.json', dest, RemoteFile('config.json',3,None),
            DownloadJob(RepoId.parse('a/b')), 0)
    assert dest.read_bytes() == b'new'


def test_chunked_body_is_bounded():
    import asyncio
    from starlette.requests import Request
    from starlette.exceptions import HTTPException
    from bridle_engine.api import body_json
    async def run():
        calls = 0
        async def receive():
            nonlocal calls
            calls += 1
            return {'type': 'http.request', 'body': b' ' * 1_000_000,
                    'more_body': calls < 4}
        with pytest.raises(HTTPException) as e:
            await body_json(Request({'type':'http', 'headers':[]}, receive))
        assert e.value.status_code == 413
    asyncio.run(run())


def test_remote_key_cannot_pull_or_delete_ollama_models(tmp_path):
    from test_api import client_for
    # local=False makes testclient non-loopback; a valid key still authenticates
    # for inference, but pull/delete are management operations.
    c, _ = client_for(tmp_path / 'm', tmp_path, local=False, api_key='k')
    auth = {'authorization': 'Bearer k'}
    assert c.post('/api/pull', headers=auth, json={'model': 'a/b'}).status_code == 403
    assert c.request('DELETE', '/api/delete', headers=auth, json={'model': 'a/b'}).status_code == 403
    assert c.get('/api/tags', headers=auth).status_code == 200


def test_local_ollama_pull_delete_require_a_key_or_control_token(tmp_path):
    from test_api import client_for, CTL
    c, _ = client_for(tmp_path / 'm', tmp_path, local=True)
    # Loopback alone no longer suffices for model management (audit F2).
    assert c.request('DELETE', '/api/delete', json={'model': 'a/b'}).status_code == 401
    assert c.post('/api/pull', json={'model': 'a/b'}).status_code == 401
    # With the control token the request reaches the handler: missing model -> 404.
    assert c.request('DELETE', '/api/delete', headers=CTL, json={'model': 'a/b'}).status_code == 404
    # A named API key works too.
    secret = c.post('/control/keys', headers=CTL, json={'name': 'mgr'}).json()['secret']
    auth = {'authorization': f'Bearer {secret}'}
    assert c.request('DELETE', '/api/delete', headers=auth, json={'model': 'a/b'}).status_code == 404


def test_ollama_body_is_bounded(tmp_path):
    from test_api import client_for
    c, _ = client_for(tmp_path / 'm', tmp_path)
    big = b'{"model":"a/b","prompt":"' + b'x' * (2 * 1024 * 1024) + b'"}'
    assert c.post('/api/generate', content=big,
                  headers={'content-type': 'application/json'}).status_code == 413


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/secret.png',
    'http://169.254.169.254/latest/meta-data/',
    'http://[::1]/secret.png',
    'http://localhost/secret.png',
    'http://10.0.0.5/secret.png',
    'file:///etc/passwd',
    'http://user:pass@8.8.8.8/secret.png',
])
def test_fetch_images_blocks_non_public_urls(tmp_path, url):
    from bridle_engine.vision import fetch_images
    with pytest.raises(ValueError):
        fetch_images((url,), tmp_path)


def test_fetch_images_uses_pinned_validated_ip(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from bridle_engine import vision

    class Handler(BaseHTTPRequestHandler):
        seen_host = None
        def log_message(self, *args):
            pass
        def do_GET(self):
            Handler.seen_host = self.headers.get('Host')
            if self.path == '/redir':
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1/private.png')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Length', '4')
            self.end_headers()
            self.wfile.write(b'img!')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    real_resolve = vision._resolve_public_ip
    # Only the fake public host is pinned to the local test server; anything
    # else (the redirect target) goes through the real public-address check.
    def resolve(url):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname == 'public.example':
            return ('http', 'public.example', port, '127.0.0.1')
        return real_resolve(url)
    monkeypatch.setattr(vision, '_resolve_public_ip', resolve)
    try:
        vision.fetch_images((f'http://public.example:{port}/img.png',), tmp_path)
        assert (tmp_path / 'image_0').read_bytes() == b'img!'
        assert Handler.seen_host == f'public.example:{port}'
        with pytest.raises(ValueError):
            vision.fetch_images((f'http://public.example:{port}/redir',), tmp_path)
    finally:
        server.shutdown()


def test_fetch_images_data_url_still_works(tmp_path):
    import base64
    from bridle_engine.vision import fetch_images
    payload = base64.b64encode(b'png-bytes').decode()
    paths = fetch_images((f'data:image/png;base64,{payload}',), tmp_path)
    assert (tmp_path / 'image_0').read_bytes() == b'png-bytes'
    assert paths == [str(tmp_path / 'image_0')]


def test_response_format_schema_bounds():
    from bridle_engine.domain import ChatRequest, ParseError
    with pytest.raises(ParseError):
        ChatRequest.parse({'messages': [{'role': 'user', 'content': 'x'}],
                           'response_format': {'type': 'json_schema', 'json_schema': {
                               'name': 'x', 'schema': {'$ref': 'https://evil.example/schema.json'}}}})
    with pytest.raises(ParseError):
        ChatRequest.parse({'messages': [{'role': 'user', 'content': 'x'}],
                           'response_format': {'type': 'json_schema', 'json_schema': {
                               'name': 'x', 'schema': {'type': 'object', 'description': 'x' * 40_000}}}})
    ok = ChatRequest.parse({'messages': [{'role': 'user', 'content': 'x'}],
                            'response_format': {'type': 'json_schema', 'json_schema': {
                                'name': 'x', 'schema': {'$ref': '#/$defs/item'}}}})
    assert ok.response_format is not None


def test_embedding_input_length_is_bounded():
    from bridle_engine.domain import EmbeddingRequest, ParseError
    with pytest.raises(ParseError):
        EmbeddingRequest.parse({'model': 'a/b', 'input': 'x' * 32_769})
    assert EmbeddingRequest.parse({'model': 'a/b', 'input': 'short'}).inputs == ('short',)


def test_key_reserve_and_settle_bounds_concurrent_use(tmp_path):
    from bridle_engine.keys import KeyStore, RateLimited
    store = KeyStore(tmp_path / 'keys.json')
    key, secret = store.create('laptop', daily_token_limit=100)
    assert store.reserve(key, 80) == 80
    # Second concurrent hold is capped at the remaining allowance, then the gate closes.
    assert store.reserve(key, 30) == 20
    with pytest.raises(RateLimited):
        store.reserve(key, 10)
    store.settle_tokens(key.id, 20, 20)
    store.settle_tokens(key.id, 80, 50)
    stored = store.list()[0]
    assert stored.tokens_today == 70 and stored.tokens_total == 70
    # A key without a daily limit never reserves.
    plain, _ = store.create('plain')
    assert store.reserve(plain, 10_000) == 0


def test_key_with_limit_below_max_tokens_can_still_request(tmp_path):
    from bridle_engine.keys import KeyStore
    store = KeyStore(tmp_path / 'keys.json')
    key, _ = store.create('small', daily_token_limit=500)
    # Default chat max_tokens is 1024; the hold caps at the 500 remaining.
    assert store.reserve(key, 1024) == 500
    store.settle_tokens(key.id, 500, 12)
    assert store.list()[0].tokens_today == 12
    assert store.reserve(key, 1024) == 488


def test_key_settle_after_midnight_counts_actual_today(tmp_path):
    from bridle_engine.keys import KeyStore
    store = KeyStore(tmp_path / 'keys.json')
    key, _ = store.create('edge', daily_token_limit=100)
    assert store.reserve(key, 80) == 80
    key.usage_day = '2000-01-01'
    store.settle_tokens(key.id, 80, 50)
    assert key.tokens_today == 50 and key.tokens_total == 50
    assert key.usage_day != '2000-01-01'


def test_remote_key_without_load_permission_gets_403_for_unloaded_model(tmp_path):
    from test_api import client_for
    c, engine = client_for(tmp_path / 'm', tmp_path, local=False)
    # Create keys directly on the store: /control is loopback-only and this
    # harness marks testclient as non-loopback.
    key, secret = engine.keys.create('phone')
    assert key.allow_model_load is False
    auth = {'authorization': f'Bearer {secret}'}
    r = c.post('/v1/chat/completions', headers=auth,
               json={'model': 'a/b', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r.status_code == 403 and 'may not load models' in r.json()['error']['message']
    # The same key may still call with no model when one is resident is covered
    # by can_load_model logic; here nothing is resident, so 403 stands.
    granted, secret2 = engine.keys.create('owner-remote', allow_model_load=True)
    r2 = c.post('/v1/chat/completions', headers={'authorization': f'Bearer {secret2}'},
                json={'model': 'a/b', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert r2.status_code == 404  # passed the load gate; model simply not downloaded


def test_corrupt_stores_are_preserved_and_reported(tmp_path):
    from bridle_engine.keys import KeyStore
    from bridle_engine.presets import PresetStore
    from bridle_engine.settings import SettingsStore
    for cls, name in ((KeyStore, 'api_keys.json'), (PresetStore, 'presets.json'),
                      (SettingsStore, 'settings.json')):
        path = tmp_path / name
        path.write_text('{not json')
        store = cls(path)
        assert store.load_error
        assert not path.exists()
        assert list(tmp_path.glob(name + '.corrupt-*'))
        (tmp_path / (list(tmp_path.glob(name + '.corrupt-*'))[0].name)).unlink()


def test_launch_file_secrets_are_read_once_and_deleted(tmp_path, monkeypatch):
    import json as _json
    from bridle_engine.__main__ import _launch_secrets
    path = tmp_path / 'engine-launch.json'
    path.write_text(_json.dumps({'control_token': 'tok', 'api_key': 'key'}))
    path.chmod(0o600)
    monkeypatch.setenv('BRIDLE_LAUNCH_FILE', str(path))
    assert _launch_secrets() == {'control_token': 'tok', 'api_key': 'key'}
    assert not path.exists()
    assert _launch_secrets() == {}
    monkeypatch.delenv('BRIDLE_LAUNCH_FILE', raising=False)
    assert _launch_secrets() == {}


def test_vision_hint_points_at_in_app_install():
    from bridle_engine.vision import INSTALL_HINT
    assert "Models" in INSTALL_HINT and "mlx-vlm" in INSTALL_HINT


def test_benchmark_store_writes_atomically(tmp_path):
    import json as _json
    from bridle_engine.jobs import BenchmarkResult, BenchmarkStore
    store = BenchmarkStore(tmp_path / 'benchmarks.json')
    store.add(BenchmarkResult('id1', 'a/b', None, 'chip', 16, 16, 1.0, 2.0, 3, 0.5, 1.0, 1.0))
    rows = _json.loads((tmp_path / 'benchmarks.json').read_text())
    assert rows[0]['id'] == 'id1' and store.list()[0]['repo'] == 'a/b'
    assert not (tmp_path / 'benchmarks.tmp').exists()


def test_shared_plaintext_inference_is_rejected(tmp_path):
    from test_api import client_for
    c, _ = client_for(tmp_path/'m', tmp_path, local=False, host='0.0.0.0')
    assert c.get('/v1/models').status_code == 426
    assert c.get('/health').status_code == 200


def test_inference_admission_rejects_overload_and_recovers(tmp_path):
    import asyncio
    from test_api import client_for
    async def run():
        c, _ = client_for(tmp_path/'m', tmp_path)
        entered = 0
        release = asyncio.Event()
        @c.app.get('/v1/slow')
        async def slow():
            nonlocal entered
            entered += 1
            await release.wait()
            return {'ok': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url='http://127.0.0.1:49813') as client:
            pending = [asyncio.create_task(client.get('/v1/slow')) for _ in range(16)]
            try:
                async with asyncio.timeout(5):
                    while entered < 16:
                        await asyncio.sleep(0.001)
                assert (await client.get('/v1/models')).status_code == 429
                assert (await client.get('/health')).status_code == 200
            finally:
                release.set()
                await asyncio.gather(*pending)
            assert (await client.get('/v1/models')).status_code == 200
    asyncio.run(run())


# ---- audit fixes (2026-11-12) ------------------------------------------------

def test_daily_retry_after_matches_local_midnight(tmp_path):
    import time as _t
    from datetime import date as _date, datetime as _dt, timedelta as _td
    from bridle_engine.keys import KeyStore, RateLimited

    store = KeyStore(tmp_path / "keys.json")
    key, secret = store.create("daily", daily_token_limit=5)
    assert store.authenticate(secret) is not None
    store.reserve(key, 5)  # exhaust the day's allowance
    with pytest.raises(RateLimited) as err:
        store.admit(key)
    tomorrow = _date.today() + _td(days=1)
    expected = max(1, int(_dt(tomorrow.year, tomorrow.month, tomorrow.day).timestamp() - _t.time()))
    assert abs(err.value.retry_after - expected) <= 2


def test_new_keys_are_salted_and_verify(tmp_path):
    from bridle_engine.keys import KeyStore

    store = KeyStore(tmp_path / "keys.json")
    key, secret = store.create("mac")
    assert key.hash.startswith("s1$") and secret not in key.hash
    assert store.authenticate(secret) is not None
    assert store.authenticate("brdl_wrong") is None


def test_legacy_unsalted_key_is_upgraded_on_use(tmp_path):
    import hashlib as _h
    import json as _json
    from dataclasses import asdict as _asdict
    from bridle_engine.keys import ApiKey, KeyStore

    secret = "brdl_legacy123"
    legacy = ApiKey(id="x", name="old", hint="\u2026",
                    hash=_h.sha256(secret.encode()).hexdigest(), created_at=0.0)
    path = tmp_path / "keys.json"
    path.write_text(_json.dumps([_asdict(legacy)]))
    store = KeyStore(path)
    found = store.authenticate(secret)
    assert found is not None and found.hash.startswith("s1$")
    store.flush()
    reloaded = KeyStore(path)
    assert reloaded.authenticate(secret) is not None
    assert reloaded.authenticate("brdl_other") is None


# ---- 2026-11-16 audit H1: web pages must not drive the local API (CSRF / DNS rebinding) --------
def _pull_probe_client(tmp_path, **cfg):
    from test_api import client_for
    c, engine = client_for(tmp_path / "m", tmp_path, **cfg)
    started = []
    engine.downloads.start = lambda repo: started.append(str(repo))
    return c, started


def test_cross_site_simple_post_cannot_start_a_download(tmp_path):
    c, started = _pull_probe_client(tmp_path)
    r = c.post("/api/pull", content=b'{"model":"mlx-community/some-huge-model","stream":false}',
               headers={"content-type": "text/plain;charset=UTF-8", "origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_origin"
    assert started == []
    assert c.post("/v1/chat/completions", json={}, headers={"origin": "null"}).status_code == 403


def test_dns_rebinding_host_is_refused_on_loopback(tmp_path):
    c, started = _pull_probe_client(tmp_path)
    rebound = {"host": "attacker-rebind.example:49813"}
    assert c.get("/v1/models", headers=rebound).status_code == 403
    assert c.post("/api/pull", json={"model": "a/b"}, headers=rebound).status_code == 403
    assert c.request("DELETE", "/api/delete", json={"model": "a/b"}, headers=rebound).status_code == 403
    assert c.get("/control/status", headers={**rebound, "authorization": "Bearer secret"}).status_code == 403
    assert started == []


def test_normal_local_clients_still_work(tmp_path):
    c, _ = _pull_probe_client(tmp_path)
    for host in ("127.0.0.1:49813", "localhost:49813", "[::1]:49813", "localhost"):
        assert c.get("/v1/models", headers={"host": host}).status_code == 200, host
    # Same-machine browser origins (e.g. a dev UI on localhost) are fine.
    assert c.get("/v1/models", headers={"origin": "http://localhost:3000"}).status_code == 200


def test_allowed_origins_opt_in(tmp_path):
    c, _ = _pull_probe_client(tmp_path, allowed_origins=("https://ui.example",))
    assert c.get("/v1/models", headers={"origin": "https://ui.example"}).status_code == 200
    assert c.get("/v1/models", headers={"origin": "https://other.example"}).status_code == 403


def test_hostname_parsing():
    from bridle_engine.api import _hostname
    assert _hostname("127.0.0.1:49813") == "127.0.0.1"
    assert _hostname("[::1]:49813") == "::1"
    assert _hostname("::1") == "::1"
    assert _hostname("http://LocalHost:3000/") == "localhost"
    assert _hostname("localhost.") == "localhost"
    assert _hostname("null") == "null"


def test_version_is_consistent_everywhere():
    """2026-11-16 audit M3: the build reads __version__, so it must match pyproject."""
    import re
    from pathlib import Path
    from bridle_engine import __version__
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == __version__


def test_tls_private_key_is_private(tmp_path):
    """2026-11-16 re-audit: the TLS key is created 0600, never briefly world-readable."""
    import stat
    from pathlib import Path
    from bridle_engine import tls
    _, key, _ = tls.ensure_certificate(tmp_path, "bridle-test.local", ["127.0.0.1"])
    assert stat.S_IMODE(Path(key).stat().st_mode) == 0o600
    # Regenerating over an existing key keeps it private too.
    (tmp_path / "cert.pem").unlink(missing_ok=True)
    for f in tmp_path.glob("*.pem"):
        if f != Path(key):
            f.unlink()
    _, key2, _ = tls.ensure_certificate(tmp_path, "other.local", ["127.0.0.2"])
    assert stat.S_IMODE(Path(key2).stat().st_mode) == 0o600


# ---- 2026-11-16 full-audit fixes (F1-F13) -------------------------------------------------
def test_non_json_content_type_is_refused_without_origin(tmp_path):
    """F5: a no-preflight text/plain POST must fail even with no Origin header."""
    c, started = _pull_probe_client(tmp_path)
    r = c.post("/api/pull", content=b'{"model":"a/b"}', headers={"content-type": "text/plain"})
    assert r.status_code == 415 and started == []
    assert c.post("/v1/chat/completions", content=b'{}',
                  headers={"content-type": "text/plain"}).status_code == 415
    # Proper JSON still works; body-less POSTs are exempt.
    assert c.post("/v1/chat/completions", json={"messages": []}).status_code == 400


def test_unknown_prefix_is_not_silently_public(tmp_path):
    """F5: deny by default outside the guarded prefixes."""
    from test_api import client_for
    c, _ = client_for(tmp_path / 'm', tmp_path)
    assert c.get("/unknown/thing").status_code == 404
    assert c.get("/health").status_code == 200
    assert c.get("/").status_code == 200


def test_audio_bad_temperature_is_400_not_500(tmp_path):
    """F6: malformed transcription temperature validates at the boundary."""
    from test_api import client_for
    c, _ = client_for(tmp_path / 'm', tmp_path)
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFF")},
               data={"model": "a/b", "temperature": "hot"})
    assert r.status_code == 400
    r = c.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFF")},
               data={"model": "a/b", "temperature": "nan"})
    assert r.status_code == 400


def test_audio_content_length_precheck(tmp_path):
    """F3: an oversized declared upload is refused before multipart parsing."""
    from test_api import client_for
    c, _ = client_for(tmp_path / 'm', tmp_path)
    r = c.post("/v1/audio/transcriptions", content=b"x",
               headers={"content-type": "multipart/form-data; boundary=x",
                        "content-length": str(300 * 1024 * 1024)})
    assert r.status_code == 400


def test_key_store_file_is_private_from_creation(tmp_path):
    """F8: api_keys.json lands at 0600 via mkstemp, not chmod-after-write."""
    import stat
    from bridle_engine.keys import KeyStore
    store = KeyStore(tmp_path / "api_keys.json")
    store.create("phone")
    assert stat.S_IMODE((tmp_path / "api_keys.json").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".api_keys-*"))  # temp file was replaced, not left behind


def test_corrupt_imports_registry_does_not_brick_listing(tmp_path):
    """F9: a corrupt .imports.json is quarantined; listing keeps working."""
    from bridle_engine.catalog import ModelStore
    from bridle_engine.imports import registry
    root = tmp_path / "models"
    root.mkdir()
    (root / ".imports.json").write_text("{not json")
    store = ModelStore(root)
    assert registry(store) == {}
    assert store.list() == []
    assert list(root.glob(".imports.json.corrupt-*"))


def test_grant_path_works_under_symlinked_prefix(tmp_path):
    """F10: grants under a symlinked prefix (e.g. /tmp -> /private/tmp) resolve."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "note.txt").write_text("hi")
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    from bridle_engine.operations import Operations
    ops = Operations.__new__(Operations)
    ops.state = {"grants": {"g1": {"path": str(link / "real"), "write": False}}}
    target = ops.grant_path({"grant_id": "g1", "path": "note.txt"})
    assert target.read_text() == "hi"
