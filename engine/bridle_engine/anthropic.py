"""Anthropic-compatible Messages API (/v1/messages) so Anthropic-shaped clients
— notably Claude Code via ANTHROPIC_BASE_URL — can drive a Bridle model.

Requests are translated to the shared OpenAI-shaped ChatRequest pipeline and
responses translated back, including Anthropic's SSE event stream and
tool_use/tool_result blocks. Reasoning text is not forwarded (Anthropic
thinking blocks require provider signatures a local model cannot produce).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .domain import ChatRequest, ParseError
from .ollama import _json
from .service import Engine

_ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error",
                403: "permission_error", 404: "not_found_error",
                409: "invalid_request_error", 413: "request_too_large",
                429: "rate_limit_error", 503: "overloaded_error"}


def anthropic_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "type": "error",
        "error": {"type": _ERROR_TYPES.get(status, "api_error"), "message": message}})


def _text_of(content: Any) -> str:
    """Anthropic content (string or block list) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append(_text_of(block.get("content")))
        return "".join(parts)
    return ""


def _image_url(block: dict) -> str | None:
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        return f"data:{source.get('media_type') or 'image/png'};base64,{source['data']}"
    if source.get("type") == "url" and source.get("url"):
        return str(source["url"])
    return None


def to_openai_body(body: dict) -> dict:
    """Translate an Anthropic /v1/messages body to an OpenAI chat body."""
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ParseError("messages must be a non-empty array", "messages")
    messages: list[dict] = []
    system = _text_of(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for i, raw in enumerate(raw_messages):
        if not isinstance(raw, dict) or raw.get("role") not in ("user", "assistant", "system"):
            raise ParseError(f"messages[{i}].role must be 'user' or 'assistant'", "messages")
        role, content = raw["role"], raw.get("content")
        if role == "system":
            # Claude Code sends mid-conversation system messages; they map
            # directly onto the shared pipeline's system role.
            text = _text_of(content)
            if text:
                messages.append({"role": "system", "content": text})
            continue
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ParseError(f"messages[{i}].content must be a string or array of blocks", "messages")
        before = len(messages)
        text_parts: list[dict] = []
        tool_calls: list[dict] = []
        pending: list[dict] = []  # tool messages, flushed in block order

        def flush_text() -> None:
            if text_parts:
                if len(text_parts) == 1 and text_parts[0]["type"] == "text":
                    messages.append({"role": role, "content": text_parts[0]["text"]})
                else:
                    messages.append({"role": role, "content": list(text_parts)})
                text_parts.clear()

        for block in content:
            if not isinstance(block, dict):
                raise ParseError(f"messages[{i}] content blocks must be objects", "messages")
            kind = block.get("type")
            if kind == "text":
                text_parts.append({"type": "text", "text": block.get("text") or ""})
            elif kind == "image":
                url = _image_url(block)
                if url is None:
                    raise ParseError(f"messages[{i}] image block needs a base64 or url source", "messages")
                text_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif kind == "tool_result" and role == "user":
                flush_text()
                result_text = _text_of(block.get("content"))
                if block.get("is_error"):
                    result_text = f"Error: {result_text}" if result_text else "Error"
                pending.append({"role": "tool", "content": result_text,
                                "tool_call_id": block.get("tool_use_id") or ""})
                messages.extend(pending)
                pending.clear()
            elif kind == "document":
                source = block.get("source") or {}
                if source.get("type") == "text" and isinstance(source.get("data"), str):
                    text_parts.append({"type": "text", "text": source["data"]})
                else:
                    raise ParseError(f"messages[{i}] document blocks need a text source "
                                     "(binary documents are not supported by local models)", "messages")
            elif kind == "tool_use" and role == "assistant":
                tool_calls.append({"id": block.get("id") or f"toolu_{len(tool_calls)}",
                                   "type": "function",
                                   "function": {"name": block.get("name") or "",
                                                "arguments": json.dumps(block.get("input") or {})}})
            elif kind in ("thinking", "redacted_thinking"):
                continue  # provider-signed reasoning; local templates take plain text only
            else:
                raise ParseError(f"messages[{i}] has an unsupported '{kind}' block for role '{role}'",
                                 "messages")
        if tool_calls:
            text = "".join(p["text"] for p in text_parts if p["type"] == "text")
            images = [p for p in text_parts if p["type"] != "text"]
            msg: dict[str, Any] = {"role": "assistant",
                                   "content": ([{"type": "text", "text": text}] + images) if images else text,
                                   "tool_calls": tool_calls}
            messages.append(msg)
        else:
            flush_text()
            if len(messages) == before:
                messages.append({"role": role, "content": ""})  # empty block list: keep the turn
    if not messages:
        raise ParseError("messages must be a non-empty array", "messages")
    out: dict[str, Any] = {"messages": messages, "stream": body.get("stream") is True}
    model = body.get("model")
    if isinstance(model, str) and model and not model.startswith("claude-"):
        out["model"] = model  # claude-* names mean "whatever Bridle has loaded"
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("max_tokens") is not None:
        from .domain import MAX_TOKENS_LIMIT
        value = body["max_tokens"]
        # Harness clients ask for frontier-sized outputs; clamp instead of rejecting.
        out["max_tokens"] = min(value, MAX_TOKENS_LIMIT) if isinstance(value, int) \
            and not isinstance(value, bool) else value
    if body.get("stop_sequences") is not None:
        stops = body["stop_sequences"]
        # Anthropic allows more stop sequences than the shared pipeline's cap; clamp like max_tokens.
        out["stop"] = stops[:4] if isinstance(stops, list) else stops
    tools = body.get("tools")
    if tools:
        if not isinstance(tools, list):
            raise ParseError("tools must be an array", "tools")
        # Anthropic server tools (web_search_20250305 etc.) execute provider-side;
        # a local model cannot run them, so don't offer them as client functions.
        custom = [t for t in tools if isinstance(t, dict)
                  and t.get("type") in (None, "custom", "function")]
        if custom:
            out["tools"] = [{"type": "function",
                             "function": {"name": t.get("name"), "description": t.get("description") or "",
                                          "parameters": t.get("input_schema") or {"type": "object"}}}
                            for t in custom]
        choice = body.get("tool_choice") or {}
        if isinstance(choice, dict):
            kind = choice.get("type")
            if kind == "any":
                out["tool_choice"] = "required"
            elif kind == "tool" and choice.get("name"):
                out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
            elif kind == "none":
                out["tool_choice"] = "none"
    return out


def stop_reason(ev: Any) -> str:
    if ev.tool_calls or ev.finish_reason == "tool_calls":
        return "tool_use"
    return "max_tokens" if ev.finish_reason == "length" else "end_turn"


def content_blocks(text: str, tool_calls: tuple | list) -> list[dict]:
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for call in tool_calls or ():
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
    return blocks


def register(app: FastAPI, engine: Engine, start_chat: Callable, collect: Callable,
             client_of: Callable) -> None:
    rt = engine.runtime

    def message_id() -> str:
        return f"msg_{uuid.uuid4().hex[:24]}"

    @app.post("/v1/messages")
    async def messages(request: Request):
        try:
            req = ChatRequest.parse(to_openai_body(await _json(request)))
        except ParseError as exc:
            return anthropic_error(400, str(exc))
        except HTTPException as exc:  # body cap etc.: same Anthropic error shape
            return anthropic_error(exc.status_code, str(exc.detail))
        from .runtime import MemoryGuardRefused, ModelUnavailable, TemplateError
        from .runtime_errors import FeatureUnavailable
        from .structured import StructuredOutputUnavailable
        try:
            started = await start_chat(request, req, "/v1/messages")
        except ModelUnavailable as exc:
            return anthropic_error(exc.status, str(exc))
        except ParseError as exc:
            return anthropic_error(400, str(exc))
        except MemoryGuardRefused as exc:
            return anthropic_error(409, str(exc))
        except (TemplateError, FeatureUnavailable, StructuredOutputUnavailable) as exc:
            return anthropic_error(getattr(exc, "status", 400) if isinstance(exc, TemplateError) else 501,
                                    str(exc))
        if isinstance(started, JSONResponse):
            return anthropic_error(started.status_code,
                                    json.loads(started.body)["error"]["message"])
        lm, events, first = started
        mid, model = message_id(), req.model or lm.repo

        if not req.stream:
            text, _reasoning, final = await collect(first, events, request)
            return {"id": mid, "type": "message", "role": "assistant", "model": model,
                    "content": content_blocks(text, final.tool_calls),
                    "stop_reason": stop_reason(final), "stop_sequence": None,
                    "usage": {"input_tokens": final.prompt_tokens,
                              "output_tokens": final.completion_tokens}}

        def sse(event: str, payload: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # Real prompt size up front: harnesses drive their context meter and
        # compaction from message_start usage, so 0 here misleads them.
        input_tokens = 0
        try:
            from .runtime import encode_prompt
            input_tokens = len(encode_prompt(lm.tokenizer, rt.chat_prompt(lm, req.messages, req.tools)))
        except Exception:  # noqa: BLE001 - estimate only; final usage is authoritative
            input_tokens = 0

        async def stream():
            yield sse("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})
            index, open_block = 0, False

            def close_text():
                nonlocal index, open_block
                if open_block:
                    open_block = False
                    out = sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    index += 1
                    return out
                return ""

            ev = first
            try:
                while True:
                    if ev.text:
                        if not open_block:
                            open_block = True
                            yield sse("content_block_start", {"type": "content_block_start",
                                      "index": index, "content_block": {"type": "text", "text": ""}})
                        yield sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                  "delta": {"type": "text_delta", "text": ev.text}})
                    if ev.finish_reason is not None:
                        tail = close_text()
                        if tail:
                            yield tail
                        for call in ev.tool_calls or ():
                            yield sse("content_block_start", {"type": "content_block_start", "index": index,
                                      "content_block": {"type": "tool_use", "id": call.id,
                                                        "name": call.name, "input": {}}})
                            yield sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                      "delta": {"type": "input_json_delta",
                                                "partial_json": json.dumps(call.arguments, ensure_ascii=False)}})
                            yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
                            index += 1
                        yield sse("message_delta", {"type": "message_delta",
                                  "delta": {"stop_reason": stop_reason(ev), "stop_sequence": None},
                                  "usage": {"input_tokens": ev.prompt_tokens,
                                            "output_tokens": ev.completion_tokens}})
                        break
                    ev = await events.__anext__()
            except StopAsyncIteration:
                tail = close_text()
                if tail:
                    yield tail
                yield sse("message_delta", {"type": "message_delta",
                          "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                          "usage": {"input_tokens": 0, "output_tokens": 0}})
            except Exception as exc:  # noqa: BLE001 - in-band, like the OpenAI stream
                yield sse("error", {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            finally:
                await events.aclose()
            yield sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    @app.get("/v1/models/{model_id:path}")
    async def model_info(model_id: str):
        from .domain import RepoId

        known = False
        if model_id.startswith("claude-"):
            known = rt.loaded is not None  # alias resolves to the loaded model
        else:
            try:
                known = engine.store.get(RepoId.parse(model_id)) is not None
            except ParseError:
                known = False
        if not known:
            return anthropic_error(404, f"model {model_id!r} is not downloaded on this Mac")
        return {"id": model_id, "type": "model", "display_name": model_id,
                "created_at": "2025-01-01T00:00:00Z"}

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        try:
            body = await _json(request)
            req = ChatRequest.parse(to_openai_body(body))
        except ParseError as exc:
            return anthropic_error(400, str(exc))
        except HTTPException as exc:
            return anthropic_error(exc.status_code, str(exc.detail))
        lm = rt.loaded
        if lm is not None and lm.kind == "text":
            try:
                from .runtime import encode_prompt
                prompt = rt.chat_prompt(lm, req.messages, req.tools)
                return {"input_tokens": len(encode_prompt(lm.tokenizer, prompt))}
            except Exception:  # noqa: BLE001 - fall back to the estimate below
                pass
        chars = sum(len(m.content) for m in req.messages) + len(json.dumps(body.get("tools") or ""))
        return {"input_tokens": max(1, chars // 4)}
