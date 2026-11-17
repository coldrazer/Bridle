"""Bounded, explicitly invoked operations for the local control plane.

No tool calls, peer requests, filesystem writes or model loads happen implicitly.
Peer credentials are private local state; paired device credentials live hashed in KeyStore.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
import stat
from itertools import islice
from pathlib import Path
import secrets
import ssl
import threading
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request

from . import __version__

LIMIT = 1024 * 1024
PROFILES = {
    "quiet": {"batching": False, "max_batch_size": 1, "idle_unload_minutes": 5},
    "balanced": {"batching": True, "max_batch_size": 4, "idle_unload_minutes": 15},
    "performance": {"batching": True, "max_batch_size": 8, "idle_unload_minutes": 30},
}
TEXT_SUFFIXES = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".py", ".js", ".ts", ".html", ".css"}


def fail(message, status=400):
    raise HTTPException(status, message)


def required(body, key, maximum=4096):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail(f"{key} must be a non-empty string (max {maximum} characters)")
    return value


def endpoint(value, https_only=False):
    parsed = urlsplit(value)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname
            or parsed.scheme not in ({"https"} if https_only else {"http", "https"})):
        fail("endpoint must be HTTPS without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        fail("HTTP is permitted only for a loopback MCP server")
    return value.rstrip("/")


async def _remote_json(url, payload, *, headers=None, ca_pem=None, response_headers=None):
    from . import egress
    egress.record(url, "peer/MCP request")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if ca_pem else ssl.create_default_context()
    if ca_pem:
        try:
            context.load_verify_locations(cadata=ca_pem)
        except (ssl.SSLError, ValueError):
            fail("invalid peer CA certificate")
    # No environment proxies, redirects or TLS verification bypasses.
    try:
        async with httpx.AsyncClient(verify=context, timeout=30, trust_env=False, follow_redirects=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 300:
                    fail(f"remote endpoint returned HTTP {response.status_code}", 502)
                if response_headers is not None:
                    response_headers.update(dict(response.headers))
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > LIMIT:
                        fail("remote response exceeds 1 MiB", 502)
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        # Return as soon as a complete matching event arrives; MCP
                        # servers may keep the HTTP stream open for notifications.
                        try:
                            complete = raw.decode().replace("\r\n", "\n").split("\n\n")[:-1]
                        except UnicodeDecodeError:
                            complete = []
                        for event in complete:
                            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                            if data:
                                message = json.loads(data)
                                if isinstance(message, dict) and message.get("id") == payload.get("id"):
                                    return message
                if not raw.strip():
                    return {}
                if "text/event-stream" in response.headers.get("content-type", ""):
                    for event in raw.decode().replace("\r\n", "\n").split("\n\n"):
                        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                        if data:
                            message = json.loads(data)
                            if isinstance(message, dict) and message.get("id") == payload.get("id"):
                                return message
                    fail("MCP response did not contain a matching JSON-RPC result", 502)
                return json.loads(raw)
    except (httpx.HTTPError, ValueError) as exc:
        fail(f"remote request failed: {type(exc).__name__}", 502)


async def bounded_json(url, payload, *, headers=None, ca_pem=None, response_headers=None):
    try:
        return await asyncio.wait_for(_remote_json(url, payload, headers=headers, ca_pem=ca_pem,
                                                  response_headers=response_headers), timeout=30)
    except asyncio.TimeoutError:
        fail("remote request exceeded the 30-second deadline", 504)


class CredentialVault:
    """Peer credentials in a private 0600 file. No Keychain: the file lives in
    the app's already-private data directory on every platform."""

    def __init__(self, directory):
        self.path = Path(directory) / "peer-credentials.json"
        self.storage = "private 0600 file"

    def _read(self):
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            fail("peer credential store is unreadable", 503)

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".peer-credentials-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(value))
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def put(self, identifier, secret):
        value = self._read()
        value[identifier] = secret
        self._write(value)

    def get(self, identifier):
        secret = self._read().get(identifier)
        if not secret:
            fail("peer credential missing; register the peer again", 503)
        return secret

    def delete(self, identifier):
        value = self._read()
        value.pop(identifier, None)
        self._write(value)


