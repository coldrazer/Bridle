"""Ollama-compatible API (/api/*) so tools that only speak Ollama can use Bridle.

Models are addressed by their Hugging Face id (an optional ":latest" tag is ignored);
presets work by name too."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .catalog import CATALOG
from .domain import ChatRequest, ParseError, RepoId
from .service import Engine

OLLAMA_VERSION = "0.12.0"


async def _json(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2 * 1024 * 1024:
            raise HTTPException(413, "JSON request exceeds 2 MiB")
        raw.extend(chunk)
    try:
        body = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError("request body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ParseError("request body must be a JSON object")
    return body


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns(seconds: float) -> int:
    return int(seconds * 1e9)


def _options_to_openai(body: dict) -> dict:
    opts = body.get("options") or {}
    if not isinstance(opts, dict):
        raise ParseError("options must be an object", "options")
    out: dict[str, Any] = {}
    if "temperature" in opts:
        out["temperature"] = opts["temperature"]
    if "top_p" in opts:
        out["top_p"] = opts["top_p"]
    if "seed" in opts:
        out["seed"] = opts["seed"]
    if "stop" in opts:
        out["stop"] = opts["stop"]
    n = opts.get("num_predict")
    if isinstance(n, int) and not isinstance(n, bool):
        out["max_tokens"] = n if n > 0 else 8192
    fmt = body.get("format")
    if fmt == "json":
        out["response_format"] = {"type": "json_object"}
    elif isinstance(fmt, dict):
        out["response_format"] = {"type": "json_schema", "json_schema": {"name": "ollama", "schema": fmt}}
    elif fmt not in (None, ""):
        raise ParseError("format must be 'json' or a JSON schema object", "format")
    return out


def _convert_messages(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ParseError("messages must be an array", "messages")
    out = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ParseError(f"messages[{i}] must be an object", "messages")
        images = m.get("images") or []
        msg = {k: v for k, v in m.items() if k not in ("images", "thinking", "tool_name")}
        if images:
            parts: list[dict] = [{"type": "text", "text": m.get("content") or ""}]
            for img in images:
                if not isinstance(img, str):
                    raise ParseError(f"messages[{i}].images must be base64 strings", "messages")
                url = img if img.startswith("data:") else f"data:image/png;base64,{img}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            msg["content"] = parts
        if m.get("role") == "tool" and m.get("tool_name") and not m.get("name"):
            msg["name"] = m["tool_name"]
        out.append(msg)
    return out


def _model_name(body: dict) -> str:
    name = body.get("model") or body.get("name")
    if not isinstance(name, str) or not name:
        raise ParseError("model is required", "model")
    return name


def _details(repo: str, kind: str, size: int, quant_bits: int | None) -> dict:
    entry = next((e for e in CATALOG if e.repo == repo), None)
    return {"format": "mlx", "family": entry.family if entry else "", "families": [entry.family] if entry else [],
            "parameter_size": entry.params if entry else "",
            "quantization_level": entry.quant if entry else (f"Q{quant_bits}" if quant_bits else ""), "kind": kind}


def register(app: FastAPI, engine: Engine, start_chat: Callable, collect: Callable, client_of: Callable) -> None:
    rt = engine.runtime

    @app.get("/")
    async def root():
        return PlainTextResponse("Ollama is running")

    @app.get("/api/version")
    async def version():
        return {"version": OLLAMA_VERSION}

    @app.get("/api/tags")
    async def tags():
        models = []
        for m in engine.store.list():
            models.append({"name": f"{m.repo}:latest", "model": f"{m.repo}:latest", "modified_at": m.completed_at,
                           "size": m.size_bytes, "digest": m.revision,
                           "details": _details(m.repo, m.kind, m.size_bytes, m.quant_bits)})
        for p in engine.presets.list():
            models.append({"name": p.name, "model": p.name, "modified_at": "", "size": 0, "digest": "",
                           "details": {"format": "preset", "family": "preset"}})
        return {"models": models}

    @app.get("/api/ps")
    async def ps():
        lm = rt.loaded
        if lm is None:
            return {"models": []}
        minutes = engine.settings.idle_unload_minutes
        expires = (datetime.fromtimestamp(lm.last_used + minutes * 60, timezone.utc).isoformat().replace("+00:00", "Z")
                   if minutes else "0001-01-01T00:00:00Z")
        return {"models": [{"name": f"{lm.repo}:latest", "model": f"{lm.repo}:latest", "size": lm.size_bytes,
                            "size_vram": lm.size_bytes, "digest": "", "expires_at": expires,
                            "details": _details(lm.repo, lm.kind, lm.size_bytes, None)}]}

    @app.post("/api/show")
    async def show(request: Request):
        body = await _json(request)
        repo = RepoId.parse(_model_name(body))
        local = engine.store.get(repo)
        if local is None:
            return JSONResponse(status_code=404, content={"error": f"model '{repo}' not found"})
        caps = {"text": ["completion"], "vision": ["completion", "vision"], "embedding": ["embedding"],
                "speech": ["transcription"]}[local.kind]
        lm = rt.loaded
        if lm is not None and lm.repo == str(repo) and getattr(lm.tokenizer, "has_tool_calling", False):
            caps.append("tools")
        if lm is not None and lm.repo == str(repo) and getattr(lm.tokenizer, "has_thinking", False):
            caps.append("thinking")
        return {"modelfile": "", "parameters": "", "template": "", "capabilities": caps,
                "details": _details(local.repo, local.kind, local.size_bytes, local.quant_bits),
                "model_info": {"general.architecture": local.model_type,
                               "general.parameter_count": local.size_bytes},
                "modified_at": local.completed_at}

    async def chat_like(request: Request, oa_body: dict, stream: bool, endpoint: str, generate_mode: bool):
        req = ChatRequest.parse(oa_body)
        started = time.perf_counter()
        result = await start_chat(request, req, endpoint)
        if isinstance(result, JSONResponse):
            return JSONResponse(status_code=result.status_code,
                                content={"error": json.loads(result.body)["error"]["message"]})
        lm, events, first = result
        name = f"{lm.repo}:latest"

        def final_fields(ev: Any) -> dict:
            total = time.perf_counter() - started
            gen_s = ev.completion_tokens / ev.generation_tps if ev.generation_tps else 0.0
            prompt_s = ev.prompt_tokens / ev.prompt_tps if ev.prompt_tps else 0.0
            reason = "length" if ev.finish_reason == "length" else "stop"
            return {"done": True, "done_reason": reason, "total_duration": _ns(total), "load_duration": 0,
                    "prompt_eval_count": ev.prompt_tokens, "prompt_eval_duration": _ns(prompt_s),
                    "eval_count": ev.completion_tokens, "eval_duration": _ns(gen_s)}

        def piece(ev: Any) -> dict:
            if generate_mode:
                d: dict[str, Any] = {"model": name, "created_at": _now(), "response": ev.text, "done": False}
                if ev.reasoning:
                    d["thinking"] = ev.reasoning
                return d
            msg: dict[str, Any] = {"role": "assistant", "content": ev.text}
            if ev.reasoning:
                msg["thinking"] = ev.reasoning
            if ev.tool_calls:
                msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in ev.tool_calls]
            return {"model": name, "created_at": _now(), "message": msg, "done": False}

        if not stream:
            text, reasoning, final = await collect(first, events, request)
            from .runtime import GenEvent

            merged = GenEvent(text=text, reasoning=reasoning, tool_calls=final.tool_calls)
            out = piece(merged)
            out.update(final_fields(final))
            return out

        async def ndjson():
            ev = first
            try:
                while True:
                    if ev.finish_reason is not None:
                        last = piece(ev)
                        last.update(final_fields(ev))
                        yield json.dumps(last) + "\n"
                        break
                    if ev.text or ev.reasoning:
                        yield json.dumps(piece(ev)) + "\n"
                    ev = await events.__anext__()
            except StopAsyncIteration:
                pass
            except Exception as exc:  # noqa: BLE001
                yield json.dumps({"error": str(exc)}) + "\n"
            finally:
                await events.aclose()

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")


    async def preload(request: Request, model: str):
        """Empty chat/generate preloads a model; remote keys without load
        permission may only preload the resident model."""
        from .service import resolve_model
        await resolve_model(engine, model, allow_load=client_of(request).can_load_model)

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await _json(request)
        model = _model_name(body)
        messages = body.get("messages") or []
        if not messages:  # Ollama clients send an empty chat to preload a model
            await preload(request, model)
            return {"model": model, "created_at": _now(), "message": {"role": "assistant", "content": ""},
                    "done": True, "done_reason": "load"}
        oa = {"model": model, "messages": _convert_messages(messages), **_options_to_openai(body)}
        if body.get("tools"):
            oa["tools"] = body["tools"]
        return await chat_like(request, oa, body.get("stream", True) is not False, "/api/chat", False)

    @app.post("/api/generate")
    async def generate(request: Request):
        body = await _json(request)
        model = _model_name(body)
        prompt = body.get("prompt") or ""
        if not prompt and not body.get("images"):
            await preload(request, model)
            return {"model": model, "created_at": _now(), "response": "", "done": True, "done_reason": "load"}
        messages = []
        if body.get("system"):
            messages.append({"role": "system", "content": body["system"]})
        messages.append({"role": "user", "content": prompt, "images": body.get("images") or []})
        oa = {"model": model, "messages": _convert_messages(messages), **_options_to_openai(body)}
        return await chat_like(request, oa, body.get("stream", True) is not False, "/api/generate", True)

    @app.post("/api/embed")
    async def embed(request: Request):
        body = await _json(request)
        repo = RepoId.parse(_model_name(body))
        inputs = body.get("input")
        inputs = [inputs] if isinstance(inputs, str) else inputs
        if not isinstance(inputs, list) or not inputs or not all(isinstance(s, str) for s in inputs):
            raise ParseError("input must be a string or array of strings", "input")
        started = time.perf_counter()
        vectors, tokens = await asyncio.to_thread(rt.embed, repo, inputs)
        return {"model": str(repo), "embeddings": vectors, "prompt_eval_count": tokens,
                "total_duration": _ns(time.perf_counter() - started)}

    @app.post("/api/embeddings")
    async def embeddings_legacy(request: Request):
        body = await _json(request)
        repo = RepoId.parse(_model_name(body))
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ParseError("prompt is required", "prompt")
        vectors, _ = await asyncio.to_thread(rt.embed, repo, [prompt])
        return {"embedding": vectors[0]}

    @app.post("/api/pull")
    async def pull(request: Request):
        body = await _json(request)
        repo = RepoId.parse(_model_name(body))
        if engine.store.get(repo) is not None:
            job = None
        else:
            job = engine.downloads.start(repo)
        stream = body.get("stream", True) is not False

        async def progress():
            yield json.dumps({"status": "pulling manifest"}) + "\n"
            while job is not None and job.state in ("queued", "resolving", "downloading", "verifying"):
                yield json.dumps({"status": f"downloading {job.current_file or str(repo)}", "digest": job.revision,
                                  "total": job.total_bytes, "completed": job.done_bytes}) + "\n"
                await asyncio.sleep(1)
            if job is not None and job.state != "completed":
                yield json.dumps({"error": job.error or job.state}) + "\n"
                return
            yield json.dumps({"status": "success"}) + "\n"

        if stream:
            return StreamingResponse(progress(), media_type="application/x-ndjson")
        while job is not None and job.state in ("queued", "resolving", "downloading", "verifying"):
            await asyncio.sleep(1)
        if job is not None and job.state != "completed":
            return JSONResponse(status_code=500, content={"error": job.error or job.state})
        return {"status": "success"}

    @app.delete("/api/delete")
    async def delete(request: Request):
        body = await _json(request)
        repo = RepoId.parse(_model_name(body))
        lm = rt.loaded
        if lm is not None and lm.repo == str(repo):
            return JSONResponse(status_code=409, content={"error": "model is loaded; unload it first"})
        if not await asyncio.to_thread(engine.store.delete, repo):
            return JSONResponse(status_code=404, content={"error": f"model '{repo}' not found"})
        return {}
