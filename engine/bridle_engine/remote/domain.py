"""Validated remote-workspace types, limits, and typed errors.

No authentication secrets live here or in the store: a connection references
an SSH agent or a key *file path*, never key material, passphrases, or passwords.
"""
from __future__ import annotations

import re
import uuid

HANDSHAKE_TIMEOUT = 15
FILE_TIMEOUT = 30
MAX_CONNECTIONS = 4
MAX_FOLDER_PAGE = 200
MAX_TEXT_READ = 1024 * 1024
MAX_TRANSFER = 64 * 1024 * 1024
APPROVAL_TTL = 300

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,79}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,252}$")
_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class RemoteError(Exception):
    """A remote-operation failure with a stable machine-readable code."""

    def __init__(self, message: str, code: str = "remote_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def new_id() -> str:
    return uuid.uuid4().hex


def identifier(value, what="resource"):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RemoteError(f"invalid {what} id", "invalid_id")
    return value


def clean_text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum \
            or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RemoteError(f"{field} must be text of 1-{maximum} characters without control characters", "invalid_input")
    return value.strip()


def validate_destination(body: dict) -> dict:
    """Return a normalized connection destination from an alias or manual entry."""
    name = clean_text(body.get("name"), "name", 80)
    if not _NAME_RE.fullmatch(name):
        raise RemoteError("name may contain letters, digits, spaces, '-', '_' and '.'", "invalid_input")
    alias = body.get("host_alias")
    if alias is not None:
        alias = clean_text(alias, "host_alias", 253)
        if not _HOST_RE.fullmatch(alias) or alias.startswith("-"):
            raise RemoteError("host_alias looks like an option or is malformed", "invalid_input")
        hostname = username = None
        port = None
    else:
        hostname = clean_text(body.get("hostname"), "hostname", 253)
        if not _HOST_RE.fullmatch(hostname) or hostname.startswith("-"):
            raise RemoteError("hostname looks like an option or is malformed", "invalid_input")
        username = clean_text(body.get("username"), "username", 64)
        if not _USER_RE.fullmatch(username):
            raise RemoteError("username is malformed", "invalid_input")
        port = body.get("port", 22)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise RemoteError("port must be an integer in [1, 65535]", "invalid_input")
        alias = None
    auth_kind = body.get("auth_kind", "agent")
    if auth_kind not in ("agent", "identity_file"):
        raise RemoteError("auth_kind must be 'agent' or 'identity_file'", "invalid_input")
    identity = None
    if auth_kind == "identity_file":
        identity = clean_text(body.get("identity_file"), "identity_file", 4096)
    return {"name": name, "host_alias": alias, "hostname": hostname, "username": username,
            "port": port, "auth_kind": auth_kind, "identity_file": identity}


def validate_capabilities(raw) -> list:
    if raw is None:
        return ["read"]
    if not isinstance(raw, list) or not raw or len(raw) > 2:
        raise RemoteError("capabilities must be a non-empty list", "invalid_input")
    caps = []
    for cap in raw:
        if cap not in ("read", "write"):
            raise RemoteError("capabilities may only include 'read' and 'write' in this version; "
                              "remote command execution is not available yet", "invalid_input")
        if cap not in caps:
            caps.append(cap)
    if "read" not in caps:
        caps.insert(0, "read")
    return caps


def validate_relative_path(raw) -> str:
    """A remote path relative to a workspace root. '.' means the root itself."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096 \
            or any(ord(c) < 32 for c in raw) or "\\" in raw:
        raise RemoteError("path must be a relative POSIX path without control characters", "invalid_path")
    if raw.startswith("/"):
        raise RemoteError("path must be relative to the workspace", "invalid_path")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise RemoteError("path must not contain '..'", "invalid_path")
    return "/".join(parts) if parts else "."


def validate_absolute_path(raw) -> str:
    if not isinstance(raw, str) or not raw.startswith("/") or len(raw) > 4096 \
            or any(ord(c) < 32 for c in raw) or "\\" in raw:
        raise RemoteError("path must be an absolute remote POSIX path", "invalid_path")
    return raw
