"""Mesh mode at runtime: run this Mac's rank, coordinate the other Macs, serve the pooled model.

* MeshSession runs this Mac's rank (`python -m bridle_engine.mesh_worker`) and tracks it.
* MeshCoordinator (on the Mac the user drives, rank 0) asks each approved peer for its node
  report, plans the mesh (mesh.plan), downloads the model where it is missing, starts every
  member's rank over the peer's HTTPS endpoint, starts rank 0 here, waits for the sharded load,
  and then serves the pooled model through this engine's normal /v1 API.
* Member API (/mesh/*): what a coordinator may ask of this Mac. It needs an API key with the
  mesh grant (KeyStore allow_mesh) and HTTPS from other machines; see api.Guard.

The control channel is ordinary authenticated HTTPS; tensors never travel through it. MLX moves
them over RDMA (JACCL) or, if explicitly allowed, a TCP ring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .domain import ParseError, RepoId
from .mesh import RECOVERY_STEPS, STRATEGIES, Issue, Plan, model_support, node_report, plan

log = logging.getLogger("bridle.mesh")
ACTIVE = ("joining", "loading", "ready")
RESPONSE_LIMIT = 4 * 1024 * 1024
# Request fields forwarded to the rank-0 server. Anything else (adapters, draft models, other
# model paths) is dropped: mlx_lm.server would try to load them, which a mesh cannot do.
FORWARDED = {"messages", "prompt", "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k",
             "min_p", "stop", "stream", "stream_options", "seed", "tools", "tool_choice", "logprobs",
             "top_logprobs", "presence_penalty", "frequency_penalty", "repetition_penalty",
             "repetition_context_size", "logit_bias", "response_format", "chat_template_kwargs", "n"}


class MeshError(Exception):
    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status, self.extra = status, extra or {}


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- this Mac's rank -------------------------------------------------------------------------------
class MeshSession:
    def __init__(self, engine, python: str | None = None):
        self.engine = engine
        self.python = python or sys.executable
        self.state = "idle"            # idle | joining | loading | ready | failed | stopped
        self.error = ""
        self.config: dict = {}
        self.started_at: float | None = None
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=300)
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def rank(self) -> int | None:
        return self.config.get("rank") if self.config else None

    @property
    def port(self) -> int | None:
        return self.config.get("port") if self.config else None

    def serves(self, model: Any) -> bool:
        return (self.state == "ready" and self.rank == 0 and isinstance(model, str)
                and model in (self.config.get("model"), "mesh"))

    def status(self) -> dict:
        with self._lock:
            return self.status_unlocked()

    def start(self, cfg: dict) -> dict:
        required = ("rank", "world", "backend", "strategy", "model")
        if not isinstance(cfg, dict) or any(k not in cfg for k in required):
            raise MeshError("mesh config needs " + ", ".join(required))
        if cfg["backend"] not in ("jaccl", "jaccl-ring", "ring") or cfg["strategy"] not in ("tensor", "pipeline"):
            raise MeshError("unknown mesh backend or strategy")
        if not (isinstance(cfg["rank"], int) and isinstance(cfg["world"], int) and 0 <= cfg["rank"] < cfg["world"] <= 64):
            raise MeshError("invalid rank/world")
        if cfg["backend"].startswith("jaccl") and not (isinstance(cfg.get("devices"), list) and cfg.get("coordinator")):
            raise MeshError("a JACCL mesh needs the device matrix and coordinator address")
        if cfg["backend"] == "ring" and not isinstance(cfg.get("ring_hosts"), list):
            raise MeshError("a TCP ring needs the host list")
        with self._lock:
            if self.state in ACTIVE:
                raise MeshError(f"this Mac is already in a mesh ({self.state})", 409)
        repo = RepoId.parse(cfg["model"])
        local = self.engine.store.get(repo)
        if local is None:
            raise MeshError(f"{repo} is not downloaded on this Mac", 409)
        if cfg.get("revision") and local.revision and cfg["revision"] != local.revision:
            raise MeshError(f"this Mac has a different revision of {repo}", 409)
        rt = self.engine.runtime
        if rt is not None and rt.loaded is not None:
            raise MeshError(f"unload {rt.loaded.repo} on this Mac first", 409)
        full = {**cfg, "model": str(repo), "model_path": local.path}
        launch = Path(self.engine.config.data_dir) / "mesh" / f"launch-{cfg['rank']}.json"
        _write_private(launch, json.dumps(full).encode())
        env = os.environ.copy()
        env["BRIDLE_MESH_CONFIG"] = str(launch)
        env["BRIDLE_PARENT_PID"] = str(os.getpid())
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # never write .pyc into a signed app bundle
        try:
            proc = subprocess.Popen([self.python, "-m", "bridle_engine.mesh_worker"], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1, env=env, cwd=package_root)
        except OSError as exc:
            launch.unlink(missing_ok=True)
            raise MeshError(f"could not start the mesh worker: {exc}", 500) from exc
        with self._lock:
            self.proc, self.config, self.state, self.error = proc, full, "joining", ""
            self.started_at = time.time()
            self.lines.clear()
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        log.info("mesh rank %d/%d started (%s, %s, %s)", cfg["rank"], cfg["world"], cfg["backend"], cfg["strategy"], repo)
        return self.status()

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            with self._lock:
                self.lines.append(line)
                if proc is not self.proc:
                    continue
                if line.startswith("BRIDLE_MESH_JOINED") and self.state == "joining":
                    self.state = "loading"
                elif line.startswith("BRIDLE_MESH_ERROR"):
                    self.error = line[len("BRIDLE_MESH_ERROR"):].strip()
        code = proc.wait()
        with self._lock:
            if proc is not self.proc:
                return
            if self.state == "stopping":
                self.state = "stopped"
            else:
                self.state = "failed"
                if not self.error:
                    tail = [ln for ln in self.lines if ln.strip()][-3:]
                    self.error = f"the mesh worker exited (code {code})" + (": " + " | ".join(tail) if tail else "")
            self.proc = None
        log.info("mesh rank exited with %s (%s)", code, self.state)

    def mark_ready(self) -> None:
        with self._lock:
            if self.state in ("joining", "loading"):
                self.state = "ready"

    def fail(self, message: str) -> None:
        with self._lock:
            self.error = message
        self.stop()
        with self._lock:
            self.state, self.error = "failed", message

    def stop(self, timeout: float = 5.0) -> dict:
        with self._lock:
            proc = self.proc
            if proc is None:
                if self.state in ACTIVE:
                    self.state = "stopped"
                return self.status_unlocked()
            self.state = "stopping"
        proc.terminate()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        deadline = time.time() + 2
        while time.time() < deadline:
            with self._lock:
                if self.proc is None:
                    break
            time.sleep(0.05)
        with self._lock:
            self.proc = None
            if self.state == "stopping":
                self.state = "stopped"
        return self.status()

    def status_unlocked(self) -> dict:
        return {"state": self.state, "error": self.error, "rank": self.rank, "world": self.config.get("world"),
                "backend": self.config.get("backend"), "strategy": self.config.get("strategy"),
                "model": self.config.get("model"), "started_at": self.started_at, "log": list(self.lines)[-40:]}


# ---- talking to approved peers ---------------------------------------------------------------------
async def peer_call(peer: dict, secret: str, method: str, path: str, body: dict | None = None,
                    timeout: float = 20.0) -> dict:
    """One request to an approved peer: its own pinned certificate, no proxies, no redirects."""
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=peer["ca_pem"])
    except (KeyError, ValueError, ssl.SSLError) as exc:
        raise MeshError(f"{peer.get('name', 'peer')}: invalid trusted certificate", 502) from exc
    url = peer["url"].rstrip("/") + path
    from . import egress
    egress.record(peer["url"], f"mesh peer {method} {path}")
    try:
        async with httpx.AsyncClient(verify=context, timeout=timeout, trust_env=False, follow_redirects=False) as client:
            async with client.stream(method, url, json=body, headers={"Authorization": "Bearer " + secret}) as resp:
                raw = bytearray()
                async for chunk in resp.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > RESPONSE_LIMIT:
                        raise MeshError(f"{peer.get('name')}: response too large", 502)
                try:
                    data = json.loads(raw) if raw else {}
                except ValueError:
                    data = {}
                if resp.status_code >= 300:
                    detail = data.get("detail") or (data.get("error") or {}).get("message") if isinstance(data, dict) else ""
                    raise MeshError(f"{peer.get('name')}: HTTP {resp.status_code}" + (f" ({detail})" if detail else ""),
                                    502 if resp.status_code >= 500 else resp.status_code)
                return data if isinstance(data, dict) else {"data": data}
    except httpx.HTTPError as exc:
        raise MeshError(f"{peer.get('name')}: unreachable ({type(exc).__name__})", 502) from exc


def _cert_fingerprint(pem: str) -> str:
    from cryptography import x509
    from .tls import _fingerprint
    return _fingerprint(x509.load_pem_x509_certificate(pem.encode()))


def fetch_certificate(url: str, timeout: float = 6.0) -> dict:
    """The certificate a Bridle server presents, for the user to compare with the fingerprint that
    Mac shows when it pairs. Trust is only granted after that comparison (see connect)."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise MeshError("enter the other Mac's HTTPS address, e.g. https://studio.local:49814")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise MeshError("the address has an invalid port") from exc
    from . import egress
    egress.record(url, "mesh certificate fetch")
    try:
        pem = ssl.get_server_certificate((parts.hostname, port), timeout=timeout)
    except (OSError, ssl.SSLError) as exc:
        raise MeshError(f"could not reach {parts.hostname}:{port} over HTTPS ({type(exc).__name__}). "
                        "On that Mac, turn on Share on network and HTTPS in Bridle's settings.", 502) from exc
    return {"pem": pem, "fingerprint": _cert_fingerprint(pem), "host": parts.hostname, "port": port}


