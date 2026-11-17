"""Structured SFTP (v3) file operations over a system-OpenSSH subsystem channel.

The wire protocol is binary, so filenames with spaces or newlines and large
directories are handled without parsing human-oriented tool output. Paths are
canonicalized on the remote side (OpenSSH ``realpath`` extension when offered,
otherwise a symlink-resolving walk) and confined to the workspace root.
"""
from __future__ import annotations

import os
import select
import stat as statmod
import struct
import time

from .domain import FILE_TIMEOUT, MAX_FOLDER_PAGE, RemoteError, validate_relative_path

# SFTP v3 message types
_INIT, _VERSION = 1, 2
_OPEN, _CLOSE, _READ, _WRITE = 3, 4, 5, 6
_LSTAT, _OPENDIR, _READDIR = 7, 11, 12
_REMOVE, _MKDIR, _STAT, _RENAME, _READLINK = 13, 14, 16, 17, 18
_STATUS, _HANDLE, _DATA, _ATTRS, _NAME, _EXTENDED, _EXTENDED_REPLY = 101, 102, 103, 104, 105, 200, 201
# status codes
_FX_OK, _FX_EOF, _FX_NO_SUCH, _FX_DENIED, _FX_FAILURE = 0, 1, 2, 3, 4
# open pflags (distinct from the message-type constants above)
PF_READ, PF_WRITE, PF_APPEND, PF_CREAT, PF_TRUNC, PF_EXCL = 1, 2, 4, 8, 16, 32


class _EOF(Exception):
    pass


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def _string(data) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogateescape")
    return _u32(len(data)) + data


def _parse_string(buf: bytes, off: int) -> tuple[bytes, int]:
    (length,) = struct.unpack_from(">I", buf, off)
    off += 4
    return buf[off:off + length], off + length


def _parse_attrs(buf: bytes, off: int) -> tuple[dict, int]:
    (flags,) = struct.unpack_from(">I", buf, off)
    off += 4
    attrs: dict = {}
    if flags & 1:
        (attrs["size"],) = struct.unpack_from(">Q", buf, off)
        off += 8
    if flags & 2:
        attrs["uid"], attrs["gid"] = struct.unpack_from(">II", buf, off)
        off += 8
    if flags & 4:
        (attrs["permissions"],) = struct.unpack_from(">I", buf, off)
        off += 4
    if flags & 8:
        attrs["atime"], attrs["mtime"] = struct.unpack_from(">II", buf, off)
        off += 8
    return attrs, off


def _encode_attrs(attrs: dict) -> bytes:
    flags = 0
    body = b""
    if "size" in attrs:
        flags |= 1
        body += struct.pack(">Q", attrs["size"])
    if "uid" in attrs:
        flags |= 2
        body += struct.pack(">II", attrs["uid"], attrs["gid"])
    if "permissions" in attrs:
        flags |= 4
        body += _u32(attrs["permissions"])
    return _u32(flags) + body


def describe_attrs(attrs: dict, name: str) -> dict:
    mode = attrs.get("permissions", 0)
    kind = "file"
    if statmod.S_ISDIR(mode):
        kind = "directory"
    elif statmod.S_ISLNK(mode):
        kind = "symlink"
    return {"name": name, "kind": kind, "directory": kind == "directory", "symlink": kind == "symlink",
            "size": attrs.get("size", 0), "permissions": mode,
            "uid": attrs.get("uid"), "gid": attrs.get("gid"), "mtime": attrs.get("mtime", 0)}


