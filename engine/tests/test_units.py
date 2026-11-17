import json
from types import SimpleNamespace

import pytest

from bridle_engine.catalog import GB, ModelStore, catalog_view, fit_status, gpu_memory_budget
from bridle_engine.domain import ChatMessage, ChatRequest, CompletionRequest, ParseError, RepoId, SamplingParams
from bridle_engine.downloads import DownloadFailed, select_files
from bridle_engine.runtime import InferenceRuntime, ModelHandle
from bridle_engine.textproc import StopMatcher
from bridle_engine.system_info import (count_thunderbolt_ports, machine_kind, parse_rdma_status,
                                     thunderbolt_generation_for_chip)


# ---- domain ----------------------------------------------------------------------------
def test_repo_id_parsing():
    assert str(RepoId.parse("mlx-community/Qwen3-8B-4bit")) == "mlx-community/Qwen3-8B-4bit"
    assert str(RepoId.parse("https://huggingface.co/mlx-community/Qwen3-8B-4bit/")) == "mlx-community/Qwen3-8B-4bit"
    for bad in ["", "noslash", "a/b/c", "../etc", "a/..", 5, None, "own er/name"]:
        with pytest.raises(ParseError):
            RepoId.parse(bad)


def test_chat_request_parses_openai_shapes():
    req = ChatRequest.parse({
        "model": "m/x",
        "messages": [
            {"role": "developer", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}]},
        ],
        "max_completion_tokens": 50, "temperature": 0, "stop": "END", "stream": True,
        "stream_options": {"include_usage": True},
    })
    assert req.messages[0] == ChatMessage("system", "be brief")
    assert req.messages[1].content == "hi there"
    assert req.params.max_tokens == 50 and req.params.temperature == 0.0 and req.params.stop == ("END",)
    assert req.stream and req.include_usage


@pytest.mark.parametrize("body,param", [
    ({"messages": []}, "messages"),
    ({"messages": [{"role": "robot", "content": "x"}]}, "messages[0].role"),
    ({"messages": [{"role": "user", "content": 3}]}, "messages[0].content"),
    ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}, "max_tokens"),
    ({"messages": [{"role": "user", "content": "x"}], "max_tokens": True}, "max_tokens"),
    ({"messages": [{"role": "user", "content": "x"}], "temperature": 3}, "temperature"),
    ({"messages": [{"role": "user", "content": "x"}], "stop": ["a", "b", "c", "d", "e"]}, "stop"),
    ({"messages": [{"role": "user", "content": "x"}], "stream": "yes"}, "stream"),
])
def test_chat_request_rejects_bad_input(body, param):
    with pytest.raises(ParseError) as err:
        ChatRequest.parse(body)
    assert err.value.param == param


def test_completion_request():
    req = CompletionRequest.parse({"prompt": ["once upon"], "max_tokens": 5})
    assert req.prompt == "once upon" and req.params.max_tokens == 5
    with pytest.raises(ParseError):
        CompletionRequest.parse({"prompt": ""})


def test_sampling_defaults():
    p = SamplingParams.parse({})
    assert (p.max_tokens, p.temperature, p.top_p, p.stop, p.seed) == (1024, 0.7, 1.0, (), None)


# ---- stop sequences ----------------------------------------------------------------------
def run_matcher(stops, chunks):
    m, out = StopMatcher(stops), []
    for c in chunks:
        text, hit = m.feed(c)
        out.append(text)
        if hit:
            return "".join(out), True
    out.append(m.flush())
    return "".join(out), False


def test_stop_matcher_across_chunk_boundaries():
    assert run_matcher(("STOP",), ["hello S", "TO", "P world"]) == ("hello ", True)
    assert run_matcher(("STOP",), ["hello S", "TOX"]) == ("hello STOX", False)
    assert run_matcher((), ["a", "b"]) == ("ab", False)
    assert run_matcher(("\n\n", "###"), ["x\n", "\ny"]) == ("x", True)


def test_stop_matcher_never_leaks_stop_prefix():
    m = StopMatcher(("</s>",))
    assert m.feed("abc</") == ("ab", False)
    assert m.feed("s>") == ("c", True)


