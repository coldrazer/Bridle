"""Unit tests for the production add-ons (no model needed)."""

import json

import pytest

from bridle_engine.domain import ChatMessage, ChatRequest, ParseError, ResponseFormat, SamplingParams
from bridle_engine.history import RequestHistory, RequestRecord
from bridle_engine.jobs import parse_bench, parse_compare
from bridle_engine.keys import KeyStore, RateLimited, parse_key_create
from bridle_engine.presets import PresetStore, parse_preset
from bridle_engine.runtime import PromptCacheStore, model_kind
from bridle_engine.service import apply_preset
from bridle_engine.settings import SettingsStore, parse_settings_patch
from bridle_engine.textproc import TextRouter, parse_tool_calls


# ---- tool calls & reasoning ------------------------------------------------------------------------
def route(router, chunks):
    out = []
    for c in chunks:
        out += router.feed(c)
    return out + router.flush()


def test_router_splits_reasoning_tools_and_content_across_chunks():
    r = TextRouter(("<think>", "</think>"), ("<tool_call>", "</tool_call>"))
    out = route(r, ["<thi", "nk>pondering", "</th", "ink>Sure. <tool_", 'call>{"name":"f","arguments":{}}</tool', "_call> done"])
    kinds = [(k, t) for k, t in out]
    assert ("reasoning", "pondering") in kinds or "".join(t for k, t in out if k == "reasoning") == "pondering"
    assert "".join(t for k, t in out if k == "content") == "Sure.  done"
    assert [t for k, t in out if k == "tool"] == ['{"name":"f","arguments":{}}']


def test_router_starting_inside_reasoning_and_plain_models():
    r = TextRouter(("<think>", "</think>"), None, start_in_reasoning=True)
    out = route(r, ["a", "b</think>", "answer"])
    assert "".join(t for k, t in out if k == "reasoning") == "ab"
    assert "".join(t for k, t in out if k == "content") == "answer"
    plain = TextRouter(None, None)
    assert route(plain, ["<think>x</think>"]) == [("content", "<think>x</think>")]


def test_router_unterminated_tool_call_is_flushed():
    r = TextRouter(None, ("<tool_call>", "</tool_call>"))
    out = route(r, ['<tool_call>{"name": "f"'])
    assert out == [("tool", '{"name": "f"')]


def test_tool_call_parsing_with_model_parser_and_lenient_fallback():
    strict = lambda text, tools: json.loads(text.strip())  # noqa: E731 - mirrors mlx-lm's json_tools parser
    calls = parse_tool_calls(strict, ['{"name": "get_weather", "arguments": {"city": "Paris"}}'], ())
    assert calls[0].name == "get_weather" and calls[0].arguments == {"city": "Paris"}
    messy = '\n{{"name": "get_weather", "arguments": {"city": "Paris"}} {{\n'
    assert [c.name for c in parse_tool_calls(strict, [messy], ())] == ["get_weather"]
    assert parse_tool_calls(strict, ["not json at all"], ()) == []
    as_string = parse_tool_calls(strict, ['{"name": "f", "arguments": "{\\"a\\": 1}"}'], ())
    assert as_string[0].arguments == {"a": 1}
    openai = calls[0].openai(0)
    assert openai["type"] == "function" and json.loads(openai["function"]["arguments"]) == {"city": "Paris"}


