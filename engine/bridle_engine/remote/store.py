"""Versioned, secret-free persistence for remote connections and workspaces."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .domain import RemoteError, identifier

SCHEMA_VERSION = 1


class RemoteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.load_error = ""
        self._data = {"version": SCHEMA_VERSION, "connections": [], "workspaces": []}
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text())
                if not isinstance(saved, dict) or saved.get("version") != SCHEMA_VERSION \
                        or not isinstance(saved.get("connections"), list) \
                        or not isinstance(saved.get("workspaces"), list):
                    raise ValueError("unsupported remote store schema")
                self._data = saved
            except (OSError, ValueError) as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"
                backup = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
                try:
                    self.path.rename(backup)
                except OSError:
                    pass

    # ---- persistence -----------------------------------------------------
    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".remote-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self._data, indent=2))
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    # ---- connections -----------------------------------------------------
    def list_connections(self) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._data["connections"]]

    def get_connection(self, connection_id: str) -> dict:
        identifier(connection_id, "connection")
        with self._lock:
            for conn in self._data["connections"]:
                if conn["id"] == connection_id:
                    return dict(conn)
        raise RemoteError("connection not found", "not_found", 404)

    def put_connection(self, record: dict) -> dict:
        with self._lock:
            rows = self._data["connections"]
            for index, conn in enumerate(rows):
                if conn["id"] == record["id"]:
                    rows[index] = dict(record)
                    break
            else:
                rows.append(dict(record))
            self._save_locked()
        return dict(record)

    def delete_connection(self, connection_id: str) -> None:
        identifier(connection_id, "connection")
        with self._lock:
            before = len(self._data["connections"])
            self._data["connections"] = [c for c in self._data["connections"] if c["id"] != connection_id]
            self._data["workspaces"] = [w for w in self._data["workspaces"]
                                         if w["connection_id"] != connection_id]
            if len(self._data["connections"]) == before:
                raise RemoteError("connection not found", "not_found", 404)
            self._save_locked()

    # ---- workspaces ------------------------------------------------------
    def list_workspaces(self, connection_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = self._data["workspaces"]
            if connection_id is not None:
                identifier(connection_id, "connection")
                rows = [w for w in rows if w["connection_id"] == connection_id]
            return [dict(w) for w in rows]

    def get_workspace(self, workspace_id: str) -> dict:
        identifier(workspace_id, "workspace")
        with self._lock:
            for ws in self._data["workspaces"]:
                if ws["id"] == workspace_id:
                    return dict(ws)
        raise RemoteError("workspace not found", "not_found", 404)

    def put_workspace(self, record: dict) -> dict:
        with self._lock:
            rows = self._data["workspaces"]
            for index, ws in enumerate(rows):
                if ws["id"] == record["id"]:
                    rows[index] = dict(record)
                    break
            else:
                rows.append(dict(record))
            self._save_locked()
        return dict(record)

    def delete_workspace(self, workspace_id: str) -> None:
        identifier(workspace_id, "workspace")
        with self._lock:
            before = len(self._data["workspaces"])
            self._data["workspaces"] = [w for w in self._data["workspaces"] if w["id"] != workspace_id]
            if len(self._data["workspaces"]) == before:
                raise RemoteError("workspace not found", "not_found", 404)
            self._save_locked()
