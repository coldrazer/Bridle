"""Remote-connection control routes and the workspace manager behind them.

All routes sit behind the existing loopback + control-token Guard in api.py.
Remote workspaces are metadata plus bounded, approved file operations; task
execution and inference stay local in this version.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import stat as statmod
import threading
import time
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, Request

from .domain import (APPROVAL_TTL, FILE_TIMEOUT, MAX_TRANSFER, MAX_TEXT_READ,
                     RemoteError, clean_text, identifier, new_id, validate_absolute_path,
                     validate_capabilities, validate_destination, validate_relative_path)
from .files import SFTPClient, access_for, page_entries, parent_and_name, resolve_within
from .ssh import SSHAdapter
from .store import RemoteStore

_READ, _WRITE_F, _CREAT, _TRUNC = 1, 2, 8, 16
BODY_LIMIT = 4 * 1024 * 1024


def _public_connection(conn: dict) -> dict:
    return {k: conn.get(k) for k in (
        "id", "name", "host_alias", "hostname", "username", "port", "auth_kind", "identity_file",
        "effective_destination", "approved_host_key_fingerprint", "host_key_type",
        "identity", "created_at", "last_success_at", "uses_proxy")}


def _public_workspace(ws: dict) -> dict:
    return {k: ws.get(k) for k in (
        "id", "connection_id", "canonical_root", "display_name", "verified_host_identity",
        "verified_user_identity", "capabilities", "readable", "writable", "verified_at")}


class RemoteManager:
    def __init__(self, data_dir: Path, store: RemoteStore | None = None, adapter: SSHAdapter | None = None):
        self.store = store or RemoteStore(Path(data_dir) / "remote.json")
        self.adapter = adapter or SSHAdapter(Path(data_dir))
        self._pending: dict[str, dict] = {}
        self._approvals: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._slots = threading.Semaphore(4)

    # ---- session plumbing ---------------------------------------------------
    def _session(self, conn: dict) -> SFTPClient:
        proc = self.adapter.open_sftp(conn)
        try:
            return SFTPClient(proc, timeout=FILE_TIMEOUT)
        except BaseException:
            try:
                proc.terminate()
            except OSError:
                pass
            self.adapter.release(conn["id"], proc)
            raise

    def _release(self, conn: dict, client: SFTPClient) -> None:
        proc = client.proc
        client.close()
        self.adapter.release(conn["id"], proc)

    def _require_trusted(self, conn: dict) -> dict:
        if not conn.get("approved_host_key_fingerprint"):
            raise RemoteError("verify and trust the host key before using this connection",
                              "trust_required", 409)
        if not conn.get("identity"):
            raise RemoteError("test this connection successfully before using it",
                              "test_required", 409)
        return conn

    # ---- connections ----------------------------------------------------------
    def create_connection(self, body: dict) -> dict:
        dest = validate_destination(body)
        if dest["auth_kind"] == "identity_file":
            identity_path = Path(dest["identity_file"]).expanduser()
            if not identity_path.is_file():
                raise RemoteError("the identity file does not exist on this Mac", "invalid_input")
            dest["identity_file"] = str(identity_path)
        now = time.time()
        conn = {"id": new_id(), "created_at": now, "last_success_at": None,
                "approved_host_key_fingerprint": "", "host_key_type": "",
                "effective_destination": "", "identity": None, "uses_proxy": False, **dest}
        return _public_connection(self.store.put_connection(conn))

    def test_connection(self, connection_id: str) -> dict:
        conn = self.store.get_connection(connection_id)
        scanned = self.adapter.scan_host_key(conn)
        approved = conn.get("approved_host_key_fingerprint") or ""
        conn["effective_destination"] = scanned["effective"]["destination"]
        conn["uses_proxy"] = scanned["effective"]["uses_proxy"]
        if approved and approved != scanned["fingerprint"]:
            self.store.put_connection(conn)
            raise RemoteError("the server's host key has changed since it was trusted; "
                              "the connection was blocked", "host_key_changed", 409)
        if not approved:
            attempt = secrets.token_urlsafe(24)
            with self._lock:
                self._pending = {k: v for k, v in self._pending.items() if v["expires"] > time.time()}
                self._pending[attempt] = {"connection_id": conn["id"], "fingerprint": scanned["fingerprint"],
                                          "key_line": scanned["key_line"], "key_type": scanned["key_type"],
                                          "expires": time.time() + APPROVAL_TTL}
            self.store.put_connection(conn)
            return {"status": "trust_required", "attempt_id": attempt,
                    "fingerprint": scanned["fingerprint"], "key_type": scanned["key_type"],
                    "effective_destination": conn["effective_destination"],
                    "uses_proxy": conn["uses_proxy"],
                    "message": "Compare this fingerprint with the server, then trust it to continue."}
        identity = self.adapter.probe(conn)
        conn.update(identity=identity, last_success_at=time.time(), host_key_type=scanned["key_type"])
        self.store.put_connection(conn)
        return {"status": "connected", "connection": _public_connection(conn), "identity": identity}

    def trust(self, body: dict, connection_id: str | None = None) -> dict:
        attempt_id = clean_text(body.get("attempt_id"), "attempt_id", 128)
        fingerprint = clean_text(body.get("fingerprint"), "fingerprint", 128)
        with self._lock:
            pending = self._pending.get(attempt_id)
            if pending is not None and connection_id is not None \
                    and pending["connection_id"] != connection_id:
                raise RemoteError("trust attempt does not belong to this connection",
                                  "trust_mismatch", 409)
            pending = self._pending.pop(attempt_id, None)
        if pending is None or pending["expires"] < time.time():
            raise RemoteError("this trust attempt has expired; test the connection again",
                              "trust_expired", 409)
        if not secrets.compare_digest(fingerprint, pending["fingerprint"]):
            raise RemoteError("the fingerprint does not match the one observed for this attempt",
                              "trust_mismatch", 409)
        conn = self.store.get_connection(pending["connection_id"])
        self.adapter.trust_key(pending["key_line"])
        conn["approved_host_key_fingerprint"] = pending["fingerprint"]
        conn["host_key_type"] = pending["key_type"]
        self.store.put_connection(conn)
        identity = self.adapter.probe(conn)
        conn.update(identity=identity, last_success_at=time.time())
        self.store.put_connection(conn)
        return {"status": "connected", "connection": _public_connection(conn), "identity": identity}

    def disconnect(self, connection_id: str) -> dict:
        self.store.get_connection(connection_id)
        stopped = self.adapter.disconnect(connection_id)
        return {"disconnected": True, "processes_stopped": stopped}

    def forget(self, connection_id: str) -> dict:
        self.adapter.disconnect(connection_id)
        self.store.delete_connection(connection_id)
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items()
                             if v["connection_id"] != connection_id}
            self._approvals = {k: v for k, v in self._approvals.items()
                               if v.get("connection_id") != connection_id}
        return {"forgotten": True}

    # ---- folder browsing and workspaces ---------------------------------------
    def browse(self, connection_id: str, raw_path: str | None, offset: int = 0,
               show_hidden: bool = False) -> dict:
        conn = self._require_trusted(self.store.get_connection(connection_id))
        identity = conn["identity"]
        path = validate_absolute_path(raw_path) if raw_path else identity["home"]
        with self._slots:
            client = self._session(conn)
            try:
                canonical = client.canonicalize(path)
                attrs = client.lstat(canonical)
                if not statmod.S_ISDIR(attrs.get("permissions", 0)):
                    raise RemoteError("the selected remote path is not a folder", "invalid_path")
                entries = client.list_names(canonical)
                readable, writable = access_for(attrs, identity)
            finally:
                self._release(conn, client)
        result = page_entries(entries, offset, show_hidden)
        parent = str(PurePosixPath(canonical).parent)
        return {"path": canonical, "parent": parent if parent != canonical else None,
                "readable": readable, "writable": writable, **result}

    def create_workspace(self, body: dict) -> dict:
        conn = self._require_trusted(self.store.get_connection(body.get("connection_id")))
        capabilities = validate_capabilities(body.get("capabilities"))
        requested = validate_absolute_path(body.get("path"))
        identity = conn["identity"]
        with self._slots:
            client = self._session(conn)
            try:
                canonical = client.canonicalize(requested)
                attrs = client.lstat(canonical)
                if not statmod.S_ISDIR(attrs.get("permissions", 0)):
                    raise RemoteError("the selected remote path is not a folder", "invalid_path")
                client.list_names(canonical)  # proves readability
                readable, writable = access_for(attrs, identity)
                if "write" in capabilities:
                    writable = self._probe_writable(client, canonical)
                    if not writable:
                        raise RemoteError("write access was requested but this folder is not writable "
                                          "by the connected user", "permission_denied", 403)
            finally:
                self._release(conn, client)
        ws = {"id": new_id(), "connection_id": conn["id"], "canonical_root": canonical,
              "display_name": PurePosixPath(canonical).name or canonical,
              "verified_host_identity": conn["approved_host_key_fingerprint"],
              "verified_user_identity": identity["user"],
              "capabilities": capabilities, "readable": bool(readable), "writable": bool(writable),
              "verified_at": time.time()}
        return _public_workspace(self.store.put_workspace(ws))

    @staticmethod
    def _probe_writable(client: SFTPClient, root: str) -> bool:
        probe_name = f".bridle-write-test-{secrets.token_hex(8)}"
        target = f"{root}/{probe_name}"
        try:
            handle = client.open(target, _WRITE_F | _CREAT | _TRUNC)
            client.close_handle(handle)
            client.remove(target)
            return True
        except RemoteError:
            try:
                client.remove(target)
            except RemoteError:
                pass
            return False

    # ---- task binding ------------------------------------------------------------
    def binding_for(self, ref: dict) -> dict:
        """Resolve a workspace reference into an immutable task snapshot."""
        if not isinstance(ref, dict):
            raise ValueError("workspace_binding must be an object")
        workspace_id = ref.get("workspace_id")
        ws = self.store.get_workspace(workspace_id)
        conn = self.store.get_connection(ws["connection_id"])
        if conn.get("approved_host_key_fingerprint") != ws["verified_host_identity"]:
            raise ValueError("the connection's trusted host key changed; review the connection before starting")
        return {"location": "remote", "connection_id": conn["id"], "workspace_id": ws["id"],
                "canonical_root": ws["canonical_root"], "host_identity": ws["verified_host_identity"],
                "user_identity": ws["verified_user_identity"],
                "approved_capabilities": list(ws["capabilities"]),
                "inference_location": "local", "binding_version": 1}

    # ---- bounded reads -----------------------------------------------------------
    def read_file(self, workspace_id: str, body: dict) -> dict:
        ws, conn = self._workspace_conn(workspace_id, need="read")
        relative = validate_relative_path(body.get("path"))
        with self._slots:
            client = self._session(conn)
            try:
                target = resolve_within(client, ws["canonical_root"], relative)
                data = self._read_bounded(client, target, MAX_TEXT_READ)
            finally:
                self._release(conn, client)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RemoteError("the file is not UTF-8 text", "invalid_content") from exc
        return {"path": relative, "content": content, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def _read_bounded(self, client: SFTPClient, target: str, limit: int) -> bytes:
        attrs = client.lstat(target)
        mode = attrs.get("permissions", 0)
        if not statmod.S_ISREG(mode):
            raise RemoteError("the selected remote path is not a regular file", "invalid_path")
        size = attrs.get("size", 0)
        if size > limit:
            raise RemoteError(f"the file is larger than the {limit} byte limit", "too_large", 413)
        handle = client.open(target, _READ)
        chunks, offset = [], 0
        try:
            while True:
                chunk = client.read_chunk(handle, offset, 32768)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                if offset > limit:
                    raise RemoteError(f"the file is larger than the {limit} byte limit", "too_large", 413)
        finally:
            client.close_handle(handle)
        return b"".join(chunks)

    def _workspace_conn(self, workspace_id: str, need: str | None = None) -> tuple[dict, dict]:
        ws = self.store.get_workspace(workspace_id)
        conn = self._require_trusted(self.store.get_connection(ws["connection_id"]))
        if conn.get("approved_host_key_fingerprint") != ws["verified_host_identity"]:
            raise RemoteError("the connection's trusted host key changed; review it before continuing",
                              "host_key_changed", 409)
        if need and need not in ws["capabilities"]:
            raise RemoteError(f"this workspace was not granted {need} access", "permission_denied", 403)
        return ws, conn

    # ---- approved writes ---------------------------------------------------------
    def _new_approval(self, record: dict) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._approvals = {k: v for k, v in self._approvals.items() if v["expires"] > time.time()}
            if len(self._approvals) >= 100:
                raise RemoteError("too many pending approvals", "too_many_approvals", 429)
            self._approvals[token] = {**record, "expires": time.time() + APPROVAL_TTL}
        return token

    def _take_approval(self, token_raw) -> dict:
        token = clean_text(token_raw, "approval", 128)
        with self._lock:
            record = self._approvals.pop(token, None)
        if record is None or record["expires"] < time.time():
            raise RemoteError("write approval is missing, expired, or already used", "approval_invalid", 409)
        return record

    def preview_write(self, workspace_id: str, body: dict) -> dict:
        ws, conn = self._workspace_conn(workspace_id, need="write")
        relative = validate_relative_path(body.get("path"))
        task_id = clean_text(body.get("task_id"), "task_id", 128) if body.get("task_id") else ""
        content = body.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TEXT_READ:
            raise RemoteError("content must be UTF-8 text no larger than 1 MiB", "invalid_input")
        data = content.encode("utf-8")
        with self._slots:
            client = self._session(conn)
            try:
                parent, name = parent_and_name(client, ws["canonical_root"], relative)
                target = f"{parent}/{name}"
                previous = self._existing_digest(client, target)
            finally:
                self._release(conn, client)
        token = self._new_approval({
            "kind": "write", "connection_id": conn["id"], "workspace_id": ws["id"],
            "host_identity": ws["verified_host_identity"], "root": ws["canonical_root"],
            "path": relative, "previous_sha256": previous,
            "sha256": hashlib.sha256(data).hexdigest(), "task_id": task_id})
        return {"approval": token, "expires_in": APPROVAL_TTL, "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(), "previous_sha256": previous,
                "bytes": len(data), "content": content}

    def _existing_digest(self, client: SFTPClient, target: str) -> str | None:
        try:
            attrs = client.lstat(target)
        except RemoteError as exc:
            if exc.code == "not_found":
                return None
            raise
        if statmod.S_ISLNK(attrs.get("permissions", 0)):
            raise RemoteError("refusing to write through a symbolic link", "invalid_path", 403)
        if not statmod.S_ISREG(attrs.get("permissions", 0)):
            raise RemoteError("the write target is not a regular file", "invalid_path")
        if attrs.get("size", 0) > MAX_TEXT_READ:
            raise RemoteError("the existing file is larger than 1 MiB", "too_large", 413)
        handle = client.open(target, _READ)
        hasher, offset = hashlib.sha256(), 0
        try:
            while chunk := client.read_chunk(handle, offset, 32768):
                hasher.update(chunk)
                offset += len(chunk)
        finally:
            client.close_handle(handle)
        return hasher.hexdigest()

    def apply_write(self, workspace_id: str, body: dict) -> dict:
        record = self._take_approval(body.get("approval"))
        ws, conn = self._workspace_conn(workspace_id, need="write")
        content = body.get("content")
        if not isinstance(content, str):
            raise RemoteError("content must be text", "invalid_input")
        data = content.encode("utf-8")
        expected = {"kind": "write", "workspace_id": ws["id"], "connection_id": conn["id"],
                    "host_identity": ws["verified_host_identity"], "root": ws["canonical_root"]}
        if any(record.get(k) != v for k, v in expected.items()) \
                or hashlib.sha256(data).hexdigest() != record["sha256"] \
                or len(data) > MAX_TEXT_READ:
            raise RemoteError("write approval does not match this workspace or content",
                              "approval_invalid", 409)
        with self._slots:
            client = self._session(conn)
            try:
                parent, name = parent_and_name(client, ws["canonical_root"], record["path"])
                target = f"{parent}/{name}"
                if self._existing_digest(client, target) != record["previous_sha256"]:
                    raise RemoteError("the file changed since the preview; review it again",
                                      "approval_stale", 409)
                temporary = f"{parent}/.bridle-write-{secrets.token_hex(12)}"
                handle = client.open(temporary, _WRITE_F | _CREAT | _TRUNC)
                try:
                    offset = 0
                    while offset < len(data):
                        piece = data[offset:offset + 32768]
                        client.write_chunk(handle, offset, piece)
                        offset += len(piece)
                finally:
                    client.close_handle(handle)
                try:
                    client.rename(temporary, target, overwrite=True)
                except RemoteError:
                    client.remove(target)
                    client.rename(temporary, target)
            finally:
                self._release(conn, client)
        return {"written": True, "path": record["path"], "sha256": record["sha256"], "bytes": len(data)}

    # ---- explicit single-file transfers --------------------------------------------
    def preview_transfer(self, workspace_id: str, body: dict) -> dict:
        ws, conn = self._workspace_conn(workspace_id)
        direction = body.get("direction")
        if direction not in ("download", "upload"):
            raise RemoteError("direction must be 'download' or 'upload'", "invalid_input")
        if direction == "upload" and "write" not in ws["capabilities"]:
            raise RemoteError("this workspace was not granted write access", "permission_denied", 403)
        relative = validate_relative_path(body.get("path"))
        local = Path(clean_text(body.get("local_path"), "local_path", 4096)).expanduser()
        task_id = clean_text(body.get("task_id"), "task_id", 128) if body.get("task_id") else ""
        with self._slots:
            client = self._session(conn)
            try:
                if direction == "download":
                    target = resolve_within(client, ws["canonical_root"], relative)
                    attrs = client.lstat(target)
                    if not statmod.S_ISREG(attrs.get("permissions", 0)):
                        raise RemoteError("the remote path is not a regular file", "invalid_path")
                    size = attrs.get("size", 0)
                    if size > MAX_TRANSFER:
                        raise RemoteError("the file exceeds the 64 MiB transfer limit", "too_large", 413)
                    digest = self._existing_digest(client, target) if size <= MAX_TEXT_READ else None
                    local_previous = hashlib.sha256(local.read_bytes()).hexdigest() \
                        if local.is_file() and local.stat().st_size <= MAX_TEXT_READ else None
                    local_exists = local.exists()
                else:
                    if not local.is_file():
                        raise RemoteError("the local file does not exist", "invalid_input")
                    size = local.stat().st_size
                    if size > MAX_TRANSFER:
                        raise RemoteError("the file exceeds the 64 MiB transfer limit", "too_large", 413)
                    digest = hashlib.sha256(local.read_bytes()).hexdigest() if size <= MAX_TEXT_READ else None
                    parent, name = parent_and_name(client, ws["canonical_root"], relative)
                    target = f"{parent}/{name}"
                    local_previous = self._existing_digest(client, target) \
                        if self._exists(client, target) else None
                    local_exists = self._exists(client, target)
            finally:
                self._release(conn, client)
        token = self._new_approval({
            "kind": "transfer", "direction": direction, "connection_id": conn["id"],
            "workspace_id": ws["id"], "host_identity": ws["verified_host_identity"],
            "root": ws["canonical_root"], "path": relative, "local_path": str(local),
            "size": size, "sha256": digest, "task_id": task_id})
        return {"approval": token, "expires_in": APPROVAL_TTL, "direction": direction,
                "path": relative, "local_path": str(local), "bytes": size, "sha256": digest,
                "destination_exists": local_exists,
                "destination_sha256": local_previous if direction == "download" else local_previous}

    @staticmethod
    def _exists(client: SFTPClient, target: str) -> bool:
        try:
            client.lstat(target)
            return True
        except RemoteError:
            return False

    def apply_transfer(self, workspace_id: str, body: dict) -> dict:
        record = self._take_approval(body.get("approval"))
        ws, conn = self._workspace_conn(workspace_id)
        if record.get("kind") != "transfer" or record["workspace_id"] != ws["id"] \
                or record["host_identity"] != ws["verified_host_identity"]:
            raise RemoteError("transfer approval does not match this workspace", "approval_invalid", 409)
        local = Path(record["local_path"])
        with self._slots:
            client = self._session(conn)
            try:
                if record["direction"] == "download":
                    target = resolve_within(client, ws["canonical_root"], record["path"])
                    data = self._read_bounded(client, target, MAX_TRANSFER)
                    local.parent.mkdir(parents=True, exist_ok=True)
                    temporary = local.with_name(local.name + f".bridle-{secrets.token_hex(6)}")
                    temporary.write_bytes(data)
                    temporary.replace(local)
                else:
                    if not local.is_file() or local.stat().st_size != record["size"]:
                        raise RemoteError("the local file changed since the preview; review it again",
                                          "approval_stale", 409)
                    parent, name = parent_and_name(client, ws["canonical_root"], record["path"])
                    target = f"{parent}/{name}"
                    temporary = f"{parent}/.bridle-write-{secrets.token_hex(12)}"
                    handle = client.open(temporary, _WRITE_F | _CREAT | _TRUNC)
                    try:
                        offset = 0
                        with local.open("rb") as fh:
                            while chunk := fh.read(32768):
                                client.write_chunk(handle, offset, chunk)
                                offset += len(chunk)
                    finally:
                        client.close_handle(handle)
                    try:
                        client.rename(temporary, target, overwrite=True)
                    except RemoteError:
                        if self._exists(client, target):
                            client.remove(target)
                        client.rename(temporary, target)
            finally:
                self._release(conn, client)
        return {"transferred": True, "direction": record["direction"], "path": record["path"],
                "local_path": record["local_path"], "bytes": record["size"]}


def register_remote_routes(app, engine):
    """Register /control/remote/* behind the app's existing control Guard."""
    if getattr(app.state, "remote", None) is not None:
        return app.state.remote
    manager = RemoteManager(engine.config.data_dir)
    app.state.remote = manager
    intelligence = getattr(app.state, "intelligence", None)
    if intelligence is not None:
        intelligence.workspace_resolver = manager.binding_for
    prefix = "/control/remote"

    def fail_from(exc: RemoteError):
        raise HTTPException(exc.status, exc.args[0] if exc.args else exc.code)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RemoteError as exc:
            fail_from(exc)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    async def body(request: Request) -> dict:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > BODY_LIMIT:
                raise HTTPException(413, "remote request exceeds 4 MiB")
            raw.extend(chunk)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "expected a JSON object") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "expected a JSON object")
        return value

    async def offload(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except RemoteError as exc:
            fail_from(exc)

    @app.get(prefix + "/connections")
    async def connections():
        return {"connections": [_public_connection(c) for c in manager.store.list_connections()],
                "store_load_error": manager.store.load_error}

    @app.post(prefix + "/connections")
    async def create_connection(request: Request):
        return call(manager.create_connection, await body(request))

    @app.post(prefix + "/connections/{connection_id}/test")
    async def test_connection(connection_id: str):
        return await offload(manager.test_connection, connection_id)

    @app.post(prefix + "/connections/{connection_id}/trust")
    async def trust(connection_id: str, request: Request):
        data = await body(request)
        data.setdefault("connection_id", connection_id)
        return await offload(lambda: _trust_checked(manager, connection_id, data))

    @app.post(prefix + "/connections/{connection_id}/disconnect")
    async def disconnect(connection_id: str):
        return call(manager.disconnect, connection_id)

    @app.delete(prefix + "/connections/{connection_id}")
    async def forget(connection_id: str):
        return call(manager.forget, connection_id)

    @app.get(prefix + "/connections/{connection_id}/folders")
    async def folders(connection_id: str, path: str | None = None, offset: int = 0,
                      show_hidden: bool = False):
        return await offload(lambda: manager.browse(connection_id, path, offset, show_hidden))

    @app.post(prefix + "/workspaces")
    async def create_workspace(request: Request):
        return await offload(manager.create_workspace, await body(request))

    @app.get(prefix + "/workspaces")
    async def workspaces(connection_id: str | None = None):
        try:
            return {"workspaces": [_public_workspace(w)
                                   for w in manager.store.list_workspaces(connection_id)]}
        except RemoteError as exc:
            fail_from(exc)

    @app.delete(prefix + "/workspaces/{workspace_id}")
    async def delete_workspace(workspace_id: str):
        return call(manager.store.delete_workspace, workspace_id)

    @app.post(prefix + "/workspaces/{workspace_id}/read")
    async def read_file(workspace_id: str, request: Request):
        return await offload(manager.read_file, workspace_id, await body(request))

    @app.post(prefix + "/workspaces/{workspace_id}/writes/preview")
    async def preview_write(workspace_id: str, request: Request):
        return await offload(manager.preview_write, workspace_id, await body(request))

    @app.post(prefix + "/workspaces/{workspace_id}/writes/apply")
    async def apply_write(workspace_id: str, request: Request):
        return await offload(manager.apply_write, workspace_id, await body(request))

    @app.post(prefix + "/workspaces/{workspace_id}/transfer/preview")
    async def preview_transfer(workspace_id: str, request: Request):
        return await offload(manager.preview_transfer, workspace_id, await body(request))

    @app.post(prefix + "/workspaces/{workspace_id}/transfer/apply")
    async def apply_transfer(workspace_id: str, request: Request):
        return await offload(manager.apply_transfer, workspace_id, await body(request))

    return manager


def _trust_checked(manager: RemoteManager, connection_id: str, data: dict) -> dict:
    identifier(connection_id, "connection")
    return manager.trust(data, connection_id=connection_id)