def test_chat_prompt_without_template(tmp_path):
    rt = InferenceRuntime(ModelStore(tmp_path))
    lm = ModelHandle("a/b", str(tmp_path), "text", None, SimpleNamespace(chat_template=None), 0, None, 0.0)
    prompt = rt.chat_prompt(lm, (ChatMessage("system", "s"), ChatMessage("user", "u")))
    assert prompt == "System: s\nUser: u\nAssistant:"


# ---- catalog / store ----------------------------------------------------------------------
def test_fit_status_thresholds():
    assert gpu_memory_budget(16 * GB) == int(16 * GB * 0.67)
    assert gpu_memory_budget(64 * GB) == int(64 * GB * 0.75)
    assert fit_status(2 * GB, 16 * GB) == "fits"
    assert fit_status(11_180_000_000, 16 * GB) == "too_large"  # gpt-oss-20b on a 16 GB Mac
    assert fit_status(11_180_000_000, 48 * GB) == "fits"
    assert fit_status(39_690_000_000, 48 * GB) == "too_large"


def make_model(root, repo, manifest=True):
    d = root / repo
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"model_type": "llama", "quantization": {"bits": 4}}))
    (d / "model.safetensors").write_bytes(b"x" * 10)
    if manifest:
        (d / ".bridle.json").write_text(json.dumps({"repo": repo, "revision": "abc", "size_bytes": 10}))
    return d


def test_store_only_lists_complete_models(tmp_path):
    make_model(tmp_path, "mlx-community/Done")
    make_model(tmp_path, "mlx-community/NoManifest", manifest=False)
    make_model(tmp_path, "mlx-community/Half.partial", manifest=False)
    store = ModelStore(tmp_path)
    models = store.list()
    assert [m.repo for m in models] == ["mlx-community/Done"]
    assert models[0].model_type == "llama" and models[0].quant_bits == 4
    rows = catalog_view(store, 16 * GB)
    assert any(r["repo"] == "mlx-community/Done" and r["downloaded"] for r in rows)
    assert store.delete(RepoId.parse("mlx-community/Done"))
    assert store.get(RepoId.parse("mlx-community/Done")) is None


# ---- downloads ----------------------------------------------------------------------------
def sib(name, size, sha=None):
    return SimpleNamespace(rfilename=name, size=size, lfs=SimpleNamespace(sha256=sha, size=size) if sha else None)


def test_select_files_filters_and_requires_weights():
    files = select_files([sib("config.json", 10), sib("model.safetensors", 100, "ab"), sib("README.md", 5),
                          sib("original/model.pt", 999), sib("tokenizer.json", 7), sib("figure.png", 3)])
    assert [f.name for f in files] == ["config.json", "model.safetensors", "tokenizer.json"]
    assert files[1].sha256 == "ab"
    with pytest.raises(DownloadFailed):
        select_files([sib("config.json", 10)])


# ---- system info --------------------------------------------------------------------------
@pytest.mark.parametrize("chip,gen", [
    ("Apple M2 Pro", 4), ("Apple M4", 4), ("Apple M4 Pro", 5), ("Apple M4 Max", 5), ("Apple M5 Max", 5),
    ("Apple M3 Ultra", 5), ("Apple M2 Ultra", 4), ("Apple M1", 4), ("Intel Core i9", None),
])
def test_thunderbolt_generation(chip, gen):
    assert thunderbolt_generation_for_chip(chip) == gen


def test_machine_kind_and_rdma():
    assert machine_kind("MacBook Pro") == "laptop" and machine_kind("Mac mini") == "mini"
    assert machine_kind("Mac Studio") == "studio"
    assert parse_rdma_status(None) == "unavailable"
    assert parse_rdma_status("RDMA is enabled\n") == "enabled"
    assert parse_rdma_status("disabled") == "disabled"
    assert count_thunderbolt_ports([{"_name": "thunderboltusb4_bus_0"}, {"_name": "thunderboltusb4_bus_1"}]) == 2
    assert count_thunderbolt_ports([]) is None