def test_chat_request_tools_tool_choice_and_tool_messages():
    tools = [{"type": "function", "function": {"name": "a", "parameters": {}}},
             {"type": "function", "function": {"name": "b", "parameters": {}}}]
    base = {"messages": [{"role": "user", "content": "x"}], "tools": tools}
    assert len(ChatRequest.parse(base).tools) == 2
    assert ChatRequest.parse({**base, "tool_choice": "none"}).tools == ()
    named = ChatRequest.parse({**base, "tool_choice": {"type": "function", "function": {"name": "b"}}})
    assert [t["function"]["name"] for t in named.tools] == ["b"]
    with pytest.raises(ParseError):
        ChatRequest.parse({**base, "tool_choice": {"type": "function", "function": {"name": "zzz"}}})
    convo = ChatRequest.parse({"messages": [
        {"role": "user", "content": "w?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": '{"q": 1}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "42"}]})
    d = convo.messages[1].as_template_dict()
    assert d["tool_calls"][0]["function"]["arguments"] == {"q": 1}  # templates want a mapping
    assert convo.messages[2].as_template_dict()["tool_call_id"] == "c1"


def test_images_and_response_format_parsing():
    req = ChatRequest.parse({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}})
    assert req.messages[0].images == ("data:image/png;base64,AAAA",) and req.has_images
    assert req.response_format == ResponseFormat("json_schema", {"type": "object"}, "x")
    assert ChatRequest.parse({"messages": [{"role": "user", "content": "x"}],
                              "response_format": {"type": "text"}}).response_format is None
    with pytest.raises(ParseError):
        ChatRequest.parse({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}]}]})


