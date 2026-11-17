"""Aria: action parsing, state grounding, and control routes with a fake slot."""
from __future__ import annotations

import pytest

from bridle_engine.aria import parse_action, state_summary, tidy_reply, validate_messages
from bridle_engine.domain import ParseError

KNOWN = {"mlx-community/Qwen3-4B-4bit", "mlx-community/Qwen3-8B-4bit"}
DOWNLOADED = {"mlx-community/Qwen3-4B-4bit"}


def test_parse_action_accepts_catalog_download_and_downloaded_load():
    text, action = parse_action(
        'Download it.\n```aria-action\n{"kind": "download", "repo": "mlx-community/Qwen3-8B-4bit"}\n```',
        KNOWN, DOWNLOADED)
    assert text == "Download it." and action["kind"] == "download"
    text, action = parse_action(
        '```aria-action\n{"kind": "load", "repo": "mlx-community/Qwen3-4B-4bit"}\n```', KNOWN, DOWNLOADED)
    assert action["kind"] == "load" and text == ""


def test_parse_action_drops_unknown_or_invalid_proposals():
    for bad in ('{"kind": "download", "repo": "evil/model"}',
                '{"kind": "load", "repo": "mlx-community/Qwen3-8B-4bit"}',
                '{"kind": "delete", "repo": "mlx-community/Qwen3-4B-4bit"}',
                '{"kind": "open", "screen": "terminal"}',
                'not json'):
        _, action = parse_action(f'Try this.\n```aria-action\n{bad}\n```', KNOWN, DOWNLOADED)
        assert action is None, bad
    text, action = parse_action('Open it.\n```aria-action\n{"kind": "open", "screen": "models"}\n```',
                                KNOWN, DOWNLOADED)
    assert action == {"kind": "open", "screen": "models", "label": "Open Models"} and text == "Open it."


def test_state_summary_uses_live_facts():
    summary = state_summary(
        {"machine": {"chip": "Apple M4", "memory_total_bytes": 24_000_000_000},
         "server": {"state": "running"},
         "model": {"loaded": {"repo": "a/b"}}, "downloads": []},
        [{"repo": "a/b", "downloaded": True}, {"repo": "c/d", "downloaded": False}])
    assert "Apple M4" in summary and "24 GB" in summary and "a/b" in summary


def test_tidy_reply_collapses_repetition():
    assert tidy_reply("Step one.\nStep one.\n\nStep two.\nstep one.") == "Step one.\n\nStep two.\nstep one."


def test_validate_messages_bounds():
    assert validate_messages({"messages": [{"role": "user", "content": "hi"}]})[0].content == "hi"
    with pytest.raises(ParseError):
        validate_messages({"messages": [{"role": "assistant", "content": "hi"}]})
    with pytest.raises(ParseError):
        validate_messages({"messages": [{"role": "user", "content": "x" * 4001}]})


def test_aria_routes_with_fake_runtime(tmp_path):
    from test_api import client_for, CTL
    from bridle_engine.runtime import GenEvent

    c, engine = client_for(tmp_path / "m", tmp_path)
    assert c.get("/control/aria/status").status_code == 401
    status = c.get("/control/aria/status", headers=CTL).json()
    assert status["available"] is False and status["repo"].endswith("Qwen3-0.6B-4bit")
    assert c.post("/control/aria/chat", headers=CTL,
                  json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 404

    # Stand in for the loaded Aria handle: ensure/chat_prompt/generate only.
    handle = type("H", (), {"repo": "aria/test", "kind": "text"})()
    engine.runtime.ensure_aria = lambda: handle
    engine.runtime.chat_prompt = lambda lm, messages: "prompt"
    engine.runtime.generate = lambda lm, prompt, params, cancel: iter([
        GenEvent(text='Try Models.\n```aria-action\n{"kind": "open", "screen": "models"}\n```',
                 finish_reason="stop")])
    reply = c.post("/control/aria/chat", headers=CTL,
                   json={"messages": [{"role": "user", "content": "where are models?"}]}).json()
    assert reply["source"] == "answer_sheet" and reply["action"] is None
    reply = c.post("/control/aria/chat", headers=CTL,
                   json={"messages": [{"role": "user", "content": "hello, how is it going today?"}]}).json()
    assert reply["reply"] == "Try Models." and reply["action"]["screen"] == "models"
    assert reply["source"] == "model"


def test_aria_transcribe_prefers_downloaded_then_bundled(tmp_path):
    from test_api import client_for, CTL

    c, engine = client_for(tmp_path / "m", tmp_path)
    # Nothing available yet.
    r = c.post("/control/aria/transcribe", headers=CTL, files={"file": ("x.wav", b"RIFF")})
    assert r.status_code == 404
    # Bundled Onyx fallback.
    bundle = tmp_path / "onyx"
    bundle.mkdir()
    (bundle / "config.json").write_text("{}")
    (bundle / "model.safetensors").write_bytes(b"x")
    engine.runtime.onyx_model_path = str(bundle)
    engine.runtime.transcribe_bundled = lambda path, **kw: {"text": "bundled words"}
    r = c.post("/control/aria/transcribe", headers=CTL, files={"file": ("x.wav", b"RIFF")})
    assert r.json() == {"text": "bundled words", "model": "wabibito/Onyx-Whisper-Tiny",
                        "source": "bundled"}
    # A downloaded speech model wins over the bundle.
    import json as _json
    from bridle_engine.domain import RepoId
    repo_dir = engine.store.path_for(RepoId.parse("acme/whisper-x"))
    repo_dir.mkdir(parents=True)
    (repo_dir / ".bridle.json").write_text(_json.dumps({"size_bytes": 1}))
    (repo_dir / "config.json").write_text('{"model_type": "whisper"}')
    (repo_dir / "model.safetensors").write_bytes(b"x")
    engine.runtime.transcribe = lambda repo, path, **kw: {"text": "downloaded words"}
    r = c.post("/control/aria/transcribe", headers=CTL, files={"file": ("x.wav", b"RIFF")})
    assert r.json()["text"] == "downloaded words" and r.json()["source"] == "downloaded"
    assert r.json()["model"] == "acme/whisper-x"


def test_onyx_availability_requires_bundle_files(tmp_path):
    from bridle_engine.catalog import ModelStore
    from bridle_engine.runtime import InferenceRuntime

    rt = InferenceRuntime(ModelStore(tmp_path / "models"), onyx_model_path=str(tmp_path / "onyx"))
    assert rt.onyx_available is False and rt.onyx_status()["loaded"] is False
    bundle = tmp_path / "onyx"
    bundle.mkdir()
    (bundle / "config.json").write_text("{}")
    assert rt.onyx_available is False
    (bundle / "model.safetensors").write_bytes(b"x")
    assert rt.onyx_available is True
