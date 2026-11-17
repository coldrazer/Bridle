"""Typed request/value objects. Untrusted JSON is parsed into these at the boundary
(parse, don't validate): everything past this module can rely on the invariants."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

MAX_TOKENS_LIMIT = 32768
MAX_STOP_SEQUENCES = 4
MAX_IMAGES = 8
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.\-]{0,95}$")
_ROLES = ("system", "user", "assistant", "tool", "developer")


class ParseError(ValueError):
    """Raised when client input does not satisfy a domain invariant."""

    def __init__(self, message: str, param: str | None = None):
        super().__init__(message)
        self.param = param


@dataclass(frozen=True)
class RepoId:
    owner: str
    name: str

    @staticmethod
    def parse(raw: Any) -> "RepoId":
        if not isinstance(raw, str):
            raise ParseError("model id must be a string like 'owner/name'", "model")
        raw = raw.strip()
        if raw.startswith("https://huggingface.co/"):
            raw = raw[len("https://huggingface.co/"):].strip("/")
        if raw.endswith(":latest"):  # Ollama-style tag
            raw = raw[: -len(":latest")]
        if not _REPO_RE.match(raw) or ".." in raw:
            raise ParseError(f"invalid model id {raw!r}; expected 'owner/name'", "model")
        owner, name = raw.split("/", 1)
        return RepoId(owner, name)

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict

    def openai(self, index: int | None = None) -> dict:
        out = {"id": self.id, "type": "function",
               "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)}}
        if index is not None:
            out["index"] = index
        return out


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    images: tuple[str, ...] = ()      # image URLs (http(s) or data:) for vision models

    @staticmethod
    def parse(raw: Any, index: int) -> "ChatMessage":
        where = f"messages[{index}]"
        if not isinstance(raw, dict):
            raise ParseError(f"{where} must be an object", where)
        role = raw.get("role")
        if role not in _ROLES:
            raise ParseError(f"{where}.role must be one of {', '.join(_ROLES)}", f"{where}.role")
        content = raw.get("content")
        images: list[str] = []
        if content is None:
            content = ""
        if isinstance(content, list):
            parts: list[str] = []
            for j, part in enumerate(content):
                kind = part.get("type") if isinstance(part, dict) else None
                if kind == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif kind == "image_url":
                    url = part.get("image_url")
                    url = url.get("url") if isinstance(url, dict) else url
                    if not isinstance(url, str) or not url.startswith(("http://", "https://", "data:image/")):
                        raise ParseError(f"{where}.content[{j}].image_url must be an http(s) or data:image URL",
                                         f"{where}.content")
                    images.append(url)
                else:
                    raise ParseError(f"{where}.content[{j}] must be a text or image_url part", f"{where}.content")
            content = "".join(parts)
        if not isinstance(content, str):
            raise ParseError(f"{where}.content must be a string or list of parts", f"{where}.content")
        tool_calls = _parse_tool_calls(raw.get("tool_calls"), where) if role == "assistant" else ()
        tool_call_id = raw.get("tool_call_id")
        if tool_call_id is not None and not isinstance(tool_call_id, str):
            raise ParseError(f"{where}.tool_call_id must be a string", f"{where}.tool_call_id")
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise ParseError(f"{where}.name must be a string", f"{where}.name")
        # 'developer' is OpenAI's newer name for 'system'.
        return ChatMessage("system" if role == "developer" else role, content, tool_calls,
                           tool_call_id, name, tuple(images))

    def as_template_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            # HF chat templates expect arguments as a mapping, not a JSON string.
            d["tool_calls"] = [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments}} for c in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


def _parse_tool_calls(raw: Any, where: str) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ParseError(f"{where}.tool_calls must be an array", f"{where}.tool_calls")
    calls = []
    for i, c in enumerate(raw):
        fn = c.get("function") if isinstance(c, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise ParseError(f"{where}.tool_calls[{i}].function.name is required", f"{where}.tool_calls")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError as exc:
                raise ParseError(f"{where}.tool_calls[{i}].function.arguments is not valid JSON",
                                 f"{where}.tool_calls") from exc
        if not isinstance(args, dict):
            raise ParseError(f"{where}.tool_calls[{i}].function.arguments must be an object",
                             f"{where}.tool_calls")
        calls.append(ToolCall(str(c.get("id") or f"call_{i}"), fn["name"], args))
    return tuple(calls)


@dataclass(frozen=True)
class SamplingParams:
    max_tokens: int
    temperature: float
    top_p: float
    stop: tuple[str, ...]
    seed: int | None
    explicit: frozenset[str] = field(default_factory=frozenset)  # fields the client actually sent

    @staticmethod
    def parse(body: dict[str, Any], default_max_tokens: int = 1024) -> "SamplingParams":
        explicit = set()
        max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
        if max_tokens is None:
            max_tokens = default_max_tokens
        else:
            explicit.add("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
            raise ParseError(f"max_tokens must be an integer in [1, {MAX_TOKENS_LIMIT}]", "max_tokens")
        if body.get("temperature") is not None:
            explicit.add("temperature")
        if body.get("top_p") is not None:
            explicit.add("top_p")
        temperature = _number(body.get("temperature", 0.7) if body.get("temperature") is not None else 0.7,
                              "temperature", 0.0, 2.0)
        top_p = _number(body.get("top_p") if body.get("top_p") is not None else 1.0, "top_p", 0.0, 1.0)
        stop_raw = body.get("stop")
        if stop_raw is None:
            stop: tuple[str, ...] = ()
        elif isinstance(stop_raw, str):
            stop = (stop_raw,) if stop_raw else ()
        elif isinstance(stop_raw, list) and all(isinstance(s, str) for s in stop_raw):
            stop = tuple(s for s in stop_raw if s)
        else:
            raise ParseError("stop must be a string or a list of strings", "stop")
        if len(stop) > MAX_STOP_SEQUENCES:
            raise ParseError(f"at most {MAX_STOP_SEQUENCES} stop sequences are allowed", "stop")
        seed = body.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ParseError("seed must be an integer", "seed")
        return SamplingParams(max_tokens, temperature, top_p, stop, seed, frozenset(explicit))


@dataclass(frozen=True)
class ResponseFormat:
    type: str                       # text | json_object | json_schema
    schema: dict | None = None
    name: str = ""

    @staticmethod
    def parse(raw: Any) -> "ResponseFormat | None":
        if raw is None:
            return None
        if not isinstance(raw, dict) or raw.get("type") not in ("text", "json_object", "json_schema"):
            raise ParseError("response_format.type must be text, json_object or json_schema", "response_format")
        kind = raw["type"]
        if kind == "text":
            return None
        if kind == "json_object":
            return ResponseFormat("json_object", {"type": "object"})
        spec = raw.get("json_schema")
        if not isinstance(spec, dict) or not isinstance(spec.get("schema"), dict):
            raise ParseError("response_format.json_schema.schema must be an object", "response_format")
        _check_schema_bounds(spec["schema"])
        return ResponseFormat("json_schema", spec["schema"], str(spec.get("name", "")))


def _check_schema_bounds(schema: dict) -> None:
    """Mirror the intelligence module's limits so a client schema cannot exhaust the
    grammar compiler or the validator."""
    try:
        serialized = json.dumps(schema)
    except (TypeError, ValueError) as exc:
        raise ParseError("response_format schema must be JSON-serializable", "response_format") from exc
    if len(serialized) > 32_000:
        raise ParseError("response_format schema exceeds 32000 characters", "response_format")
    pending, nodes = [(schema, 0)], 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 2000:
            raise ParseError("response_format schema exceeds complexity limit", "response_format")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("$ref", "$dynamicRef", "$recursiveRef") and \
                        (not isinstance(child, str) or not child.startswith("#")):
                    raise ParseError("response_format schema references must be local fragments",
                                     "response_format")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


@dataclass(frozen=True)
class ChatRequest:
    model: str | None
    messages: tuple[ChatMessage, ...]
    params: SamplingParams
    stream: bool
    include_usage: bool
    tools: tuple[dict, ...] = ()
    response_format: ResponseFormat | None = None

    @staticmethod
    def parse(body: Any) -> "ChatRequest":
        if not isinstance(body, dict):
            raise ParseError("request body must be a JSON object")
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ParseError("messages must be a non-empty array", "messages")
        messages = tuple(ChatMessage.parse(m, i) for i, m in enumerate(raw_messages))
        if sum(len(m.images) for m in messages) > MAX_IMAGES:
            raise ParseError(f"at most {MAX_IMAGES} images per request", "messages")
        model = _optional_model(body.get("model"))
        stream = _bool(body.get("stream", False), "stream")
        opts = body.get("stream_options") or {}
        if not isinstance(opts, dict):
            raise ParseError("stream_options must be an object", "stream_options")
        include_usage = _bool(opts.get("include_usage", False), "stream_options.include_usage")
        tools = parse_tools(body.get("tools"), body.get("tool_choice"))
        return ChatRequest(model, messages, SamplingParams.parse(body), stream, include_usage, tools,
                           ResponseFormat.parse(body.get("response_format")))

    @property
    def has_images(self) -> bool:
        return any(m.images for m in self.messages)

    def with_changes(self, **kw: Any) -> "ChatRequest":
        return replace(self, **kw)


def parse_tools(raw: Any, tool_choice: Any) -> tuple[dict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ParseError("tools must be an array", "tools")
    tools = []
    for i, t in enumerate(raw):
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(t, dict) or t.get("type", "function") != "function" or not isinstance(fn, dict) \
                or not isinstance(fn.get("name"), str):
            raise ParseError(f"tools[{i}] must be {{type: 'function', function: {{name, parameters}}}}", "tools")
        tools.append({"type": "function", "function": fn})
    if tool_choice is None or tool_choice in ("auto", "required"):
        return tuple(tools)
    if tool_choice == "none":
        return ()
    if isinstance(tool_choice, dict) and isinstance(tool_choice.get("function"), dict):
        wanted = tool_choice["function"].get("name")
        chosen = tuple(t for t in tools if t["function"]["name"] == wanted)
        if not chosen:
            raise ParseError(f"tool_choice names unknown tool {wanted!r}", "tool_choice")
        return chosen
    raise ParseError("tool_choice must be 'none', 'auto', 'required' or {type:'function', function:{name}}",
                     "tool_choice")


@dataclass(frozen=True)
class CompletionRequest:
    model: str | None
    prompt: str
    params: SamplingParams
    stream: bool

    @staticmethod
    def parse(body: Any) -> "CompletionRequest":
        if not isinstance(body, dict):
            raise ParseError("request body must be a JSON object")
        prompt = body.get("prompt")
        if isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], str):
            prompt = prompt[0]
        if not isinstance(prompt, str) or not prompt:
            raise ParseError("prompt must be a non-empty string", "prompt")
        return CompletionRequest(
            _optional_model(body.get("model")),
            prompt,
            SamplingParams.parse(body, default_max_tokens=256),
            _bool(body.get("stream", False), "stream"),
        )


@dataclass(frozen=True)
class EmbeddingRequest:
    model: str
    inputs: tuple[str, ...]
    encoding_format: str

    @staticmethod
    def parse(body: Any) -> "EmbeddingRequest":
        if not isinstance(body, dict):
            raise ParseError("request body must be a JSON object")
        model = str(RepoId.parse(body.get("model")))
        raw = body.get("input")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw or not all(isinstance(s, str) and s for s in raw):
            raise ParseError("input must be a non-empty string or array of non-empty strings", "input")
        if any(len(s) > 32_768 for s in raw):
            raise ParseError("each input must be at most 32768 characters", "input")
        if len(raw) > 512:
            raise ParseError("at most 512 inputs per request", "input")
        fmt = body.get("encoding_format", "float")
        if fmt not in ("float", "base64"):
            raise ParseError("encoding_format must be float or base64", "encoding_format")
        return EmbeddingRequest(model, tuple(raw), fmt)


def _optional_model(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ParseError("model must be a string", "model")
    return raw.strip()


def _bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise ParseError(f"{name} must be a boolean", name)
    return raw


def _number(raw: Any, name: str, lo: float, hi: float) -> float:
    if raw is None:
        raise ParseError(f"{name} must be a number", name)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not lo <= float(raw) <= hi:
        raise ParseError(f"{name} must be a number in [{lo}, {hi}]", name)
    return float(raw)