# ---- API keys ------------------------------------------------------------------------------------
def test_keys_hash_only_rate_limits_and_daily_tokens(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    key, secret = store.create("laptop", rpm_limit=2, daily_token_limit=100)
    assert secret.startswith("brdl_") and secret not in (tmp_path / "keys.json").read_text()
    assert store.authenticate(secret).id == key.id and store.authenticate("brdl_wrong") is None
    store.admit(key)
    store.admit(key)
    with pytest.raises(RateLimited) as err:
        store.admit(key)
    assert 1 <= err.value.retry_after <= 60
    store._windows.clear()
    store.record_tokens(key.id, 150)
    with pytest.raises(RateLimited):
        store.admit(key)
    store.revoke(key.id)
    assert store.authenticate(secret) is None and not store.has_active_keys()
    store.flush()
    assert KeyStore(tmp_path / "keys.json").list()[0].tokens_total == 150   # persisted
    with pytest.raises(ParseError):
        parse_key_create({"name": "", "rpm_limit": 0})


# ---- presets ---------------------------------------------------------------------------------------
def test_presets_apply_defaults_but_explicit_values_win(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    store.put(parse_preset({"name": "Coder", "model": "mlx-community/Qwen3-8B-4bit",
                            "system_prompt": "You write code.", "temperature": 0.1, "max_tokens": 2048}))
    req = ChatRequest.parse({"model": "preset:coder", "messages": [{"role": "user", "content": "hi"}],
                             "temperature": 0.9})
    out, preset = apply_preset(req, store)
    assert preset.name == "Coder" and out.model == "mlx-community/Qwen3-8B-4bit"
    assert out.messages[0] == ChatMessage("system", "You write code.")
    assert out.params.temperature == 0.9 and out.params.max_tokens == 2048
    by_name, _ = apply_preset(ChatRequest.parse({"model": "Coder", "messages": [{"role": "user", "content": "x"}]}), store)
    assert by_name.params.temperature == 0.1
    with pytest.raises(ParseError):
        apply_preset(ChatRequest.parse({"model": "preset:nope", "messages": [{"role": "user", "content": "x"}]}), store)
    untouched, p = apply_preset(ChatRequest.parse({"model": "a/b", "messages": [{"role": "user", "content": "x"}]}), store)
    assert p is None and untouched.model == "a/b"
    assert PresetStore(tmp_path / "presets.json").get("coder").max_tokens == 2048


def test_sampling_params_track_explicit_fields():
    assert SamplingParams.parse({}).explicit == frozenset()
    assert SamplingParams.parse({"temperature": 0, "max_tokens": 5}).explicit == {"temperature", "max_tokens"}


# ---- settings / history / jobs ---------------------------------------------------------------------
def test_settings_patch_validation_and_persistence(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    _, restart = store.update(parse_settings_patch({"batching": False, "idle_unload_minutes": 5, "tls": True}))
    assert restart == ["tls"]
    again = SettingsStore(tmp_path / "settings.json").current
    assert again.batching is False and again.idle_unload_minutes == 5 and again.tls is True
    for bad in ({"batching": "no"}, {"idle_unload_minutes": -1}, {"nope": 1}, {"max_batch_size": 0}):
        with pytest.raises(ParseError):
            parse_settings_patch(bad)


def test_history_records_without_content_unless_enabled(tmp_path):
    h = RequestHistory(tmp_path / "r.sqlite3")
    h.add(RequestRecord("/v1/chat/completions", 200, model="m", client="laptop", prompt="secret",
                        response="answer", completion_tokens=10, tps=20.0), keep_content=False)
    h.add(RequestRecord("/v1/chat/completions", 500, model="m", client="phone", error="boom",
                        prompt="p", response="r"), keep_content=True)
    rows = h.list()
    assert rows[0]["client"] == "phone" and rows[0]["prompt"] == "p"
    assert rows[1]["prompt"] is None and rows[1]["response"] is None
    assert [r["client"] for r in h.list(client="laptop")] == ["laptop"]
    s = h.summary()
    assert s["requests"] == 2 and s["errors"] == 1 and s["completion_tokens"] == 10
    h.clear()
    assert h.list() == []


def test_job_request_parsing():
    assert parse_bench({}) == (512, 128)
    with pytest.raises(ParseError):
        parse_bench({"prompt_tokens": 5})
    job = parse_compare({"models": ["a/b", "c/d"], "prompt": "hi", "max_tokens": 50})
    assert [r.repo for r in job.runs] == ["a/b", "c/d"] and job.params.max_tokens == 50
    with pytest.raises(ParseError):
        parse_compare({"models": ["a/b"], "prompt": "hi"})


# ---- prompt cache bookkeeping ------------------------------------------------------------------------
class FakeKV:
    def __init__(self, offset):
        self.offset = offset

    def is_trimmable(self):
        return True

    def trim(self, n):
        n = min(self.offset, n)
        self.offset -= n
        return n


def test_prompt_cache_reuses_longest_prefix_and_trims():
    store = PromptCacheStore()
    made = []

    def make():
        made.append(1)
        return [FakeKV(0), FakeKV(0)]

    cache, reused = store.fetch(("m", False), make, [1, 2, 3])
    assert reused == 0 and len(made) == 1
    for c in cache:
        c.offset = 5          # prompt 1,2,3 + generated 7,8
    store.store(("m", False), [1, 2, 3, 7, 8, 9], cache, main_layers=2)   # 9 sampled but never fed
    cache2, reused = store.fetch(("m", False), make, [1, 2, 3, 7, 8, 4, 5])
    assert reused == 5 and cache2 is cache and len(made) == 1
    cache2[0].offset = cache2[1].offset = 7
    store.store(("m", False), [1, 2, 3, 7, 8, 4, 5], cache2, 2)
    cache3, reused = store.fetch(("m", False), make, [1, 2, 9])           # diverges after 2 tokens
    assert reused == 2 and cache3[0].offset == 2
    assert store.fetch(("other", False), make, [1, 2])[1] == 0              # keyed per model


def test_model_kind_detection(tmp_path):
    def mk(name, config, extra=()):
        d = tmp_path / name
        d.mkdir()
        (d / "config.json").write_text(json.dumps(config))
        for f in extra:
            (d / f).write_text("{}")
        return d

    assert model_kind(mk("llm", {"model_type": "qwen2"})) == "text"
    assert model_kind(mk("vlm", {"model_type": "qwen2_5_vl", "vision_config": {}})) == "vision"
    assert model_kind(mk("emb", {"model_type": "bert"}, ["modules.json"])) == "embedding"
    assert model_kind(mk("asr", {"model_type": "whisper"})) == "speech"


# ---- TLS -------------------------------------------------------------------------------------------
def test_self_signed_certificate_is_reused_until_addresses_change(tmp_path):
    from bridle_engine.tls import ensure_certificate

    cert, key, fp = ensure_certificate(tmp_path, "studio", ["192.168.1.20"])
    assert cert.exists() and key.exists() and len(fp.split(":")) == 32
    assert ensure_certificate(tmp_path, "studio", ["192.168.1.20"])[2] == fp
    assert ensure_certificate(tmp_path, "studio", ["10.0.0.5"])[2] != fp
    assert oct(key.stat().st_mode & 0o777) == "0o600"