class SFTPClient:
    """One SFTP session over an already-launched ssh subsystem process."""

    def __init__(self, proc, timeout: float = FILE_TIMEOUT):
        self.proc = proc
        self.timeout = timeout
        self._next_id = 1
        self._buffer = bytearray()
        self.extensions: dict[str, str] = {}
        self._handshake()

    # ---- framing -----------------------------------------------------------
    def _fill(self, count: int, deadline: float) -> None:
        while len(self._buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteError("the remote file operation timed out", "timeout", 504)
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                raise RemoteError("the remote file operation timed out", "timeout", 504)
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RemoteError("the SSH connection closed during a file operation",
                                  "connection_lost", 502)
            self._buffer.extend(chunk)

    def _read_exact(self, count: int, deadline: float) -> bytes:
        self._fill(count, deadline)
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    def _send(self, msg_type: int, payload: bytes) -> None:
        packet = bytes([msg_type]) + payload
        try:
            self.proc.stdin.write(_u32(len(packet)) + packet)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RemoteError("the SSH connection closed during a file operation",
                              "connection_lost", 502) from exc

    def _receive(self, deadline: float) -> tuple[int, bytes]:
        (length,) = struct.unpack(">I", self._read_exact(4, deadline))
        if length <= 0 or length > 1 << 24:
            raise RemoteError("the remote sent an invalid SFTP packet", "protocol_error", 502)
        body = self._read_exact(length, deadline)
        return body[0], body[1:]

    def _handshake(self) -> None:
        self._send(_INIT, _u32(3))
        deadline = time.monotonic() + self.timeout
        msg_type, body = self._receive(deadline)
        if msg_type != _VERSION:
            raise RemoteError("the remote did not offer an SFTP subsystem", "protocol_error", 502)
        off = 4  # server version
        while off + 4 <= len(body):
            raw_name, off = _parse_string(body, off)
            if off + 4 > len(body):
                break
            raw_value, off = _parse_string(body, off)
            self.extensions[raw_name.decode("utf-8", "replace")] = raw_value.decode("utf-8", "replace")

    def _request(self, msg_type: int, payload: bytes = b"") -> tuple[int, bytes]:
        request_id = self._next_id
        self._next_id += 1
        self._send(msg_type, _u32(request_id) + payload)
        deadline = time.monotonic() + self.timeout
        while True:
            kind, body = self._receive(deadline)
            (got_id,) = struct.unpack_from(">I", body, 0)
            if got_id != request_id:
                raise RemoteError("the remote sent an out-of-order SFTP response", "protocol_error", 502)
            if kind == _STATUS:
                code, message = self._parse_status(body)
                if code == _FX_OK:
                    return kind, body
                if code == _FX_EOF:
                    raise _EOF()
                if code == _FX_NO_SUCH:
                    raise RemoteError(message or "no such remote file", "not_found", 404)
                if code == _FX_DENIED:
                    raise RemoteError(message or "remote permission denied", "permission_denied", 403)
                raise RemoteError(message or f"SFTP operation failed (status {code})", "sftp_failed", 502)
            return kind, body

    @staticmethod
    def _parse_status(body: bytes) -> tuple[int, str]:
        (code,) = struct.unpack_from(">I", body, 4)
        try:
            raw, _ = _parse_string(body, 8)
            message = raw.decode("utf-8", "replace")
        except (struct.error, IndexError):
            message = ""
        return code, message

    # ---- primitives ----------------------------------------------------------
    def lstat(self, path: str) -> dict:
        _, body = self._request(_LSTAT, _string(path))
        attrs, _ = _parse_attrs(body, 4)
        return attrs

    def stat(self, path: str) -> dict:
        _, body = self._request(_STAT, _string(path))
        attrs, _ = _parse_attrs(body, 4)
        return attrs

    def readlink(self, path: str) -> str:
        _, body = self._request(_READLINK, _string(path))
        (count,) = struct.unpack_from(">I", body, 4)
        if count < 1:
            raise RemoteError("the remote returned no link target", "protocol_error", 502)
        raw, _ = _parse_string(body, 8)
        return raw.decode("utf-8", "surrogateescape")

    def list_names(self, path: str) -> list[dict]:
        _, body = self._request(_OPENDIR, _string(path))
        raw_handle, _ = _parse_string(body, 4)
        entries: list[dict] = []
        try:
            while True:
                try:
                    _, page = self._request(_READDIR, _string(raw_handle))
                except _EOF:
                    break
                (count,) = struct.unpack_from(">I", page, 4)
                off = 8
                for _ in range(count):
                    raw_name, off = _parse_string(page, off)
                    _long, off = _parse_string(page, off)
                    attrs, off = _parse_attrs(page, off)
                    name = raw_name.decode("utf-8", "surrogateescape")
                    if name in (".", ".."):
                        continue
                    entries.append(describe_attrs(attrs, name))
        finally:
            try:
                self._request(_CLOSE, _string(raw_handle))
            except RemoteError:
                pass
        return entries

    def open(self, path: str, pflags: int, attrs: dict | None = None) -> bytes:
        _, body = self._request(_OPEN, _string(path) + _u32(pflags) + _encode_attrs(attrs or {}))
        handle, _ = _parse_string(body, 4)
        return handle

    def read_chunk(self, handle: bytes, offset: int, length: int) -> bytes:
        try:
            kind, body = self._request(_READ, _string(handle) + struct.pack(">QI", offset, length))
        except _EOF:
            return b""
        if kind != _DATA:
            raise RemoteError("the remote sent an unexpected read response", "protocol_error", 502)
        data, _ = _parse_string(body, 4)
        return data

    def write_chunk(self, handle: bytes, offset: int, data: bytes) -> None:
        self._request(_WRITE, _string(handle) + struct.pack(">Q", offset) + _string(data))

    def close_handle(self, handle: bytes) -> None:
        try:
            self._request(_CLOSE, _string(handle))
        except RemoteError:
            pass

    def remove(self, path: str) -> None:
        self._request(_REMOVE, _string(path))

    def mkdir(self, path: str) -> None:
        self._request(_MKDIR, _string(path) + _encode_attrs({}))

    def rename(self, old: str, new: str, overwrite: bool = False) -> None:
        if overwrite and "posix-rename@openssh.com" in self.extensions:
            self._request(_EXTENDED, _string("posix-rename@openssh.com") + _string(old) + _string(new))
            return
        self._request(_RENAME, _string(old) + _string(new))

    # ---- canonicalization -----------------------------------------------------
    def realpath(self, path: str) -> str | None:
        if "realpath@openssh.com" not in self.extensions:
            return None
        try:
            _, body = self._request(_EXTENDED, _string("realpath@openssh.com") + _string(path))
        except RemoteError:
            return None
        (count,) = struct.unpack_from(">I", body, 4)
        if count < 1:
            return None
        raw, _ = _parse_string(body, 8)
        return raw.decode("utf-8", "surrogateescape")

    def canonicalize(self, path: str, must_exist: bool = True) -> str:
        """Resolve '.'/'..' and symlinks remotely; rejects link loops."""
        resolved = self.realpath(path)
        if resolved is not None:
            return resolved
        parts = [p for p in path.split("/") if p not in ("", ".")]
        current = ""
        queue = list(parts)
        hops = 0
        while queue:
            part = queue.pop(0)
            if part == "..":
                current = current.rsplit("/", 1)[0]
                continue
            candidate = f"{current}/{part}" if current else f"/{part}"
            try:
                attrs = self.lstat(candidate)
            except RemoteError:
                if not must_exist and not queue:
                    return candidate
                raise
            mode = attrs.get("permissions", 0)
            if statmod.S_ISLNK(mode):
                hops += 1
                if hops > 20:
                    raise RemoteError("too many symlinks while resolving the remote path",
                                      "invalid_path", 400)
                target = self.readlink(candidate)
                target_parts = [p for p in target.split("/") if p not in ("", ".")]
                if target.startswith("/"):
                    current = ""
                queue = target_parts + queue
                continue
            if queue and not statmod.S_ISDIR(mode):
                raise RemoteError("a component of the remote path is not a folder",
                                  "invalid_path", 400)
            current = candidate
        return current or "/"

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - teardown is best effort
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def resolve_within(client: SFTPClient, root: str, relative: str) -> str:
    """Canonical target for `relative` inside an already-canonical `root`."""
    relative = validate_relative_path(relative)
    joined = root if relative == "." else f"{root}/{relative}"
    canonical = client.canonicalize(joined)
    if canonical != root and not canonical.startswith(root.rstrip("/") + "/"):
        raise RemoteError("the path escapes the workspace root", "path_escape", 403)
    return canonical


def parent_and_name(client: SFTPClient, root: str, relative: str) -> tuple[str, str]:
    """(canonical parent, final name) for create/write targets that may not exist yet."""
    relative = validate_relative_path(relative)
    if relative == ".":
        raise RemoteError("a file path inside the workspace is required", "invalid_path")
    parent_rel, _, name = relative.rpartition("/")
    parent = resolve_within(client, root, parent_rel or ".")
    if name in ("", ".", "..") or "/" in name:
        raise RemoteError("invalid file name", "invalid_path")
    return parent, name


def access_for(attrs: dict, identity: dict) -> tuple[bool, bool]:
    """(readable, writable) for the probed identity from POSIX mode bits."""
    mode = attrs.get("permissions", 0)
    if identity.get("uid") == 0:
        return True, True
    if attrs.get("uid") == identity.get("uid"):
        bits = (mode >> 6) & 7
    elif attrs.get("gid") == identity.get("gid") or attrs.get("gid") in identity.get("groups", []):
        bits = (mode >> 3) & 7
    else:
        bits = mode & 7
    return bool(bits & 4), bool(bits & 2)


def page_entries(entries: list[dict], offset: int, show_hidden: bool) -> dict:
    visible = [e for e in entries if show_hidden or not e["name"].startswith(".")]
    visible.sort(key=lambda e: (not e["directory"], e["name"].lower()))
    if offset < 0:
        offset = 0
    page = visible[offset:offset + MAX_FOLDER_PAGE]
    return {"entries": page, "offset": offset, "total": len(visible),
            "has_more": offset + len(page) < len(visible)}