class Operations:
    def __init__(self, engine, vault=None):
        self.engine = engine
        self.vault = vault if vault is not None else CredentialVault(engine.config.data_dir)
        self.path = engine.config.data_dir / "operations.json"
        self.state = {"profile": "custom", "grants": {}, "mcp": {}, "peers": {}, "devices": {}}
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text())
                for key in self.state:
                    if isinstance(saved.get(key), type(self.state[key])):
                        self.state[key] = saved[key]
            except (ValueError, OSError):
                pass
        self.requests = OrderedDict()
        self.cancels = {}
        self.approvals = {}
        self.codes = {}
        self.mcp_sessions = {}
        self.lock = threading.RLock()
        self._instrument()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".operations-", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.state, indent=2))
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _instrument(self):
        runtime = self.engine.runtime
        original = runtime.submit_generation
        def submit(job):
            identifier = secrets.token_hex(8)
            record = {"id": identifier, "model": str(getattr(job.lm, "repo", "unknown")),
                      "submitted_at": time.time(), "status": "queued", "tokens": 0,
                      "first_token_at": None, "finished_at": None}
            emit = job.emit
            with self.lock:
                self.requests[identifier] = record
                self.cancels[identifier] = job.cancel
                # Keep active requests and a bounded terminal history.
                finished = [key for key, value in self.requests.items() if value["finished_at"]]
                for key in finished[:-100]:
                    self.requests.pop(key, None)
            def tracked(item):
                with self.lock:
                    if item is None:
                        record["finished_at"] = time.time()
                        if record["status"] != "failed":
                            record["status"] = "cancelled" if job.cancel.is_set() else "completed"
                        self.cancels.pop(identifier, None)
                    elif isinstance(item, Exception):
                        record["status"] = "failed"
                    else:
                        record["status"] = "cancelling" if job.cancel.is_set() else "generating"
                        if getattr(item, "text", "") or getattr(item, "reasoning", ""):
                            record["first_token_at"] = record["first_token_at"] or time.time()
                        if getattr(item, "completion_tokens", 0):
                            record["tokens"] = item.completion_tokens
                        record["generation_tps"] = getattr(item, "generation_tps", 0)
                        record["peak_memory_gb"] = getattr(item, "peak_memory_gb", 0)
                emit(item)
            job.emit = tracked
            try:
                return original(job)
            except Exception:
                with self.lock:
                    record.update(status="failed", finished_at=time.time())
                    self.cancels.pop(identifier, None)
                raise
        runtime.submit_generation = submit

    def snapshot(self):
        with self.lock:
            requests = [dict(value) for value in self.requests.values()]
        sample = self.engine.sampler.sample()
        return {"requests": requests, "cockpit": {"runtime": self.engine.runtime.status(),
                "system": asdict(sample) if is_dataclass(sample) else sample,
                "thermal_state": "unavailable", "power_watts": None},
                "profile": self.state["profile"], "profiles": PROFILES,
                "credential_storage": self.vault.storage,
                "grants": list(self.state["grants"].values()),
                "mcp": [{k: v for k, v in connection.items() if k != "session"} for connection in self.state["mcp"].values()],
                "peers": [{k: v for k, v in peer.items() if k not in {"api_key", "ca_pem"}} for peer in self.state["peers"].values()],
                "devices": list(self.state["devices"].values()),
                "limits": {"request_tracking": "Generation jobs submitted since operations startup; queued includes prompt prefill before first event.",
                           "profiles": "Software scheduling and idle unload only; no OS power or thermal control.",
                           "routing": "Explicit whole non-streaming chat request to one approved HTTPS peer; no tensor sharding."}}

    def grant_path(self, body, write=False):
        grant = self.state["grants"].get(required(body, "grant_id"))
        if not grant or (write and not grant["write"]):
            fail("missing or revoked filesystem permission", 403)
        relative = Path(required(body, "path"))
        if relative.is_absolute() or ".." in relative.parts:
            fail("path must be relative to the granted folder")
        root = Path(grant["path"])
        if root.is_symlink():
            fail("granted folder changed", 403)
        # Compare resolved-to-resolved so legitimate prefixes that are
        # themselves behind a symlink (e.g. /tmp -> /private/tmp) still work.
        root_resolved = root.resolve()
        target = root_resolved / relative
        current = root_resolved
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                fail("symlinks are not permitted", 403)
        if not target.resolve().is_relative_to(root_resolved):
            fail("path escapes granted folder", 403)
        return target

    def tool(self, action, body):
        target = self.grant_path(body, write=action in {"preview-write", "commit-write"})
        # Walk every directory using descriptors and O_NOFOLLOW. Holding the final
        # parent descriptor prevents a concurrent symlink swap escaping the grant.
        parent = target if action == "list" else target.parent
        descriptor = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if action == "list":
                with os.scandir(descriptor) as entries:
                    return {"entries": [{"name": item.name, "directory": item.is_dir(follow_symlinks=False),
                                         "symlink": item.is_symlink()} for item in islice(entries, 200)]}
            if target.suffix.lower() not in TEXT_SUFFIXES:
                fail("only allowlisted text file extensions are supported")
            def read_existing():
                try:
                    handle = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                except FileNotFoundError:
                    return None
                with os.fdopen(handle, "rb") as file:
                    info = os.fstat(file.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                        fail("file must be regular text no larger than 1 MiB")
                    raw = file.read(LIMIT + 1)
                    if len(raw) > LIMIT:
                        fail("file exceeds 1 MiB")
                    return raw
            previous = read_existing()
            if action == "read":
                if previous is None:
                    fail("file does not exist")
                try:
                    return {"content": previous.decode("utf-8")}
                except UnicodeError:
                    fail("file is not UTF-8 text")
            content = body.get("content")
            if not isinstance(content, str) or len(content.encode()) > LIMIT:
                fail("content must be UTF-8 text no larger than 1 MiB")
            digest = hashlib.sha256(content.encode()).hexdigest()
            before = hashlib.sha256(previous).hexdigest() if previous is not None else None
            if action == "preview-write":
                with self.lock:
                    self.approvals = {key: value for key, value in self.approvals.items() if value[3] > time.time()}
                    if len(self.approvals) >= 100:
                        fail("too many pending write approvals", 429)
                    token = secrets.token_urlsafe(24)
                    self.approvals[token] = (str(target), digest, before, time.time() + 120, body["grant_id"])
                return {"approval": token, "expires_in": 120, "path": str(target), "sha256": digest,
                        "previous_sha256": before, "bytes": len(content.encode()), "content": content}
            if action != "commit-write":
                fail("unknown tool", 404)
            with self.lock:
                approval = self.approvals.pop(required(body, "approval"), None)
            if not approval or approval != (str(target), digest, before, approval[3], body["grant_id"]) or approval[3] < time.time():
                fail("write approval missing, expired, or file/content changed", 409)
            temporary = ".bridle-write-" + secrets.token_hex(12)
            try:
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    file.write(content)
                os.replace(temporary, target.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            finally:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
            return {"written": True, "sha256": digest}
        except OSError:
            fail("filesystem access denied or path changed", 403)
        finally:
            os.close(descriptor)

    def propose(self, body):
        from .domain import ChatMessage, ResponseFormat, SamplingParams
        from .structured import StructuredOutputUnavailable
        grant_id = required(body, "grant_id")
        grant = self.state["grants"].get(grant_id)
        if not grant:
            fail("missing or revoked filesystem permission", 403)
        task = required(body, "task", 8000)
        loaded = getattr(self.engine.runtime, "loaded", None)
        if loaded is None or getattr(loaded, "kind", "text") != "text":
            fail("Load a text model before asking it to propose a tool action", 409)
        schema = {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "read", "preview-write"]},
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["action", "path"], "additionalProperties": False}
        format = ResponseFormat("json_schema", schema, "filesystem_proposal")
        instruction = ("Propose exactly one filesystem action as JSON. You cannot execute tools. "
            "Allowed action: list, read, preview-write. path must be relative inside the granted folder; "
            "use '.' to list its root. Never use absolute paths or '..'. Include content only for preview-write. "
            "This grant is " + ("writable." if grant["write"] else "read-only; never propose preview-write.") +
            " Return only the JSON object; the human must approve it separately.")
        cancel = threading.Event()
        deadline = threading.Timer(30, cancel.set)
        deadline.daemon = True
        deadline.start()
        text = ""
        try:
            for use_format in (format, None):
                try:
                    prompt = self.engine.runtime.chat_prompt(loaded, (ChatMessage("system", instruction), ChatMessage("user", task)), response_format=use_format)
                    text = ""
                    for event in self.engine.runtime.generate(loaded, prompt,
                            SamplingParams.parse({"max_tokens": 512, "temperature": 0}), cancel,
                            response_format=use_format, raw_text=True):
                        text += getattr(event, "text", "")
                        if len(text.encode()) > 64 * 1024:
                            fail("model proposal exceeds 64 KiB; try a simpler task", 422)
                    break
                except StructuredOutputUnavailable:
                    if cancel.is_set():
                        break
            if cancel.is_set():
                fail("tool proposal timed out; try a smaller task or faster model", 504)
        except HTTPException:
            raise
        except Exception as exc:
            fail(f"model could not propose an action ({type(exc).__name__}); check the loaded model and retry", 503)
        finally:
            cancel.set()
            deadline.cancel()
        # Accept ordinary JSON or a single fenced JSON block; reject prose and
        # multiple objects rather than guessing which instruction should execute.
        raw = text.strip()
        if raw.startswith("```") and raw.endswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1])
        try:
            candidate = json.loads(raw)
        except ValueError:
            fail("model did not return valid proposal JSON; simplify the task and retry", 422)
        if not isinstance(candidate, dict) or set(candidate) - {"action", "path", "content"}:
            fail("model proposal must contain only action, path and optional content", 422)
        action = candidate.get("action")
        if action not in {"list", "read", "preview-write"}:
            fail("model proposed an unsupported tool action; nothing was executed", 422)
        proposal = {"action": action, "grant_id": grant_id, "path": required(candidate, "path")}
        self.grant_path(proposal, write=action == "preview-write")
        if action in {"read", "preview-write"} and Path(proposal["path"]).suffix.lower() not in TEXT_SUFFIXES:
            fail("model proposed an unsupported file extension; nothing was executed", 422)
        if action == "preview-write":
            if not isinstance(candidate.get("content"), str) or len(candidate["content"].encode()) > LIMIT:
                fail("model write proposal is missing bounded text content", 422)
            proposal["content"] = candidate["content"]
        elif "content" in candidate:
            fail("read/list proposals cannot contain write content", 422)
        return {"proposal": proposal, "requires_approval": True,
                "executed": False, "model": str(getattr(loaded, "repo", "unknown"))}

    def health(self, body):
        from .domain import RepoId, SamplingParams
        if not isinstance(body.get("generate", False), bool):
            fail("generate must be a boolean")
        try:
            repo = RepoId.parse(required(body, "repo"))
        except (ValueError, TypeError) as exc:
            fail(str(exc))
        local = self.engine.store.get(repo)
        path = Path(local.path) if local is not None else self.engine.store.path_for(repo)
        checks = []
        def check(name, ok, detail):
            checks.append({"name": name, "ok": ok, "detail": detail})
        local = self.engine.store.get(repo)
        check("download_manifest", local is not None, "Bridle manifest and model metadata")
        try:
            config = json.loads((path / "config.json").read_text())
            check("config", isinstance(config, dict), "config.json parses as an object")
        except (ValueError, OSError):
            check("config", False, "config.json is missing or invalid")
        weights = list(path.glob("*.safetensors"))
        def valid_weight(weight):
            try:
                with weight.open("rb") as handle:
                    length = int.from_bytes(handle.read(8), "little")
                    if not 2 <= length <= min(16 * LIMIT, weight.stat().st_size - 8):
                        return False
                    header = json.loads(handle.read(length))
                data_size = weight.stat().st_size - 8 - length
                tensors = [value for key, value in header.items() if key != "__metadata__"]
                return bool(tensors) and all(isinstance(tensor, dict)
                    and isinstance(tensor.get("data_offsets"), list) and len(tensor["data_offsets"]) == 2
                    and all(isinstance(offset, int) for offset in tensor["data_offsets"])
                    and 0 <= tensor["data_offsets"][0] <= tensor["data_offsets"][1] <= data_size for tensor in tensors)
            except (OSError, ValueError, TypeError, AttributeError):
                return False
        check("weights", bool(weights) and all(valid_weight(p) for p in weights), "safetensors headers and tensor bounds")
        index = path / "model.safetensors.index.json"
        if index.is_file():
            try:
                names = set(json.loads(index.read_text())["weight_map"].values())
                valid = bool(names) and all(isinstance(n, str) and Path(n).name == n and (path / n).is_file() for n in names)
            except (KeyError, TypeError, ValueError, OSError):
                valid = False
            check("weight_index", valid, "all indexed shards exist")
        if body.get("generate") and all(c["ok"] for c in checks):
            cancel = threading.Event()
            deadline = threading.Timer(45, cancel.set)
            deadline.daemon = True
            deadline.start()
            try:
                loaded = self.engine.runtime.load(repo)
                if cancel.is_set():
                    check("generation", False, "deadline reached during model load; no generation started")
                elif getattr(loaded, "kind", "text") != "text":
                    check("generation", False, "tiny generation is currently supported for text models only")
                else:
                    tokens = 0
                    for event in self.engine.runtime.generate(loaded, "Hello", SamplingParams.parse({"max_tokens": 8, "temperature": 0}), cancel, raw_text=True):
                        tokens = max(tokens, getattr(event, "completion_tokens", 0))
                    check("generation", not cancel.is_set() and tokens > 0,
                          f"generated {tokens} tokens" if not cancel.is_set() else "generation deadline reached")
            except Exception as exc:
                check("generation", False, f"load/generation failed: {type(exc).__name__}")
            finally:
                cancel.set()
                deadline.cancel()
        elif body.get("generate"):
            check("generation", False, "file checks failed; model was not loaded")
        return {"repo": str(repo), "ok": all(c["ok"] for c in checks), "checks": checks,
                "level": "generation" if body.get("generate") else "static",
                "limitation": "Explicit generation loads this model (may replace the active model), capped at 8 tokens with a 45-second cancellation deadline. Loading cannot be interrupted. Publisher checksums are not verified." if body.get("generate") else
                    "Does not load the model, verify publisher checksums, or prove generation works."}