def _node_summary(report: dict) -> dict:
    return {k: report.get(k) for k in ("name", "chip", "model_name", "memory_total_bytes", "macos", "macos_ok",
                                       "thunderbolt_generation", "thunderbolt_ports", "rdma", "rdma_devices",
                                       "bridge", "mlx_version", "engine_version")}


class MeshCoordinator:
    """Runs on the Mac the user drives; it is always rank 0."""

    def __init__(self, engine, operations, call=peer_call):
        self.engine, self.operations, self.call = engine, operations, call
        self.state = "idle"           # idle | starting | ready | failed | stopping
        self.error = ""
        self.plan: Plan | None = None
        self.members: list[str] = []  # peer ids in rank order (rank 1..n-1)
        self.task: asyncio.Task | None = None

    # peers -----------------------------------------------------------------------------------------
    def peers(self) -> list[dict]:
        return [{"id": p["id"], "name": p.get("name"), "url": p.get("url"), "mesh": bool(p.get("mesh")),
                 "fingerprint": p.get("fingerprint", "")}
                for p in self.operations.state.get("peers", {}).values()]

    async def _peer(self, peer_id: str) -> tuple[dict, str]:
        peer = self.operations.state.get("peers", {}).get(peer_id)
        if not peer:
            raise MeshError(f"unknown or unapproved peer {peer_id}", 404)
        cred = peer.get("credential_id")
        try:
            secret = await asyncio.to_thread(self.operations.vault.get, cred) if cred else peer.get("api_key")
        except HTTPException as exc:
            raise MeshError(f"{peer.get('name')}: {exc.detail}", 503) from exc
        if not secret:
            raise MeshError(f"{peer.get('name')}: credential missing; approve the peer again", 503)
        return peer, secret

    async def ask(self, peer_id: str, method: str, path: str, body: dict | None = None, timeout: float = 20.0) -> dict:
        peer, secret = await self._peer(peer_id)
        return await self.call(peer, secret, method, path, body, timeout)

    async def connect(self, name: str, url: str, api_key: str, pem: str, fingerprint: str) -> dict:
        """Approve another Mac for pooling: check its certificate against the fingerprint the user
        confirmed, prove the key carries the mesh grant by asking for its node report, then save it."""
        from .operations import endpoint
        url = endpoint(url, https_only=True)
        try:
            actual = _cert_fingerprint(pem)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a bad certificate
            raise MeshError("that is not a valid certificate") from exc
        if actual.replace(":", "").upper() != fingerprint.replace(":", "").replace(" ", "").upper():
            raise MeshError("the certificate fingerprint does not match the one you confirmed; do not connect", 400)
        peer = {"id": "", "name": name, "url": url, "ca_pem": pem}
        try:
            report = await self.call(peer, api_key, "GET", "/mesh/node", None, 15.0)
        except MeshError as exc:
            if exc.status == 403:
                raise MeshError(f"{name} accepted the key but it does not allow mesh. On {name}, pair again with "
                                "Allow mesh turned on.", 403) from exc
            if exc.status == 401:
                raise MeshError(f"{name} did not accept that key. Copy the key {name} showed when you approved "
                                "this Mac.", 401) from exc
            raise
        existing = next((p for p in self.operations.state.get("peers", {}).values() if p.get("url") == url), None)
        identifier = existing["id"] if existing else os.urandom(8).hex()
        await asyncio.to_thread(self.operations.vault.put, identifier, api_key)
        saved = {"id": identifier, "name": name, "url": url, "credential_id": identifier, "ca_pem": pem,
                 "approved_at": time.time(), "mesh": True, "fingerprint": actual}
        self.operations.state.setdefault("peers", {})[identifier] = saved
        await asyncio.to_thread(self.operations.save)
        return {"peer": {"id": identifier, "name": name, "url": url, "fingerprint": actual},
                "node": _node_summary(report)}

    # planning --------------------------------------------------------------------------------------
    async def reports(self, peer_ids: list[str]) -> tuple[list[dict], list[str], list[Issue]]:
        local = await asyncio.to_thread(node_report, self.engine)
        reports, ids, problems = [local], [""], []
        results = await asyncio.gather(*(self.ask(p, "GET", "/mesh/node") for p in peer_ids), return_exceptions=True)
        for pid, result in zip(peer_ids, results):
            if isinstance(result, Exception):
                problems.append(Issue(pid, "peer_unreachable", str(result),
                                      "Check that Bridle runs there with HTTPS on, and that its key allows mesh."))
            else:
                reports.append(result)
                ids.append(pid)
        return reports, ids, problems

    async def make_plan(self, model: str, peer_ids: list[str], strategy: str = "auto",
                        allow_tcp: bool = False) -> tuple[Plan, list[str], list[dict]]:
        if strategy not in STRATEGIES:
            raise MeshError(f"strategy must be one of {', '.join(STRATEGIES)}")
        repo = str(RepoId.parse(model))
        if len(set(peer_ids)) != len(peer_ids):
            raise MeshError("each peer can join once")
        known = self.operations.state.get("peers", {})
        unknown = [p for p in peer_ids if p not in known]
        if unknown:
            raise MeshError(f"unknown or unapproved peer {unknown[0]}", 404)
        reports, ids, problems = await self.reports(peer_ids)
        local = self.engine.store.get(RepoId.parse(repo))
        support = model_support(local.model_type) if local else {}
        result = plan(reports, repo, strategy, allow_tcp, support)
        result.blockers = problems + result.blockers
        result.ok = result.ok and not problems
        for row in result.nodes:
            row["peer_id"] = ids[row["index"]]
        self.plan = result
        return result, ids, reports

    async def prepare(self, model: str, peer_ids: list[str]) -> dict:
        """Start downloads of the model on every Mac that lacks it (this one included)."""
        repo = RepoId.parse(model)
        started = []
        if self.engine.store.get(repo) is None:
            self.engine.downloads.start(repo)
            started.append("this Mac")
        for pid in peer_ids:
            result = await self.ask(pid, "POST", "/mesh/download", {"model": str(repo)})
            if not result.get("present"):
                started.append(result.get("node") or pid)
        return {"downloading_on": started}

    # running ---------------------------------------------------------------------------------------
    async def start(self, model: str, peer_ids: list[str], strategy: str = "auto", allow_tcp: bool = False) -> dict:
        if self.state in ("starting", "ready", "stopping"):
            raise MeshError(f"a mesh is already {self.state}; stop it first", 409)
        result, ids, reports = await self.make_plan(model, peer_ids, strategy, allow_tcp)
        if not result.ok:
            raise MeshError("the mesh cannot start yet", 409, {"plan": result.to_json()})
        order = [row["peer_id"] for row in result.nodes]  # "" is this Mac
        if order[0] != "":
            raise MeshError("internal: the coordinator must be rank 0", 500)
        revision = next((m.get("revision") for m in reports[0].get("models", []) if m.get("repo") == result.model), "")
        base = {"world": len(order), "backend": result.backend, "strategy": result.strategy,
                "model": result.model, "revision": revision, "coordinator": result.coordinator,
                "devices": result.devices, "ring_hosts": result.ring_hosts}
        self.members, self.state, self.error = order[1:], "starting", ""
        self.task = asyncio.create_task(self._launch(base, order))
        return self.status()

    async def _launch(self, base: dict, order: list[str]) -> None:
        session = self.engine.mesh
        port = free_port()
        try:
            for rank, pid in enumerate(order):
                if pid:
                    await self.ask(pid, "POST", "/mesh/join", {**base, "rank": rank}, timeout=60)
            await asyncio.to_thread(session.start, {**base, "rank": 0, "port": port})
            await self._wait_ready(port)
            session.mark_ready()
            self.state = "ready"
            log.info("mesh ready: %s on %d Macs (%s, %s)", base["model"], base["world"], base["backend"], base["strategy"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure tears the mesh down
            self.error = str(exc)
            self.state = "failed"
            log.warning("mesh failed to start: %s", exc)
            await self._teardown(failed=str(exc))

    async def _wait_ready(self, port: int, load_timeout: float = 1800.0) -> None:
        """Wait for rank 0's server, then send one tiny request: it completes only after every rank
        has finished loading its shard (the load is collective)."""
        session = self.engine.mesh
        deadline = time.time() + load_timeout
        url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            while True:
                await self._check_members()
                if session.state == "failed":
                    raise MeshError(session.error or "this Mac's rank failed")
                try:
                    if (await client.get(url + "/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.time() > deadline:
                    raise MeshError("timed out waiting for the mesh to come up")
                await asyncio.sleep(1)
        warm = asyncio.create_task(self._warm_up(url, deadline - time.time()))
        while not warm.done():
            await asyncio.sleep(2)
            try:
                await self._check_members()
                if session.state == "failed":
                    raise MeshError(session.error or "this Mac's rank failed")
            except Exception:
                warm.cancel()
                raise
        warm.result()

    async def _warm_up(self, url: str, timeout: float) -> None:
        body = {"model": "default_model", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}
        async with httpx.AsyncClient(timeout=max(timeout, 30), trust_env=False) as client:
            resp = await client.post(url + "/v1/chat/completions", json=body)
            if resp.status_code != 200:
                raise MeshError(f"the pooled model did not answer (HTTP {resp.status_code}): {resp.text[:300]}")

    async def _check_members(self) -> None:
        for pid in self.members:
            status = await self.ask(pid, "GET", "/mesh/status")
            if status.get("state") == "failed":
                raise MeshError(f"{pid}: {status.get('error') or 'its rank failed'}")

    async def _teardown(self, failed: str = "") -> None:
        for pid in self.members:
            try:
                await self.ask(pid, "POST", "/mesh/leave", {})
            except Exception as exc:  # noqa: BLE001 - keep tearing down the rest
                log.warning("could not stop mesh member %s: %s", pid, exc)
        if failed:
            await asyncio.to_thread(self.engine.mesh.fail, failed)
        else:
            await asyncio.to_thread(self.engine.mesh.stop)

    async def stop(self) -> dict:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.state = "stopping"
        await self._teardown()
        self.state, self.error = "idle", ""
        return self.status()

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "members": self.members,
                "plan": self.plan.to_json() if self.plan else None, "rank0": self.engine.mesh.status()}


# ---- serving the pooled model ----------------------------------------------------------------------
async def proxy(engine, data: dict, path: str) -> Any:
    """Forward an OpenAI request for the pooled model to rank 0's server on loopback."""
    session = engine.mesh
    model = data.get("model")
    body = {k: v for k, v in data.items() if k in FORWARDED}
    body["model"] = "default_model"
    url = f"http://127.0.0.1:{session.port}{path}"
    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), trust_env=False)
    if body.get("stream"):
        request = client.build_request("POST", url, json=body)
        try:
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            return JSONResponse(status_code=502, content={"error": {"message": f"mesh unavailable: {exc}", "type": "server_error"}})
        if resp.status_code != 200:
            text = await resp.aread()
            await resp.aclose()
            await client.aclose()
            return JSONResponse(status_code=resp.status_code, content={"error": {"message": text.decode(errors="replace")[:500], "type": "server_error"}})

        async def relay():
            try:
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            chunk["model"] = model
                            line = "data: " + json.dumps(chunk, ensure_ascii=False)
                        except ValueError:
                            pass
                    yield line + "\n"
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(relay(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache", "x-accel-buffering": "no"})
    try:
        resp = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content={"error": {"message": f"mesh unavailable: {exc}", "type": "server_error"}})
    finally:
        await client.aclose()
    try:
        out = resp.json()
    except ValueError:
        return JSONResponse(status_code=502, content={"error": {"message": "the mesh returned invalid JSON", "type": "server_error"}})
    if isinstance(out, dict):
        out["model"] = model
        out.setdefault("bridle", {})["mesh"] = {"world": session.config.get("world"), "backend": session.config.get("backend"),
                                                "strategy": session.config.get("strategy")}
    return JSONResponse(status_code=resp.status_code, content=out)


# ---- routes -------------------------------------------------------------------------------------------
def register_mesh_routes(app, engine, operations, call=peer_call) -> MeshCoordinator:
    coordinator = MeshCoordinator(engine, operations, call)
    app.state.mesh = coordinator

    async def body_of(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > 256 * 1024:
            raise HTTPException(413, "request too large")
        try:
            data = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise ParseError("request body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ParseError("request body must be a JSON object")
        return data

    def mesh_error(exc: MeshError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": {"message": str(exc), "type": "mesh_error", **exc.extra}})

    def peer_list(data: dict) -> list[str]:
        peers = data.get("peers", [])
        if not isinstance(peers, list) or not all(isinstance(p, str) and p for p in peers) or len(peers) > 15:
            raise ParseError("peers must be a list of approved peer ids", "peers")
        return peers

    def options(data: dict) -> tuple[str, list[str], str, bool]:
        model = data.get("model")
        if not isinstance(model, str) or not model:
            raise ParseError("model is required", "model")
        strategy = data.get("strategy", "auto")
        allow_tcp = data.get("allow_tcp", False)
        if not isinstance(allow_tcp, bool):
            raise ParseError("allow_tcp must be a boolean", "allow_tcp")
        return model, peer_list(data), strategy, allow_tcp

    # coordinator (this Mac, loopback + control token) ---------------------------------------------
    @app.get("/control/mesh")
    async def mesh_overview():
        local = await asyncio.to_thread(node_report, engine)
        return {"node": local, "peers": coordinator.peers(), "coordinator": coordinator.status(),
                "setup": {"recovery_steps": RECOVERY_STEPS, "command": "rdma_ctl enable",
                          "verify": ["ibv_devices", "ibv_devinfo"], "recommended_size": "2–4 Macs",
                          "requirements": ["macOS 26.2 or later on every Mac",
                                           "Thunderbolt 5 on every Mac (M4 Pro/Max or later Pro/Max, M3 Ultra or later)",
                                           "Direct Thunderbolt 5 cables between the Macs (every pair for a full mesh; "
                                           "a ring for 5 or more Macs; two cables between a pair add bandwidth)",
                                           "RDMA enabled from macOS Recovery on every Mac",
                                           "Thunderbolt Bridge inactive (System Settings > Network)",
                                           "Enough ports: links are point-to-point (no Thunderbolt 5 switches), so a full "
                                           "mesh of N Macs needs N−1 ports on each (a 6-port M5 Ultra Mac Studio tops out "
                                           "at 7); beyond that, cable a ring",
                                           "The same model downloaded on every Mac"]}}

    @app.get("/control/mesh/peers")
    async def mesh_peers():
        return {"peers": coordinator.peers(), "state": coordinator.state}

    @app.post("/control/mesh/certificate")
    async def mesh_certificate(request: Request):
        url = (await body_of(request)).get("url")
        if not isinstance(url, str) or len(url) > 300:
            raise ParseError("url is required", "url")
        try:
            return await asyncio.to_thread(fetch_certificate, url)
        except MeshError as exc:
            return mesh_error(exc)

    @app.post("/control/mesh/connect")
    async def mesh_connect(request: Request):
        data = await body_of(request)
        fields = {}
        for key, limit in (("name", 80), ("url", 300), ("api_key", 400), ("ca_pem", 30000), ("fingerprint", 200)):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ParseError(f"{key} is required", key)
            fields[key] = value.strip()
        try:
            return await coordinator.connect(fields["name"], fields["url"], fields["api_key"], fields["ca_pem"],
                                             fields["fingerprint"])
        except MeshError as exc:
            return mesh_error(exc)
        except HTTPException as exc:
            return mesh_error(MeshError(str(exc.detail), exc.status_code))

    @app.post("/control/mesh/plan")
    async def mesh_plan(request: Request):
        try:
            result, _, _ = await coordinator.make_plan(*options(await body_of(request)))
        except MeshError as exc:
            return mesh_error(exc)
        except ValueError as exc:
            raise ParseError(str(exc)) from exc
        return result.to_json()

    @app.post("/control/mesh/prepare")
    async def mesh_prepare(request: Request):
        model, peers, _, _ = options(await body_of(request))
        try:
            return await coordinator.prepare(model, peers)
        except MeshError as exc:
            return mesh_error(exc)

    @app.post("/control/mesh/start")
    async def mesh_start(request: Request):
        try:
            return JSONResponse(status_code=202, content=await coordinator.start(*options(await body_of(request))))
        except MeshError as exc:
            return mesh_error(exc)
        except ValueError as exc:
            raise ParseError(str(exc)) from exc

    @app.post("/control/mesh/stop")
    async def mesh_stop():
        return await coordinator.stop()

    # member (another Mac's coordinator; key with the mesh grant, HTTPS) ----------------------------
    @app.get("/mesh/node")
    async def member_node():
        return await asyncio.to_thread(node_report, engine)

    @app.post("/mesh/download")
    async def member_download(request: Request):
        repo = RepoId.parse((await body_of(request)).get("model"))
        if engine.store.get(repo) is not None:
            return {"present": True, "node": engine.machine.hostname}
        job = engine.downloads.start(repo)
        return {"present": False, "node": engine.machine.hostname, "job": job.to_json(engine.store)}

    @app.post("/mesh/join")
    async def member_join(request: Request):
        cfg = await body_of(request)
        if not isinstance(cfg.get("rank"), int) or cfg["rank"] < 1:
            return mesh_error(MeshError("members join as rank 1 or above; rank 0 is the coordinator"))
        try:
            return await asyncio.to_thread(engine.mesh.start, cfg)
        except MeshError as exc:
            return mesh_error(exc)
        except ParseError:
            raise

    @app.post("/mesh/leave")
    async def member_leave():
        return await asyncio.to_thread(engine.mesh.stop)

    @app.get("/mesh/status")
    async def member_status():
        return engine.mesh.status()

    return coordinator
