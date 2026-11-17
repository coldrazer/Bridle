"""HTTP surface: an OpenAI-compatible API (/v1/*), an Ollama-compatible API (/api/*), and a
control API (/control/*) for the desktop app. Control calls require the per-launch token and
must come from this machine."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import math
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import __version__
from .catalog import catalog_view
from .domain import ChatRequest, CompletionRequest, EmbeddingRequest, ParseError, RepoId
from .history import RequestRecord
from .jobs import BenchmarkResult, parse_bench, parse_compare, run_benchmark
from .keys import RateLimited, parse_key_create
from .presets import parse_preset
from .runtime import ContextTooLong, GenEvent, MemoryGuardRefused, ModelHandle, ModelUnavailable, TemplateError
from .runtime_errors import FeatureUnavailable
from .service import ClientInfo, Engine, Recorder, apply_preset, prompt_summary, recorded, repo_or_none, \
    resolve_model, stream_job
from .settings import parse_settings_patch
from .structured import StructuredOutputUnavailable, validate
from .system_info import lan_address

log = logging.getLogger("bridle.api")
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# Host-header names a request from this machine may use. Anything else on a loopback connection
# is a DNS-rebinding attempt (a web page whose domain now resolves to 127.0.0.1).
TRUSTED_HOST_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_AUDIO_BYTES = 200 * 1024 * 1024


def error(status: int, message: str, code: str, err_type: str = "invalid_request_error",
          param: str | None = None, extra: dict | None = None, headers: dict | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"message": message, "type": err_type, "param": param, "code": code}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(status_code=status, content=body, headers=headers)


async def body_json(request: Request) -> Any:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2 * 1024 * 1024:
            raise HTTPException(413, "JSON request exceeds 2 MiB")
        raw.extend(chunk)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError("request body is not valid JSON") from exc


def _hostname(value: str) -> str:
    """Hostname part of a Host header or Origin ("http://[::1]:8080" -> "::1"), lowercased."""
    value = value.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def create_app(engine: Engine, control_hosts: frozenset[str] = LOOPBACK,
               loopback_hosts: frozenset[str] = LOOPBACK,
               trusted_host_names: frozenset[str] = TRUSTED_HOST_NAMES) -> FastAPI:
    cfg = engine.config
    rt = engine.runtime

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def housekeeping() -> None:
            while True:
                await asyncio.sleep(10)
                engine.keys.flush()
                if engine.discovery is not None:
                    engine.discovery.update(engine.bonjour_props())

        task = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            intelligence = getattr(app.state, "intelligence", None)
            if intelligence is not None:
                await asyncio.to_thread(intelligence.close)
            engine.keys.flush()
            if engine.mesh is not None and engine.mesh.proc is not None:
                await asyncio.to_thread(engine.mesh.stop)
            if engine.discovery is not None:
                engine.discovery.stop()

    app = FastAPI(title="Bridle engine", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)

    # ---- auth ----------------------------------------------------------------------------------
    class Guard:
        """Auth for /control, /v1 and /api as plain ASGI middleware (unlike @app.middleware it
        doesn't wrap the receive channel, so handlers still see client disconnects)."""

        def __init__(self, inner):
            self.inner = inner
            self.active = 0
            self.waiting = 0
            Guard.instance = self

        @staticmethod
        def fail(scope, status, message, code, err_type="invalid_request_error", headers=None):
            """Anthropic-shaped errors on /v1/messages*, OpenAI shape elsewhere."""
            if scope.get("path", "").startswith("/v1/messages"):
                from .anthropic import anthropic_error
                response = anthropic_error(status, message)
                if headers:
                    for k, v in headers.items():
                        response.headers[k] = v
                return response
            return error(status, message, code, err_type, headers=headers)

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            rejected = self.check(scope)
            if rejected is not None:
                return await rejected(scope, receive, send)
            inference = scope["path"].startswith(("/v1", "/api")) \
                and scope["path"] not in ("/api/pull", "/api/delete")
            if inference:
                # Queue instead of failing fast (settings-tunable): excess
                # inference requests wait for a slot up to queue_wait_seconds.
                # Note: a client that disconnects while queued is only noticed
                # when its slot arrives (first event then cancels generation).
                limit = engine.settings.max_concurrent_requests
                deadline = time.monotonic() + engine.settings.queue_wait_seconds
                self.waiting += 1
                try:
                    while self.active >= limit:
                        if time.monotonic() >= deadline:
                            return await self.fail(scope, 429, "inference capacity reached; retry later",
                                                   "overloaded", headers={"retry-after": "5"})(
                                scope, receive, send)
                        await asyncio.sleep(0.05)
                finally:
                    self.waiting -= 1
                self.active += 1
            try:
                return await self.inner(scope, receive, send)
            finally:
                if inference:
                    self.active -= 1

        def check(self, scope) -> JSONResponse | None:
            path = scope["path"]
            host = scope["client"][0] if scope.get("client") else ""
            auth = host_header = api_key_header = ""
            content_type = ""
            content_length = 0
            origin: str | None = None
            for k, v in scope.get("headers") or []:
                if k == b"authorization":
                    auth = v.decode("latin-1")
                elif k == b"x-api-key":
                    # Anthropic-shaped clients (Claude Code) authenticate this way.
                    api_key_header = v.decode("latin-1")
                elif k == b"host":
                    host_header = v.decode("latin-1")
                elif k == b"origin":
                    origin = v.decode("latin-1")
                elif k == b"content-type":
                    content_type = v.decode("latin-1").split(";")[0].strip().lower()
                elif k == b"content-length":
                    try:
                        content_length = int(v.decode("latin-1"))
                    except ValueError:
                        content_length = 0
                elif k == b"transfer-encoding":
                    content_length = max(content_length, 1)  # chunked body, length unknown
            # Try every credential presented (bearer, then x-api-key): gateways
            # and harnesses may send both, and neither should shadow the other.
            candidates: list[str] = []
            if auth.lower().startswith("bearer ") and auth[7:].strip():
                candidates.append(auth[7:].strip())
            if api_key_header and api_key_header not in candidates:
                candidates.append(api_key_header)

            def is_control() -> bool:
                return any(hmac.compare_digest(t, cfg.control_token) for t in candidates)

            def matches_shared() -> bool:
                return bool(cfg.api_key) and any(hmac.compare_digest(t, cfg.api_key) for t in candidates)

            def authenticate_any():
                for candidate in candidates:
                    found = engine.keys.authenticate(candidate)
                    if found is not None:
                        return found
                return None
            # Browser defenses. API clients (curl, SDKs, the app) send no Origin; browsers always
            # do on cross-site requests, so a foreign Origin means a web page is calling us (CSRF).
            if origin is not None and _hostname(origin) not in trusted_host_names \
                    and origin.strip().rstrip("/").lower() not in cfg.allowed_origins:
                return self.fail(scope, 403, "requests from web pages are not allowed; set BRIDLE_ALLOWED_ORIGINS "
                                  "to permit a specific origin", "forbidden_origin", "permission_error")
            # A loopback connection must name this machine; any other Host is DNS rebinding.
            if host in loopback_hosts and _hostname(host_header) not in trusted_host_names:
                return self.fail(scope, 403, "unrecognized Host header for a local request", "forbidden_host",
                             "permission_error")
            # JSON APIs require a JSON content type on requests with a body. Browsers'
            # no-preflight "simple" cross-site POSTs use text/plain or form types, so this
            # closes that CSRF shape even if an Origin check is ever bypassed. Multipart
            # audio uploads are the only non-JSON bodies.
            if scope.get("method") in ("POST", "PUT", "PATCH", "DELETE") and content_length > 0 \
                    and path not in ("/v1/audio/transcriptions", "/control/aria/transcribe") \
                    and path.startswith(("/v1", "/api", "/control", "/mesh")) \
                    and content_type != "application/json":
                return self.fail(scope, 415, "request body must be application/json", "unsupported_media_type")
            if path.startswith("/control"):
                if host not in control_hosts:
                    return self.fail(scope, 403, "control API is only available from this machine", "forbidden", "permission_error")
                if not is_control():
                    return self.fail(scope, 401, "invalid control token", "unauthorized", "authentication_error")
            elif path.startswith("/mesh"):
                # Member API for another Mac's mesh coordinator: always a key with the mesh grant,
                # and HTTPS unless the call comes from this machine.
                if host not in loopback_hosts and scope.get("scheme") != "https":
                    return self.fail(scope, 426, "mesh control requires HTTPS; enable TLS in Settings", "https_required")
                key = authenticate_any()
                if key is None:
                    return self.fail(scope, 401, "missing or invalid API key", "invalid_api_key", "authentication_error")
                if not key.allow_mesh:
                    return self.fail(scope, 403, "this API key is not allowed to control the mesh", "forbidden", "permission_error")
                scope.setdefault("state", {})["client"] = ClientInfo(key.name, host, key=key, local=host in loopback_hosts)
            elif path.startswith("/v1") or path.startswith("/api"):
                if path.startswith("/api") and not engine.settings.ollama_api:
                    return self.fail(scope, 404, "the Ollama-compatible API is turned off", "not_found")
                if path in ("/api/pull", "/api/delete"):
                    # Management, not inference: loopback alone is not enough (any local
                    # process could otherwise pull/delete models). Require the control
                    # token, the shared key, or any named API key.
                    if host not in loopback_hosts:
                        return self.fail(scope, 403, "model pull and delete are only available from this machine",
                                     "forbidden", "permission_error")
                    management_ok = is_control() or matches_shared() or authenticate_any() is not None
                    if not management_ok:
                        return self.fail(scope, 401, "model pull and delete require an API key or the control "
                                          "token; use Bridle's Models screen or pass a key",
                                     "unauthorized", "authentication_error")
                is_local = host in loopback_hosts
                if not is_local and cfg.host not in LOOPBACK and scope.get("scheme") != "https":
                    return self.fail(scope, 426, "remote inference requires HTTPS; enable TLS in Settings", "https_required")
                client = ClientInfo("local" if is_local else "anonymous", host, local=is_local)
                need_key = (not is_local and cfg.host not in LOOPBACK) or (\
                    bool(cfg.api_key or engine.keys.has_active_keys()) and \
                    (not is_local or engine.settings.local_requires_key))
                if matches_shared():
                    client.name = "default key"
                elif candidates and (key := authenticate_any()) is not None:
                    try:
                        engine.keys.admit(key)
                    except RateLimited as exc:
                        return self.fail(scope, 429, str(exc), "rate_limited", "rate_limit_error",
                                     headers={"retry-after": str(exc.retry_after)})
                    client.key, client.name = key, key.name
                elif need_key:
                    return self.fail(scope, 401, "missing or invalid API key", "invalid_api_key", "authentication_error")
                scope.setdefault("state", {})["client"] = client
            elif path == "/metrics":
                if host not in loopback_hosts:
                    return self.fail(scope, 403, "metrics are only available from this machine",
                                 "forbidden", "permission_error")
            elif path in ("/", "/health"):
                pass  # deliberately public: reachability checks only, no state exposed
            else:
                # Deny by default: an unrecognised prefix is never silently public.
                return self.fail(scope, 404, "not found", "not_found")
            return None

    app.add_middleware(Guard)

    def client_of(request: Request) -> ClientInfo:
        return getattr(request.state, "client", None) or ClientInfo("local", "")

    def record_failure(request: Request, status: int, message: str) -> None:
        path = request.url.path
        if path.startswith(("/v1", "/api")):
            c = client_of(request)
            try:
                engine.history.add(RequestRecord(endpoint=path, status=status, client=c.name, remote=c.remote,
                                                 error=message[:500]), False)
            except Exception:  # noqa: BLE001
                pass

    # ---- error mapping ----------------------------------------------------------------------------
    @app.exception_handler(ParseError)
    async def on_parse_error(request: Request, exc: ParseError):
        record_failure(request, 400, str(exc))
        return error(400, str(exc), "invalid_request", param=exc.param)

    @app.exception_handler(ModelUnavailable)
    async def on_unavailable(request: Request, exc: ModelUnavailable):
        record_failure(request, exc.status, str(exc))
        return error(exc.status, str(exc), "model_not_available", param="model")

    @app.exception_handler(MemoryGuardRefused)
    async def on_memory(request: Request, exc: MemoryGuardRefused):
        record_failure(request, 409, str(exc))
        return error(409, str(exc), "insufficient_memory", extra={"check": exc.check})

    @app.exception_handler(FeatureUnavailable)
    async def on_feature(request: Request, exc: FeatureUnavailable):
        record_failure(request, 501, str(exc))
        return error(501, str(exc), "feature_not_installed", "server_error")

    @app.exception_handler(StructuredOutputUnavailable)
    async def on_structured(request: Request, exc: StructuredOutputUnavailable):
        return error(501, str(exc), "feature_not_installed", "server_error")

    @app.exception_handler(TemplateError)
    async def on_template(request: Request, exc: TemplateError):
        record_failure(request, 400, str(exc))
        return error(400, str(exc), "chat_template_error")

    # ---- chat pipeline -----------------------------------------------------------------------------
    async def first_event(stream: AsyncIterator[GenEvent], request: Request) -> GenEvent | JSONResponse:
        try:
            task = asyncio.create_task(stream.__anext__())
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.1)
                    if not task.done() and await request.is_disconnected():
                        task.cancel()
                        raise asyncio.CancelledError("client disconnected")
                return task.result()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        except ContextTooLong as exc:
            return error(400, str(exc), "context_length_exceeded", param="messages")
        except ModelUnavailable as exc:
            return error(503, str(exc), "model_not_available")
        except (ValueError, StructuredOutputUnavailable) as exc:
            return error(400, str(exc), "invalid_request")
        except StopAsyncIteration:
            return GenEvent(finish_reason="stop")

    async def start_chat(request: Request, req: ChatRequest, endpoint: str) \
            -> tuple[ModelHandle, AsyncIterator[GenEvent], GenEvent] | JSONResponse:
        req, _ = apply_preset(req, engine.presets)
        lm = await resolve_model(engine, req.model, allow_load=client_of(request).can_load_model)
        images = tuple(u for m in req.messages for u in m.images)
        if images and lm.kind != "vision":
            raise ParseError(f"{lm.repo} can't see images; load a vision model (e.g. Qwen2.5-VL or Gemma 3)",
                             "messages")
        tools = req.tools if lm.kind == "text" else ()
        prompt = rt.chat_prompt(lm, req.messages, tools, req.response_format)
        client = client_of(request)
        if client.key is not None:
            try:
                client.reserved_tokens = engine.keys.reserve(client.key, req.params.max_tokens)
            except RateLimited as exc:
                return error(429, str(exc), "rate_limited", "rate_limit_error",
                             headers={"retry-after": str(exc.retry_after)})
        recorder = Recorder(engine, endpoint, client, lm.repo, prompt_summary(req.messages))
        events = recorded(stream_job(rt, lm, prompt, req.params, tools=tools, response_format=req.response_format,
                                     images=images, cache_partition=client.key.id if client.key else "local"), recorder)
        first = await first_event(events, request)
        if isinstance(first, JSONResponse):
            recorder.fail(first.status_code, json.loads(first.body)["error"]["message"])
            return first
        return lm, events, first

    async def collect(first: GenEvent, rest: AsyncIterator[GenEvent],
                      request: Request | None = None) -> tuple[str, str, GenEvent]:
        """Gathers a whole generation. If the client disconnects while waiting, the generation
        is cancelled instead of running to completion for nobody."""
        text, reasoning, final = [first.text], [first.reasoning], first
        if first.finish_reason is not None:
            return "".join(text), "".join(reasoning), final

        async def drain() -> None:
            nonlocal final
            async for ev in rest:
                text.append(ev.text)
                reasoning.append(ev.reasoning)
                final = ev

        task = asyncio.create_task(drain())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.5)
                if request is not None and not task.done() and await request.is_disconnected():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await rest.aclose()
                    raise asyncio.CancelledError("client disconnected")
            task.result()
        finally:
            if not task.done():
                task.cancel()
        return "".join(text), "".join(reasoning), final

    def usage(ev: GenEvent) -> dict:
        return {"prompt_tokens": ev.prompt_tokens, "completion_tokens": ev.completion_tokens,
                "total_tokens": ev.prompt_tokens + ev.completion_tokens,
                "prompt_tokens_details": {"cached_tokens": ev.cached_tokens}}

    def perf(ev: GenEvent) -> dict:
        return {"prompt_tps": ev.prompt_tps, "generation_tps": ev.generation_tps,
                "peak_memory_gb": ev.peak_memory_gb, "cached_tokens": ev.cached_tokens,
                "draft_accepted_tokens": ev.draft_accepted, "batched": ev.batched}

    def finish_reason(ev: GenEvent) -> str:
        return ev.finish_reason if ev.finish_reason in ("length", "tool_calls") else "stop"

    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # ---- OpenAI-compatible API ----------------------------------------------------------------
    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/v1/models")
    async def list_models():
        loaded = rt.loaded
        data = [{"id": m.repo, "object": "model", "created": int(time.time()), "owned_by": m.repo.split("/", 1)[0],
                 "kind": m.kind, "loaded": bool(loaded and loaded.repo == m.repo)} for m in engine.store.list()]
        data += [{"id": f"preset:{p.name}", "object": "model", "created": int(time.time()), "owned_by": "preset",
                  "kind": "preset", "loaded": False} for p in engine.presets.list()]
        if engine.mesh is not None and engine.mesh.state == "ready" and engine.mesh.rank == 0:
            data.insert(0, {"id": engine.mesh.config.get("model"), "object": "model", "created": int(time.time()),
                            "owned_by": "mesh", "kind": "mesh", "loaded": True,
                            "mesh": {"world": engine.mesh.config.get("world"), "backend": engine.mesh.config.get("backend")}})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        data = await body_json(request)
        if isinstance(data, dict) and engine.mesh is not None and engine.mesh.serves(data.get("model")):
            from .mesh_session import proxy
            return await proxy(engine, data, "/v1/chat/completions")
        req = ChatRequest.parse(data)
        started = await start_chat(request, req, "/v1/chat/completions")
        if isinstance(started, JSONResponse):
            return started
        lm, events, first = started
        cid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())

        if not req.stream:
            text, reasoning, final = await collect(first, events, request)
            message: dict[str, Any] = {"role": "assistant", "content": text if (text or not final.tool_calls) else None}
            if reasoning:
                message["reasoning_content"] = reasoning
            if final.tool_calls:
                message["tool_calls"] = [c.openai() for c in final.tool_calls]
            body = {"id": cid, "object": "chat.completion", "created": created, "model": lm.repo,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish_reason(final)}],
                    "usage": usage(final), "bridle": perf(final)}
            if req.response_format is not None:
                problem = validate(text, req.response_format)
                if problem:
                    body["bridle"]["structured_output_error"] = problem
            return body

        def chunk(delta: dict, reason: str | None = None) -> str:
            return sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": lm.repo,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]})

        async def stream():
            yield chunk({"role": "assistant", "content": ""})
            ev = first
            try:
                while True:
                    if ev.reasoning:
                        yield chunk({"reasoning_content": ev.reasoning})
                    if ev.text:
                        yield chunk({"content": ev.text})
                    if ev.finish_reason is not None:
                        if ev.tool_calls:
                            yield chunk({"tool_calls": [c.openai(i) for i, c in enumerate(ev.tool_calls)]})
                        yield chunk({}, finish_reason(ev))
                        if req.include_usage:
                            yield sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                                       "model": lm.repo, "choices": [], "usage": usage(ev), "bridle": perf(ev)})
                        break
                    ev = await events.__anext__()
            except StopAsyncIteration:
                yield chunk({}, "stop")
            except Exception as exc:  # noqa: BLE001 - report mid-stream failures in-band
                log.exception("generation failed mid-stream")
                yield sse({"error": {"message": str(exc), "type": "server_error"}})
            finally:
                await events.aclose()
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    @app.post("/v1/completions")
    async def completions(request: Request):
        data = await body_json(request)
        if isinstance(data, dict) and engine.mesh is not None and engine.mesh.serves(data.get("model")):
            from .mesh_session import proxy
            return await proxy(engine, data, "/v1/completions")
        req = CompletionRequest.parse(data)
        lm = await resolve_model(engine, req.model, allow_load=client_of(request).can_load_model)
        if lm.kind != "text":
            raise ParseError("/v1/completions needs a text model", "model")
        client = client_of(request)
        if client.key is not None:
            try:
                client.reserved_tokens = engine.keys.reserve(client.key, req.params.max_tokens)
            except RateLimited as exc:
                return error(429, str(exc), "rate_limited", "rate_limit_error",
                             headers={"retry-after": str(exc.retry_after)})
        recorder = Recorder(engine, "/v1/completions", client, lm.repo, req.prompt[-20_000:])
        events = recorded(stream_job(rt, lm, req.prompt, req.params, raw_text=True,
                                     cache_partition=client.key.id if client.key else "local"), recorder)
        first = await first_event(events, request)
        if isinstance(first, JSONResponse):
            recorder.fail(first.status_code, json.loads(first.body)["error"]["message"])
            return first
        cid, created = f"cmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        if not req.stream:
            text, _, final = await collect(first, events, request)
            return {"id": cid, "object": "text_completion", "created": created, "model": lm.repo,
                    "choices": [{"index": 0, "text": text, "finish_reason": finish_reason(final), "logprobs": None}],
                    "usage": usage(final), "bridle": perf(final)}

        async def stream():
            ev = first
            try:
                while True:
                    reason = finish_reason(ev) if ev.finish_reason is not None else None
                    if ev.text or reason:
                        yield sse({"id": cid, "object": "text_completion", "created": created, "model": lm.repo,
                                   "choices": [{"index": 0, "text": ev.text, "finish_reason": reason, "logprobs": None}]})
                    if reason:
                        break
                    ev = await events.__anext__()
            except StopAsyncIteration:
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("generation failed mid-stream")
                yield sse({"error": {"message": str(exc), "type": "server_error"}})
            finally:
                await events.aclose()
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        req = EmbeddingRequest.parse(await body_json(request))
        recorder = Recorder(engine, "/v1/embeddings", client_of(request), req.model, "")
        try:
            vectors, tokens = await asyncio.to_thread(rt.embed, RepoId.parse(req.model), list(req.inputs))
        except Exception as exc:
            recorder.fail(getattr(exc, "status", 500), str(exc))
            raise
        recorder.rec.prompt_tokens = tokens
        recorder.finish()
        data = []
        for i, v in enumerate(vectors):
            emb: Any = v if req.encoding_format == "float" else \
                base64.b64encode(np.asarray(v, dtype=np.float32).tobytes()).decode()
            data.append({"object": "embedding", "index": i, "embedding": emb})
        return {"object": "list", "data": data, "model": req.model,
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        from .audio import to_srt, to_vtt

        # Reject before multipart parsing spools the whole upload to disk.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() \
                and int(declared) > MAX_AUDIO_BYTES + 1024 * 1024:  # + multipart overhead
            raise ParseError("audio file is larger than 200 MB", "file")
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise ParseError("multipart field 'file' is required", "file")
        model = str(RepoId.parse(form.get("model") or ""))
        fmt = form.get("response_format") or "json"
        if fmt not in ("json", "text", "srt", "vtt", "verbose_json"):
            raise ParseError("response_format must be json, text, srt, vtt or verbose_json", "response_format")
        temperature_raw = form.get("temperature")
        temperature: float | None = None
        if temperature_raw:
            try:
                temperature = float(temperature_raw)
            except (TypeError, ValueError) as exc:
                raise ParseError("temperature must be a number", "temperature") from exc
            if not math.isfinite(temperature) or not 0.0 <= temperature <= 1.0:
                raise ParseError("temperature must be a number in [0, 1]", "temperature")
        recorder = Recorder(engine, "/v1/audio/transcriptions", client_of(request), model, "")
        raw_suffix = Path(getattr(upload, "filename", "") or "audio.wav").suffix
        suffix = raw_suffix if raw_suffix[1:].isalnum() and len(raw_suffix) <= 10 else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            size = 0
            while chunk := await upload.read(1 << 20):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise ParseError("audio file is larger than 200 MB", "file")
                tmp.write(chunk)
            tmp.flush()
            try:
                result = await asyncio.to_thread(rt.transcribe, RepoId.parse(model), tmp.name,
                                                 language=form.get("language") or None,
                                                 prompt=form.get("prompt") or None,
                                                 temperature=temperature)
            except Exception as exc:
                recorder.fail(500, str(exc))
                raise
        recorder.parts.append(result["text"])
        recorder.finish()
        if fmt == "text":
            return PlainTextResponse(result["text"])
        if fmt == "srt":
            return PlainTextResponse(to_srt(result))
        if fmt == "vtt":
            return PlainTextResponse(to_vtt(result), media_type="text/vtt")
        if fmt == "verbose_json":
            return {"task": "transcribe", **result}
        return {"text": result["text"]}

    # ---- Ollama-compatible API -------------------------------------------------------------------
    from .ollama import register as register_ollama

    register_ollama(app, engine, start_chat, collect, client_of)

    # ---- Anthropic-compatible API (Claude Code via ANTHROPIC_BASE_URL) ----------------------------
    from .anthropic import register as register_anthropic

    register_anthropic(app, engine, start_chat, collect, client_of)

    # ---- OpenAI Responses API (Codex and newer SDKs) ------------------------------------------------
    from .responses import register as register_responses

    register_responses(app, engine, start_chat, collect, client_of)

    # ---- control API: status & models ------------------------------------------------------------
    @app.get("/control/status")
    async def status():
        stats = engine.sampler.sample().to_json()
        stats["mlx"] = rt.memory()
        return {
            "version": __version__,
            "server": {
                "state": "running", "mode": "standalone", "host": cfg.host, "port": engine.bound_port,
                "endpoint": engine.endpoint(), "https_endpoint": engine.https_endpoint(),
                "tls_fingerprint": engine.tls_fingerprint or None, "lan_address": lan_address(),
                "api_key_required": bool(cfg.api_key) or engine.keys.has_active_keys(),
                "auto_load": cfg.auto_load, "started_at": engine.started_at,
                "uptime_seconds": int(time.time() - engine.started_at), "models_dir": str(cfg.models_dir),
                "ollama_api": engine.settings.ollama_api,
                "inference": {"active": getattr(Guard, "instance", None).active if getattr(Guard, "instance", None) else 0,
                              "waiting": getattr(Guard, "instance", None).waiting if getattr(Guard, "instance", None) else 0,
                              "limit": engine.settings.max_concurrent_requests,
                              "queue_wait_seconds": engine.settings.queue_wait_seconds},
                "store_load_errors": {name: err for name, err in (
                    ("api_keys", engine.keys.load_error), ("settings", engine.settings_store.load_error),
                    ("presets", engine.presets.load_error)) if err},
                "bonjour": bool(engine.discovery and not engine.discovery.error),
            },
            "machine": engine.machine.to_json(),
            "stats": stats,
            "model": rt.status(),
            "downloads": [j.to_json(engine.store) for j in engine.downloads.list()],
            "aria": rt.aria_status(),
            "onyx": rt.onyx_status(),
            "features": engine.features(),
            "peers": len(engine.discovery.peers()) if engine.discovery else 0,
            "usage": engine.history.summary(),
            "mesh": engine.mesh.status() if engine.mesh is not None else {"state": "idle"},
        }

    @app.get("/control/catalog")
    async def catalog():
        return {"models": catalog_view(engine.store, engine.machine.memory_total_bytes)}

    async def body_dict(request: Request) -> dict:
        body = await body_json(request)
        if not isinstance(body, dict):
            raise ParseError("request body must be a JSON object")
        return body

    @app.get("/control/models/check")
    async def check_model(repo: str, draft: str = ""):
        return rt.check_fit(RepoId.parse(repo), repo_or_none(draft))

    @app.post("/control/models/load")
    async def load_model(request: Request):
        body = await body_dict(request)
        repo, draft = RepoId.parse(body.get("repo")), repo_or_none(body.get("draft"))
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise ParseError("force must be a boolean", "force")
        if engine.mesh is not None and engine.mesh.active:
            return error(409, "this Mac's memory is in use by a mesh; stop the mesh first", "mesh_active")
        rt.load_in_background(repo, draft, force)
        return JSONResponse(status_code=202, content={"state": "loading", "repo": str(repo),
                                                      "draft": str(draft) if draft else None})

    @app.post("/control/models/unload")
    async def unload_model():
        await asyncio.to_thread(rt.unload)
        return {"state": "empty"}

    @app.delete("/control/models/{owner}/{name}")
    async def delete_model(owner: str, name: str):
        repo = RepoId.parse(f"{owner}/{name}")
        loaded = rt.loaded
        if loaded and str(repo) in (loaded.repo, loaded.draft.repo if loaded.draft else None):
            return error(409, "unload the model before deleting it", "model_in_use")
        if not await asyncio.to_thread(engine.store.delete, repo):
            return error(404, f"{repo} is not downloaded", "model_not_downloaded")
        log.info("deleted %s", repo)
        return {"deleted": str(repo)}

    # ---- downloads ----------------------------------------------------------------------------
    @app.post("/control/downloads")
    async def start_download(request: Request):
        repo = RepoId.parse((await body_dict(request)).get("repo"))
        job = engine.downloads.start(repo)
        log.info("download queued: %s", repo)
        return JSONResponse(status_code=202, content=job.to_json(engine.store))

    @app.delete("/control/downloads/{job_id}")
    async def cancel_download(job_id: str):
        if not engine.downloads.cancel(job_id):
            return error(404, "no active download with that id", "not_found")
        return {"cancelled": job_id}

    @app.post("/control/downloads/clear")
    async def clear_downloads():
        engine.downloads.clear_finished()
        return {"ok": True}

    @app.get("/control/logs")
    async def logs(after: int = 0):
        lines, last = engine.logs.since(after)
        return {"lines": lines, "next": last}

    # ---- settings -------------------------------------------------------------------------------
    @app.get("/control/settings")
    async def get_settings():
        return engine.settings.to_json()

    @app.patch("/control/settings")
    async def patch_settings(request: Request):
        patch = parse_settings_patch(await body_json(request))
        current, restart = engine.settings_store.update(patch)
        if "prompt_cache" in patch and not patch["prompt_cache"]:
            rt.run_on_mlx_thread(rt._pcache.clear)
        return {"settings": current.to_json(), "restart_required": restart}

    # ---- API keys ---------------------------------------------------------------------------------
    @app.get("/control/keys")
    async def list_keys():
        return {"keys": [k.public() for k in engine.keys.list()]}

    @app.post("/control/keys")
    async def create_key(request: Request):
        data = await body_json(request)
        name, rpm, daily, allow_load = parse_key_create(data)
        allow_mesh = data.get("allow_mesh", False)
        if not isinstance(allow_mesh, bool):
            raise ParseError("allow_mesh must be a boolean", "allow_mesh")
        key, secret = engine.keys.create(name, rpm, daily, allow_load, allow_mesh)
        log.info("created API key '%s'", name)
        return JSONResponse(status_code=201, content={"key": key.public(), "secret": secret})

    @app.post("/control/keys/{key_id}/revoke")
    async def revoke_key(key_id: str):
        if not engine.keys.revoke(key_id):
            return error(404, "no key with that id", "not_found")
        return {"revoked": key_id}

    @app.delete("/control/keys/{key_id}")
    async def delete_key(key_id: str):
        if not engine.keys.delete(key_id):
            return error(404, "no key with that id", "not_found")
        return {"deleted": key_id}

    # ---- request history ----------------------------------------------------------------------------
    @app.get("/control/requests")
    async def list_requests(limit: int = 100, before: int = 0, model: str = "", client: str = ""):
        return {"requests": engine.history.list(limit, before or None, model, client),
                "summary": engine.history.summary()}

    @app.delete("/control/requests")
    async def clear_requests():
        engine.history.clear()
        return {"ok": True}

    # ---- presets ---------------------------------------------------------------------------------
    @app.get("/control/presets")
    async def list_presets():
        return {"presets": [p.to_json() for p in engine.presets.list()]}

    @app.put("/control/presets")
    async def put_preset(request: Request):
        preset = parse_preset(await body_json(request))
        engine.presets.put(preset)
        return preset.to_json()

    @app.delete("/control/presets/{name}")
    async def delete_preset(name: str):
        if not engine.presets.delete(name):
            return error(404, f"no preset named {name!r}", "not_found")
        return {"deleted": name}

    # ---- benchmarks -------------------------------------------------------------------------------
    @app.get("/control/benchmarks")
    async def list_benchmarks():
        return {"benchmarks": engine.benchmarks.list()}

    @app.post("/control/benchmarks")
    async def run_bench(request: Request):
        prompt_tokens, gen_tokens = parse_bench(await body_json(request))
        lm = rt.loaded
        if lm is None or lm.kind != "text":
            return error(409, "load a text model to benchmark it", "no_model")
        result: BenchmarkResult = await asyncio.to_thread(run_benchmark, rt, lm, engine.machine.chip,
                                                          prompt_tokens, gen_tokens)
        engine.benchmarks.add(result)
        log.info("benchmark %s: %.1f tok/s generation, %.1f tok/s prompt", lm.repo, result.generation_tps,
                 result.prompt_tps)
        return result.__dict__

    @app.delete("/control/benchmarks")
    async def clear_benchmarks():
        engine.benchmarks.clear()
        return {"ok": True}

    # ---- compare -----------------------------------------------------------------------------------
    @app.post("/control/compare")
    async def start_compare(request: Request):
        job = engine.compare.start(parse_compare(await body_json(request)))
        return JSONResponse(status_code=202, content=job.to_json())

    @app.get("/control/compare")
    async def list_compare():
        return {"jobs": [j.to_json() for j in engine.compare.list()]}

    @app.get("/control/compare/{job_id}")
    async def get_compare(job_id: str):
        job = engine.compare.get(job_id)
        if job is None:
            return error(404, "no comparison with that id", "not_found")
        return job.to_json()

    @app.delete("/control/compare/{job_id}")
    async def cancel_compare(job_id: str):
        if not engine.compare.cancel(job_id):
            return error(404, "no running comparison with that id", "not_found")
        return {"cancelled": job_id}

    # ---- network ------------------------------------------------------------------------------------
    @app.get("/control/peers")
    async def peers():
        d = engine.discovery
        return {"peers": d.peers() if d else [], "advertising": bool(d and d._info is not None),
                "error": d.error if d else ("bonjour is off" if not engine.settings.bonjour else "")}

    from .intelligence import register_intelligence_routes
    from .operations import register_operations_routes
    register_intelligence_routes(app, engine)
    operations = register_operations_routes(app, engine)
    from .mesh_session import MeshSession, register_mesh_routes
    if engine.mesh is None:
        engine.mesh = MeshSession(engine)
    register_mesh_routes(app, engine, operations)
    from .imports import register_import_routes
    register_import_routes(app, engine)
    from .remote.routes import register_remote_routes
    register_remote_routes(app, engine)
    from .aria import register_aria_routes
    register_aria_routes(app, engine)
    from .production import register_production_routes
    register_production_routes(app, engine)

    return app