def register_operations_routes(app, engine, vault=None):
    """Register behind the application's existing /control authentication middleware."""
    if getattr(app.state, "operations", None) is not None:
        return app.state.operations
    operations = Operations(engine, vault=vault)
    app.state.operations = operations
    prefix = "/control/operations"

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > LIMIT:
                fail("request exceeds 1 MiB", 413)
        try:
            result = json.loads(raw or b"{}")
        except ValueError:
            fail("invalid JSON")
        if not isinstance(result, dict):
            fail("JSON object required")
        return result

    @app.get(prefix)
    async def snapshot():
        return operations.snapshot()

    @app.post(prefix + "/requests/{identifier}/cancel")
    async def cancel(identifier: str):
        with operations.lock:
            event = operations.cancels.get(identifier)
            if event is None:
                fail("request is no longer cancellable", 404)
            event.set()
            operations.requests[identifier]["status"] = "cancelling"
        return {"cancellation_requested": True}

    @app.put(prefix + "/profile")
    async def profile(request: Request):
        name = required(await body(request), "name")
        if name not in PROFILES:
            fail("unknown profile")
        current, _ = engine.settings_store.update(PROFILES[name])
        operations.state["profile"] = name
        operations.save()
        return {"profile": name, "settings": current.to_json(), "thermal_control": False}

    @app.post(prefix + "/health")
    async def health(request: Request):
        data = await body(request)
        try:
            return await asyncio.wait_for(asyncio.to_thread(operations.health, data), timeout=50)
        except asyncio.TimeoutError:
            fail("model health check timed out; loading cannot be interrupted, and generation is cancelled at its deadline", 504)

    @app.post(prefix + "/clients")
    async def clients(request: Request):
        data = await body(request)
        name = required(data, "name", 80)
        kind = data.get("kind", "openai")
        if kind not in {"openai", "curl", "continue"}:
            fail("supported clients: openai, curl, continue")
        key, secret = engine.keys.create(name)
        base = engine.endpoint().rstrip("/") + "/v1"
        return {"key": key.public(), "secret": secret, "kind": kind,
                "config": {"base_url": base, "api_key": secret, "model": "<downloaded-model-id>"},
                "health": {"engine_running": True, "generation_tested": False},
                "instructions": "Copy this configuration into your client. The secret is shown once; no client files were modified."}

    @app.post(prefix + "/grants")
    async def grant(request: Request):
        data = await body(request)
        path = Path(required(data, "path")).expanduser().resolve()
        if not path.is_dir():
            fail("grant path must be an existing folder")
        if not isinstance(data.get("write", False), bool):
            fail("write must be a boolean")
        identifier = secrets.token_hex(8)
        value = {"id": identifier, "path": str(path), "write": data.get("write", False)}
        operations.state["grants"][identifier] = value
        operations.save()
        return value

    @app.delete(prefix + "/grants/{identifier}")
    async def revoke_grant(identifier: str):
        operations.state["grants"].pop(identifier, None)
        operations.save()
        return {"revoked": True}

    @app.post(prefix + "/tools/{action}")
    async def tool(action: str, request: Request):
        if action == "propose":
            data = await body(request)
            try:
                return await asyncio.wait_for(asyncio.to_thread(operations.propose, data), timeout=35)
            except asyncio.TimeoutError:
                fail("tool proposal timed out; generation cancellation has been requested", 504)
        if action not in {"list", "read", "preview-write", "commit-write"}:
            fail("unknown tool", 404)
        return operations.tool(action, await body(request))

    @app.post(prefix + "/mcp")
    async def mcp_add(request: Request):
        data = await body(request)
        identifier = secrets.token_hex(8)
        connection = {"id": identifier, "name": required(data, "name", 80),
                      "url": endpoint(required(data, "url")), "enabled": True}
        operations.state["mcp"][identifier] = connection
        operations.save()
        return connection

    @app.put(prefix + "/mcp/{identifier}")
    async def mcp_enable(identifier: str, request: Request):
        connection = operations.state["mcp"].get(identifier)
        if not connection:
            fail("MCP connection is missing", 404)
        data = await body(request)
        if not isinstance(data.get("enabled"), bool):
            fail("enabled must be a boolean")
        connection["enabled"] = data["enabled"]
        if not data["enabled"]:
            operations.mcp_sessions.pop(identifier, None)
        operations.save()
        return connection

    @app.delete(prefix + "/mcp/{identifier}")
    async def mcp_remove(identifier: str):
        operations.state["mcp"].pop(identifier, None)
        operations.mcp_sessions.pop(identifier, None)
        operations.save()
        return {"removed": True}

    @app.post(prefix + "/mcp/{identifier}/{action}")
    async def mcp_action(identifier: str, action: str, request: Request):
        connection = operations.state["mcp"].get(identifier)
        if not connection or not connection["enabled"]:
            fail("MCP connection is disabled or missing", 404)
        data = await body(request)
        methods = {"initialize": "initialize", "tools": "tools/list", "call": "tools/call"}
        if action not in methods:
            fail("unknown MCP action", 404)
        params = ({"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "Bridle", "version": __version__}} if action == "initialize" else {})
        if action == "call":
            params = {"name": required(data, "name", 200), "arguments": data.get("arguments", {})}
            if not isinstance(params["arguments"], dict):
                fail("arguments must be an object")
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2024-11-05"}
        if identifier in operations.mcp_sessions:
            headers["Mcp-Session-Id"] = operations.mcp_sessions[identifier]
        response_headers = {}
        result = await bounded_json(connection["url"], {"jsonrpc": "2.0", "id": secrets.token_hex(8),
                                    "method": methods[action], "params": params},
                                    headers=headers, response_headers=response_headers)
        if not isinstance(result, dict):
            fail("MCP response must be a JSON-RPC object", 502)
        if action == "initialize" and "error" not in result:
            session = response_headers.get("mcp-session-id")
            if session:
                operations.mcp_sessions[identifier] = session
                headers["Mcp-Session-Id"] = session
            await bounded_json(connection["url"], {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        return result

    @app.post(prefix + "/pairing")
    async def pairing():
        operations.codes = {key: expiration for key, expiration in operations.codes.items() if expiration > time.time()}
        if len(operations.codes) >= 20:
            fail("too many pending pairing codes", 429)
        code = secrets.token_urlsafe(12)
        operations.codes[hashlib.sha256(code.encode()).hexdigest()] = time.time() + 120
        return {"code": code, "expires_in": 120, "confirmation": "Host must confirm before copying the generated device key."}

    @app.post(prefix + "/pairing/confirm")
    async def pairing_confirm(request: Request):
        data = await body(request)
        name = required(data, "name", 80)
        digest = hashlib.sha256(required(data, "code").encode()).hexdigest()
        expiration = operations.codes.pop(digest, 0)
        if expiration < time.time():
            fail("pairing code expired or invalid", 403)
        allow_mesh = data.get("allow_mesh", False)
        if not isinstance(allow_mesh, bool):
            fail("allow_mesh must be a boolean")
        key, secret = engine.keys.create("Device: " + name, allow_mesh=allow_mesh)
        device = {"id": key.id, "name": name, "created_at": time.time(), "revoked": False, "allow_mesh": allow_mesh}
        operations.state["devices"][key.id] = device
        operations.save()
        return {"device": device, "secret": secret, "endpoint": engine.endpoint(),
                "tls_fingerprint": getattr(engine, "tls_fingerprint", ""),
                "instructions": "Transfer this one-time credential and verify the host certificate on the device."}

    @app.delete(prefix + "/devices/{identifier}")
    async def revoke_device(identifier: str):
        device = operations.state["devices"].get(identifier)
        if not device:
            fail("unknown device", 404)
        engine.keys.revoke(identifier)
        device["revoked"] = True
        operations.save()
        return {"revoked": True}

    @app.post(prefix + "/peers")
    async def peer_add(request: Request):
        data = await body(request)
        identifier = secrets.token_hex(8)
        pem = required(data, "ca_pem", 30000)
        try:
            # Restrict trust to the operator-approved certificate/CA for this peer.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=pem)
        except (ValueError, ssl.SSLError):
            fail("invalid peer CA certificate")
        peer = {"id": identifier, "name": required(data, "name", 80),
                "url": endpoint(required(data, "url"), https_only=True),
                "credential_id": identifier, "ca_pem": pem, "approved_at": time.time()}
        await asyncio.to_thread(operations.vault.put, identifier, required(data, "api_key"))
        operations.state["peers"][identifier] = peer
        operations.save()
        return {k: v for k, v in peer.items() if k not in {"api_key", "ca_pem"}}

    @app.delete(prefix + "/peers/{identifier}")
    async def peer_remove(identifier: str):
        peer = operations.state["peers"].get(identifier)
        if peer and peer.get("credential_id"):
            await asyncio.to_thread(operations.vault.delete, peer["credential_id"])
        operations.state["peers"].pop(identifier, None)
        operations.save()
        return {"removed": True}

    @app.post(prefix + "/route")
    async def route(request: Request):
        data = await body(request)
        peer = operations.state["peers"].get(required(data, "peer_id"))
        if not peer:
            fail("peer is not approved", 403)
        payload = data.get("request")
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            fail("request must be a complete chat request with messages")
        if payload.get("stream"):
            fail("peer routing currently supports non-streaming requests only")
        # Legacy private JSON credentials remain readable; no silent migration.
        secret = await asyncio.to_thread(operations.vault.get, peer["credential_id"]) if peer.get("credential_id") else peer.get("api_key")
        if not secret:
            fail("peer credential missing; register the peer again", 503)
        return await bounded_json(peer["url"] + "/v1/chat/completions", payload,
                                  headers={"Authorization": "Bearer " + secret}, ca_pem=peer["ca_pem"])
    return operations
