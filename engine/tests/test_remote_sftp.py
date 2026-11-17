"""SFTP wire-protocol tests: a minimal in-memory SFTP v3 server on a socketpair."""
from __future__ import annotations

import os
import socket
import stat as statmod
import subprocess
from pathlib import Path
import struct
import threading
from types import SimpleNamespace

import pytest

from bridle_engine.remote.files import SFTPClient


def _u32(n):
    return struct.pack(">I", n)


def _string(data):
    if isinstance(data, str):
        data = data.encode()
    return _u32(len(data)) + data


def _read_string(buf, off):
    (n,) = struct.unpack_from(">I", buf, off)
    off += 4
    return buf[off:off + n], off + n


def _attrs(mode, size=0, uid=1000, gid=1000):
    return _u32(1 | 2 | 4) + struct.pack(">QII", size, uid, gid) + _u32(mode)


class FakeServer(threading.Thread):
    """Files: /dir (dir), /dir/a.txt (file), /link (symlink -> /dir)."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.files = {"/dir/a.txt": b"wire-bytes"}
        self.handles = {}
        self.seq = 0

    def _recv_exact(self, n):
        data = bytearray()
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)

    def _send(self, payload):
        self.sock.sendall(_u32(len(payload)) + payload)

    def _status(self, rid, code, message=""):
        self._send(bytes([101]) + _u32(rid) + _u32(code) + _string(message) + _string("en"))

    def run(self):
        try:
            while True:
                (length,) = struct.unpack(">I", self._recv_exact(4))
                body = self._recv_exact(length)
                kind, payload = body[0], body[1:]
                if kind == 1:  # INIT
                    self._send(bytes([2]) + _u32(3) + _string("realpath@openssh.com") + _string("1")
                               + _string("posix-rename@openssh.com") + _string("1"))
                    continue
                (rid,) = struct.unpack_from(">I", payload, 0)
                off = 4
                if kind == 7:  # LSTAT
                    path, _ = _read_string(payload, off)
                    path = path.decode()
                    if path == "/dir":
                        self._send(bytes([104]) + _u32(rid) + _attrs(statmod.S_IFDIR | 0o755))
                    elif path in self.files:
                        self._send(bytes([104]) + _u32(rid) + _attrs(
                            statmod.S_IFREG | 0o644, len(self.files[path])))
                    elif path == "/link":
                        self._send(bytes([104]) + _u32(rid) + _attrs(statmod.S_IFLNK | 0o777))
                    else:
                        self._status(rid, 2, "no such file")
                elif kind == 16:  # STAT (follow) - treat the link as its target dir
                    self._send(bytes([104]) + _u32(rid) + _attrs(statmod.S_IFDIR | 0o755))
                elif kind == 18:  # READLINK
                    self._send(bytes([105]) + _u32(rid) + _u32(1) + _string("/dir") + _string("/dir")
                               + _attrs(statmod.S_IFDIR | 0o755))
                elif kind == 11:  # OPENDIR
                    self.seq += 1
                    handle = f"d{self.seq}".encode()
                    self.handles[handle] = [("/dir/a.txt", "a.txt"), ("/dir/sub dir", "sub dir")]
                    self._send(bytes([102]) + _u32(rid) + _string(handle))
                elif kind == 12:  # READDIR
                    handle, _ = _read_string(payload, off)
                    names = self.handles.pop(handle, [])
                    if not names:
                        self._status(rid, 1, "eof")
                        continue
                    out = bytes([105]) + _u32(rid) + _u32(len(names))
                    for full, name in names:
                        attrs = _attrs(statmod.S_IFREG | 0o644, len(self.files.get(full, b""))) \
                            if full in self.files else _attrs(statmod.S_IFDIR | 0o755)
                        out += _string(name) + _string(name) + attrs
                    self._send(out)
                elif kind == 3:  # OPEN
                    path, off2 = _read_string(payload, off)
                    path = path.decode()
                    self.seq += 1
                    handle = f"f{self.seq}".encode()
                    self.handles[handle] = bytearray(self.files.get(path, b""))
                    self.handles[handle + b":path"] = path
                    self._send(bytes([102]) + _u32(rid) + _string(handle))
                elif kind == 5:  # READ
                    handle, off2 = _read_string(payload, off)
                    offset, want = struct.unpack_from(">QI", payload, off2)
                    data = bytes(self.handles[handle][offset:offset + want])
                    if not data:
                        self._status(rid, 1, "eof")
                    else:
                        self._send(bytes([103]) + _u32(rid) + _string(data))
                elif kind == 6:  # WRITE
                    handle, off2 = _read_string(payload, off)
                    (offset,) = struct.unpack_from(">Q", payload, off2)
                    data, _ = _read_string(payload, off2 + 8)
                    buf = self.handles[handle]
                    end = offset + len(data)
                    if end > len(buf):
                        buf.extend(b"\x00" * (end - len(buf)))
                    buf[offset:end] = data
                    self.files[self.handles[handle + b":path"]] = bytes(buf)
                    self._status(rid, 0)
                elif kind == 4:  # CLOSE
                    self._status(rid, 0)
                elif kind == 200:  # EXTENDED
                    name, off2 = _read_string(payload, off)
                    path, _ = _read_string(payload, off2)
                    path = path.decode()
                    if name == b"realpath@openssh.com":
                        resolved = "/dir" if path in ("/link", "/dir/.", "/dir") else path
                        self._send(bytes([105]) + _u32(rid) + _u32(1) + _string(resolved)
                                   + _string(resolved) + _attrs(statmod.S_IFDIR | 0o755))
                    else:
                        self._status(rid, 0)
                else:
                    self._status(rid, 4, "unsupported")
        except (EOFError, OSError, struct.error):
            return


@pytest.fixture
def sftp():
    client_sock, server_sock = socket.socketpair()
    server = FakeServer(server_sock)
    server.start()
    proc = SimpleNamespace(stdin=client_sock.makefile("wb"), stdout=client_sock.makefile("rb"),
                           terminate=lambda: client_sock.close(), wait=lambda timeout=None: 0,
                           kill=lambda: client_sock.close())
    client = SFTPClient(proc, timeout=5)
    yield client, server
    client.close()


def test_handshake_lists_extensions_and_reads(sftp):
    client, server = sftp
    assert "realpath@openssh.com" in client.extensions
    entries = client.list_names("/dir")
    assert [e["name"] for e in entries] == ["a.txt", "sub dir"]
    assert entries[0]["size"] == len(b"wire-bytes") and entries[1]["directory"]
    handle = client.open("/dir/a.txt", 1)
    assert client.read_chunk(handle, 0, 100) == b"wire-bytes"
    client.close_handle(handle)


def test_canonicalize_uses_realpath_extension(sftp):
    client, _ = sftp
    assert client.canonicalize("/link") == "/dir"
    assert client.canonicalize("/dir/.") == "/dir"


def test_write_round_trip_over_wire(sftp):
    client, server = sftp
    handle = client.open("/dir/new.txt", 2 | 8 | 16)
    client.write_chunk(handle, 0, b"written over the wire")
    client.close_handle(handle)
    assert server.files["/dir/new.txt"] == b"written over the wire"


SFTP_SERVER = Path("/usr/libexec/sftp-server")


@pytest.mark.skipif(not SFTP_SERVER.exists(), reason="needs the OpenSSH sftp-server binary")
def test_real_sftp_server_round_trip(tmp_path):
    """The client against Apple's real OpenSSH sftp-server over pipes."""
    (tmp_path / "my dir").mkdir()
    (tmp_path / "my dir/hello world.txt").write_text("real bytes")
    proc = subprocess.Popen([str(SFTP_SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    client = SFTPClient(proc, timeout=10)
    try:
        entries = client.list_names(str(tmp_path / "my dir"))
        assert [e["name"] for e in entries] == ["hello world.txt"]
        handle = client.open(str(tmp_path / "my dir/hello world.txt"), 1)
        assert client.read_chunk(handle, 0, 100) == b"real bytes"
        client.close_handle(handle)
        canonical = client.canonicalize(str(tmp_path / "my dir"))
        assert canonical == os.path.realpath(tmp_path / "my dir")
        out = tmp_path / "my dir/written.txt"
        handle = client.open(str(out), 2 | 8 | 16)
        client.write_chunk(handle, 0, b"via real server")
        client.close_handle(handle)
        assert out.read_bytes() == b"via real server"
    finally:
        client.close()
