"""Anthropic-compatible /v1/messages: translation unit tests plus API tests.
Generation tests use the real fixture model when available (see test_api)."""

import json

import pytest

from bridle_engine.anthropic import to_openai_body
from bridle_engine.domain import ChatRequest, ParseError
from test_api import TEST_MODEL, client_for, have

needs_model = pytest.mark.skipif(not have(TEST_MODEL), reason="set BRIDLE_TEST_MODELS_DIR")


# ---- translation (no model) -------------------------------------------------------------------
def test_translation_system_tools_and_blocks():
    body = {
        "model": "claude-sonnet-5", "max_tokens": 999999,
        "system": [{"type": "text", "text": "Be terse."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "list files"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "toolu_1", "name": "ls", "input": {"path": "/"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
                {"type": "text", "text": "thanks"}]},
        ],
        "tools": [{"name": "ls", "description": "list", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
    }
    req = ChatRequest.parse(to_openai_body(body))
    assert req.model is None  # claude-* maps to the loaded model
    assert req.params.max_tokens == 32768  # clamped, not rejected
    assert req.messages[0].role == "system" and req.messages[0].content == "Be terse."
    assistant = req.messages[2]
    assert assistant.tool_calls[0].name == "ls" and assistant.tool_calls[0].arguments == {"path": "/"}
    tool_msg = req.messages[3]
    assert tool_msg.role == "tool" and tool_msg.content == "a.txt" and tool_msg.tool_call_id == "toolu_1"
    assert req.messages[4].content == "thanks"
    assert req.tools[0]["function"]["parameters"] == {"type": "object"}


def test_translation_rejects_bad_shapes():
    with pytest.raises(ParseError):
        to_openai_body({"messages": []})
    with pytest.raises(ParseError):
        to_openai_body({"messages": [{"role": "tool", "content": "x"}]})
    with pytest.raises(ParseError):
        to_openai_body({"messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "base64"}}]}]})
    # Text documents are accepted as text; server tools are filtered out.
    req = ChatRequest.parse(to_openai_body({"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "text", "data": "doc body"}}]}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "stop_sequences": ["1", "2", "3", "4", "5", "6"]}))
    assert req.messages[0].content == "doc body"
    assert req.tools == ()
    assert req.params.stop == ("1", "2", "3", "4")


def test_translation_image_block():
    body = {"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
        {"type": "text", "text": "what is this?"}]}]}
    req = ChatRequest.parse(to_openai_body(body))
    assert req.messages[0].images == ("data:image/png;base64,AA==",)


# ---- API surface (no model needed) ------------------------------------------------------------
def test_messages_validation_and_error_shape(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    r = c.post("/v1/messages", json={"messages": []},
               headers={"x-api-key": "bridle-local", "anthropic-version": "2023-06-01"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    r = c.post("/v1/messages", json={"model": "a/b", "max_tokens": 8,
                                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404  # a/b is not downloaded
    assert r.json()["type"] == "error"


def test_count_tokens_and_model_info(tmp_path):
    c, _ = client_for(tmp_path / "m", tmp_path)
    r = c.post("/v1/messages/count_tokens",
               json={"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello world"}]})
    assert r.status_code == 200 and r.json()["input_tokens"] >= 1
    r = c.get("/v1/models/mlx-community/Qwen3-8B-4bit")
    assert r.status_code == 404  # not downloaded on this store
    from test_api import MODELS_DIR
    if have(TEST_MODEL):
        c2, _ = client_for(MODELS_DIR, tmp_path)
        r = c2.get(f"/v1/models/{TEST_MODEL}")
        assert r.status_code == 200 and r.json()["type"] == "model"


def test_x_api_key_authenticates_named_key(tmp_path):
    from test_api import CTL
    c, _ = client_for(tmp_path / "m", tmp_path, local=False)
    secret = c.post("/control/keys", headers=CTL, json={"name": "claude-code"}).json()["secret"]
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models", headers={"x-api-key": secret}).status_code == 200
    assert c.get("/v1/models", headers={"x-api-key": "wrong"}).status_code == 401


# ---- generation (real fixture model) ----------------------------------------------------------
@needs_model
def test_messages_nonstream_and_stream(tmp_path):
    from test_api import MODELS_DIR
    c, _ = client_for(MODELS_DIR, tmp_path)
    body = {"model": TEST_MODEL, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with the single word: hello"}]}
    r = c.post("/v1/messages", json=body, headers={"anthropic-version": "2023-06-01"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["type"] == "message" and payload["role"] == "assistant"
    assert payload["content"] and payload["content"][0]["type"] == "text"
    assert payload["stop_reason"] in ("end_turn", "max_tokens")
    assert payload["usage"]["output_tokens"] > 0

    with c.stream("POST", "/v1/messages", json={**body, "stream": True}) as resp:
        assert resp.status_code == 200
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data:")]
    types = [e["type"] for e in events]
    assert types[0] == "message_start" and types[-1] == "message_stop"
    assert "content_block_delta" in types and "message_delta" in types
