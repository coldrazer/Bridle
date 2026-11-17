"""OpenAI Responses API (/v1/responses) — the newer OpenAI surface spoken by
Codex and recent SDKs — translated onto the shared chat pipeline."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .domain import ChatRequest, ParseError
from .ollama import _json
from .service import Engine


def to_openai_body(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    if body.get("previous_response_id"):
        raise ParseError("previous_response_id is not supported; resend the full input history "
                         "(Bridle stores no responses)", "previous_response_id")
    if body.get("background"):
        raise ParseError("background responses are not supported", "background")
    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw = body.get("input")
    if isinstance(raw, str):
        if raw:
            messages.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ParseError(f"input[{i}] must be an object", "input")
            kind = item.get("type", "message")
            if kind == "message" or (kind is None and "role" in item):
                role = item.get("role") or "user"
                content = item.get("content")
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})
                elif isinstance(content, list):
                    parts = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in ("input_text", "output_text", "text"):
                            parts.append({"type": "text", "text": part.get("text") or ""})
                        elif part.get("type") == "input_image":
                            url = part.get("image_url")
                            if isinstance(url, str):
                                parts.append({"type": "image_url", "image_url": {"url": url}})
                            else:
                                raise ParseError("input_image parts need an image_url string", "input")
                        else:
                            raise ParseError(f"unsupported input part type {part.get('type')!r}", "input")
                    messages.append({"role": role,
                                     "content": parts if len(parts) != 1 or parts[0]["type"] != "text"
                                     else parts[0]["text"]})
            elif kind == "function_call":
                args = item.get("arguments") or "{}"
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": item.get("call_id") or item.get("id") or "call_0", "type": "function",
                     "function": {"name": item.get("name") or "", "arguments": args}}]})
            elif kind == "function_call_output":
                output = item.get("output")
                messages.append({"role": "tool",
                                 "content": output if isinstance(output, str) else json.dumps(output),
                                 "tool_call_id": item.get("call_id") or ""})
            # reasoning and other item types are not replayable into a chat template; skip them.
    if not messages:
        raise ParseError("input must be a non-empty string or array of items", "input")
    out: dict[str, Any] = {"messages": messages, "stream": body.get("stream") is True}
    if isinstance(body.get("model"), str) and body["model"]:
        out["model"] = body["model"]
    if body.get("max_output_tokens") is not None:
        value = body["max_output_tokens"]
        from .domain import MAX_TOKENS_LIMIT
        out["max_tokens"] = min(value, MAX_TOKENS_LIMIT) if isinstance(value, int) \
            and not isinstance(value, bool) else value
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            out[key] = body[key]
    tools = body.get("tools")
    if tools:
        if not isinstance(tools, list):
            raise ParseError("tools must be an array", "tools")
        converted = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and "function" in t:  # chat-shaped already
                converted.append(t)
            elif t.get("type") in ("function", None) and t.get("name"):
                converted.append({"type": "function", "function": {
                    "name": t["name"], "description": t.get("description") or "",
                    "parameters": t.get("parameters") or {"type": "object"}}})
        if converted:
            out["tools"] = converted
    choice = body.get("tool_choice")
    if isinstance(choice, str) and choice in ("auto", "none", "required"):
        out["tool_choice"] = choice
    elif isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
        out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    elif choice is not None:
        raise ParseError("tool_choice must be 'auto', 'none', 'required' or {type:'function', name}",
                         "tool_choice")
    text_cfg = body.get("text")
    if isinstance(text_cfg, dict) and isinstance(text_cfg.get("format"), dict):
        fmt = text_cfg["format"]
        if fmt.get("type") == "json_object":
            out["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema":
            out["response_format"] = {"type": "json_schema", "json_schema": {
                "name": fmt.get("name") or "response", "schema": fmt.get("schema") or {}}}
    return out


def _output_items(text: str, tool_calls) -> list[dict]:
    items: list[dict] = []
    if text:
        items.append({"id": f"msg_{uuid.uuid4().hex[:20]}", "type": "message", "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for call in tool_calls or ():
        items.append({"id": f"fc_{uuid.uuid4().hex[:20]}", "type": "function_call",
                      "call_id": call.id, "name": call.name, "status": "completed",
                      "arguments": json.dumps(call.arguments, ensure_ascii=False)})
    return items


def register(app: FastAPI, engine: Engine, start_chat: Callable, collect: Callable,
             client_of: Callable) -> None:
    @app.post("/v1/responses")
    async def responses(request: Request):
        from .runtime import MemoryGuardRefused, ModelUnavailable, TemplateError
        from .runtime_errors import FeatureUnavailable
        from .structured import StructuredOutputUnavailable

        def err(status: int, message: str) -> JSONResponse:
            return JSONResponse(status_code=status, content={"error": {
                "message": message, "type": "invalid_request_error" if status < 500 else "server_error",
                "param": None, "code": None}})

        try:
            req = ChatRequest.parse(to_openai_body(await _json(request)))
            started = await start_chat(request, req, "/v1/responses")
        except ParseError as exc:
            return err(400, str(exc))
        except HTTPException as exc:
            return err(exc.status_code, str(exc.detail))
        except ModelUnavailable as exc:
            return err(exc.status, str(exc))
        except MemoryGuardRefused as exc:
            return err(409, str(exc))
        except (TemplateError, FeatureUnavailable, StructuredOutputUnavailable) as exc:
            return err(400 if isinstance(exc, TemplateError) else 501, str(exc))
        if isinstance(started, JSONResponse):
            return err(started.status_code, json.loads(started.body)["error"]["message"])
        lm, events, first = started
        rid, created = f"resp_{uuid.uuid4().hex[:24]}", int(time.time())
        model = req.model or lm.repo

        def base(status: str, output: list, usage: dict | None) -> dict:
            body: dict[str, Any] = {"id": rid, "object": "response", "created_at": created,
                                    "status": status, "model": model, "output": output,
                                    "parallel_tool_calls": True, "store": False}
            if usage is not None:
                body["usage"] = usage
            return body

        def usage_of(ev: Any) -> dict:
            return {"input_tokens": ev.prompt_tokens, "output_tokens": ev.completion_tokens,
                    "total_tokens": ev.prompt_tokens + ev.completion_tokens}

        if not req.stream:
            text, _reasoning, final = await collect(first, events, request)
            return base("completed", _output_items(text, final.tool_calls), usage_of(final))

        sequence = 0

        def sse(event: str, payload: dict) -> str:
            nonlocal sequence
            payload = {**payload, "sequence_number": sequence}
            sequence += 1
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def stream():
            yield sse("response.created", {"type": "response.created", "response": base("in_progress", [], None)})
            yield sse("response.in_progress", {"type": "response.in_progress", "response": base("in_progress", [], None)})
            msg_id = f"msg_{uuid.uuid4().hex[:20]}"
            ev, text_parts = first, []
            index, msg_open = 0, False
            final_items: list[dict] = []
            try:
                while True:
                    if ev.text:
                        if not msg_open:
                            msg_open = True
                            yield sse("response.output_item.added", {"type": "response.output_item.added",
                                      "output_index": index,
                                      "item": {"id": msg_id, "type": "message", "role": "assistant",
                                               "status": "in_progress", "content": []}})
                            yield sse("response.content_part.added", {"type": "response.content_part.added",
                                      "item_id": msg_id, "output_index": index, "content_index": 0,
                                      "part": {"type": "output_text", "text": "", "annotations": []}})
                        text_parts.append(ev.text)
                        yield sse("response.output_text.delta", {"type": "response.output_text.delta",
                                  "item_id": msg_id, "output_index": index, "content_index": 0, "delta": ev.text})
                    if ev.finish_reason is not None:
                        text = "".join(text_parts)
                        if msg_open:
                            part = {"type": "output_text", "text": text, "annotations": []}
                            item = {"id": msg_id, "type": "message", "role": "assistant",
                                    "status": "completed", "content": [part]}
                            yield sse("response.output_text.done", {"type": "response.output_text.done",
                                      "item_id": msg_id, "output_index": index, "content_index": 0, "text": text})
                            yield sse("response.content_part.done", {"type": "response.content_part.done",
                                      "item_id": msg_id, "output_index": index, "content_index": 0, "part": part})
                            yield sse("response.output_item.done",
                                      {"type": "response.output_item.done", "output_index": index, "item": item})
                            final_items.append(item)
                            index += 1
                        for call in ev.tool_calls or ():
                            args = json.dumps(call.arguments, ensure_ascii=False)
                            fc = {"id": f"fc_{uuid.uuid4().hex[:20]}", "type": "function_call",
                                  "call_id": call.id, "name": call.name, "status": "in_progress", "arguments": ""}
                            yield sse("response.output_item.added",
                                      {"type": "response.output_item.added", "output_index": index, "item": fc})
                            yield sse("response.function_call_arguments.delta",
                                      {"type": "response.function_call_arguments.delta",
                                       "item_id": fc["id"], "output_index": index, "delta": args})
                            done = {**fc, "status": "completed", "arguments": args}
                            yield sse("response.function_call_arguments.done",
                                      {"type": "response.function_call_arguments.done",
                                       "item_id": fc["id"], "output_index": index, "arguments": args})
                            yield sse("response.output_item.done",
                                      {"type": "response.output_item.done", "output_index": index, "item": done})
                            final_items.append(done)
                            index += 1
                        yield sse("response.completed",
                                  {"type": "response.completed",
                                   "response": base("completed", final_items, usage_of(ev))})
                        break
                    ev = await events.__anext__()
            except StopAsyncIteration:
                yield sse("response.completed",
                          {"type": "response.completed",
                           "response": base("completed", _output_items("".join(text_parts), ()),
                                            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})})
            except Exception as exc:  # noqa: BLE001
                yield sse("response.failed", {"type": "response.failed",
                          "response": {**base("failed", [], None),
                                       "error": {"message": str(exc), "type": "server_error", "code": None}}})
            finally:
                await events.aclose()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
