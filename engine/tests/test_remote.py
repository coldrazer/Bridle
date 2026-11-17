"""Remote workspace behavior against a fake SSH/SFTP fixture (no network)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bridle_engine.remote.domain import RemoteError, validate_destination
from bridle_engine.remote.routes import RemoteManager
from bridle_engine.remote.ssh import SSHAdapter


class FakeSFTP:
    """SFTPClient-shaped fake backed by a local directory standing in for '/'."""

    def __init__(self, rootfs: Path):
        self.rootfs = Path(rootfs)
        self.handles: dict[bytes, object] = {}
        self._seq = 0

    def _map(self, remote: str) -> Path:
        return self.rootfs / remote.lstrip("/")

    def canonicalize(self, path: str, must_exist: bool = True) -> str:
        mapped = self._map(path)
        resolved = Path(os.path.realpath(mapped))
        if must_exist and not resolved.exists():
            raise RemoteError(f"no such remote file: {path}", "not_found", 404)
        return "/" + str(resolved.relative_to(self.rootfs)) if resolved != self.rootfs else "/"

    def lstat(self, path: str) -> dict:
        try:
            st = os.lstat(self._map(path))
        except FileNotFoundError:
            raise RemoteError("no such remote file", "not_found", 404) from None
        return {"size": st.st_size, "uid": st.st_uid, "gid": st.st_gid, "permissions": st.st_mode}

    def list_names(self, path: str) -> list[dict]:
        out = []
        with os.scandir(self._map(path)) as entries:
            for entry in entries:
                st = entry.stat(follow_symlinks=False)
                kind = "directory" if entry.is_dir(follow_symlinks=False) else \
                    "symlink" if entry.is_symlink() else "file"
                out.append({"name": entry.name, "kind": kind, "directory": kind == "directory",
                            "symlink": kind == "symlink", "size": st.st_size,
                            "permissions": st.st_mode, "uid": st.st_uid, "gid": st.st_gid,
                            "mtime": int(st.st_mtime)})
        return out

    def open(self, path: str, pflags: int, attrs=None) -> bytes:
        self._seq += 1
        handle = f"h{self._seq}".encode()
        mapped = self._map(path)
        if pflags & 1:
            self.handles[handle] = mapped.open("rb")
        else:
            mode = "wb" if pflags & 16 else "r+b"
            if pflags & 8 and not mapped.exists():
                mapped.touch()
            self.handles[handle] = mapped.open(mode)
        return handle

    def read_chunk(self, handle: bytes, offset: int, length: int) -> bytes:
        fh = self.handles[handle]
        fh.seek(offset)
        return fh.read(length)

    def write_chunk(self, handle: bytes, offset: int, data: bytes) -> None:
        fh = self.handles[handle]
        fh.seek(offset)
        fh.write(data)
        fh.flush()

    def close_handle(self, handle: bytes) -> None:
        fh = self.handles.pop(handle, None)
        if fh is not None:
            fh.close()

    def remove(self, path: str) -> None:
        self._map(path).unlink()

    def rename(self, old: str, new: str, overwrite: bool = False) -> None:
        os.replace(self._map(old), self._map(new))


class FakeAdapter(SSHAdapter):
    def __init__(self, data_dir: Path):
        super().__init__(data_dir)
        self.fingerprint = "SHA256:FakeFingerprintForTests"
        self.trusted_lines: list[str] = []

    def scan_host_key(self, conn):
        return {"fingerprint": self.fingerprint, "key_type": "ssh-ed25519",
                "key_line": "host.example ssh-ed25519 FAKEKEY",
                "effective": {"hostname": "host.example", "user": "alex", "port": 22,
                              "destination": "alex@host.example:22", "uses_proxy": False,
                              "trusted_user_config": False}}

    def trust_key(self, key_line: str) -> None:
        self.trusted_lines.append(key_line)

    def probe(self, conn):
        return {"user": "alex", "uid": os.getuid(), "gid": os.getgid(),
                "groups": [os.getgid()], "home": "/home/alex", "os": "Darwin", "arch": "arm64"}


@pytest.fixture
def remote(tmp_path):
    rootfs = tmp_path / "rootfs"
    (rootfs / "home/alex").mkdir(parents=True)
    site = rootfs / "work/site"
    site.mkdir(parents=True)
    (site / "hello.txt").write_text("hello remote")
    (site / "my file's [draft].md").write_text("spaced name")
    manager = RemoteManager(tmp_path / "data", adapter=FakeAdapter(tmp_path / "data"))
    manager._session = lambda conn: FakeSFTP(rootfs)
    manager._release = lambda conn, client: None
    return manager, rootfs


def connect_and_trust(manager) -> dict:
    conn = manager.create_connection({"name": "Dev Mac", "hostname": "host.example",
                                      "username": "alex", "port": 22, "auth_kind": "agent"})
    first = manager.test_connection(conn["id"])
    assert first["status"] == "trust_required"
    trusted = manager.trust({"attempt_id": first["attempt_id"],
                             "fingerprint": first["fingerprint"]}, connection_id=conn["id"])
    assert trusted["status"] == "connected"
    return trusted["connection"]


def make_workspace(manager) -> dict:
    conn = connect_and_trust(manager)
    return manager.create_workspace({"connection_id": conn["id"], "path": "/work/site",
                                     "capabilities": ["read", "write"]})


def test_remote_routes_are_control_protected(tmp_path):
    from test_api import client_for, CTL
    c, _ = client_for(tmp_path / "m", tmp_path)
    assert c.get("/control/remote/connections").status_code == 401
    assert c.get("/control/remote/connections", headers=CTL).json() == {
        "connections": [], "store_load_error": ""}
    bad = c.post("/control/remote/connections", headers=CTL,
                 json={"name": "x", "host_alias": "-oProxyCommand=evil"})
    assert bad.status_code == 400
    good = c.post("/control/remote/connections", headers=CTL,
                  json={"name": "Dev Mac", "hostname": "host.example", "username": "alex"})
    assert good.status_code == 200 and good.json()["name"] == "Dev Mac"
    assert len(c.get("/control/remote/connections", headers=CTL).json()["connections"]) == 1


def test_destination_validation_rejects_option_injection():
    with pytest.raises(RemoteError):
        validate_destination({"name": "x", "host_alias": "-oProxyCommand=evil"})
    with pytest.raises(RemoteError):
        validate_destination({"name": "x", "hostname": "-evil.example", "username": "alex"})
    with pytest.raises(RemoteError):
        validate_destination({"name": "bad\nname", "host_alias": "dev"})
    ok = validate_destination({"name": "Dev", "host_alias": "devbox"})
    assert ok["host_alias"] == "devbox" and ok["auth_kind"] == "agent"


def test_trust_flow_requires_explicit_match(remote):
    manager, _ = remote
    conn = manager.create_connection({"name": "Dev Mac", "hostname": "host.example",
                                      "username": "alex", "auth_kind": "agent"})
    first = manager.test_connection(conn["id"])
    assert first["status"] == "trust_required" and first["fingerprint"].startswith("SHA256:")
    with pytest.raises(RemoteError) as err:
        manager.trust({"attempt_id": first["attempt_id"], "fingerprint": "SHA256:other"},
                      connection_id=conn["id"])
    assert err.value.code == "trust_mismatch"
    # The failed attempt is consumed; a new test issues a fresh attempt.
    second = manager.test_connection(conn["id"])
    trusted = manager.trust({"attempt_id": second["attempt_id"],
                             "fingerprint": second["fingerprint"]}, connection_id=conn["id"])
    assert trusted["identity"]["user"] == "alex"
    assert manager.store.get_connection(conn["id"])["approved_host_key_fingerprint"]


def test_changed_host_key_is_blocked(remote):
    manager, _ = remote
    conn = connect_and_trust(manager)
    manager.adapter.fingerprint = "SHA256:ChangedKey"
    with pytest.raises(RemoteError) as err:
        manager.test_connection(conn["id"])
    assert err.value.code == "host_key_changed"


def test_browse_workspace_read_and_unicode_names(remote):
    manager, _ = remote
    ws = make_workspace(manager)
    assert ws["canonical_root"] == "/work/site" and ws["capabilities"] == ["read", "write"]
    listing = manager.browse(ws["connection_id"], "/work/site")
    names = [e["name"] for e in listing["entries"]]
    assert "hello.txt" in names and "my file's [draft].md" in names
    assert listing["readable"] is True
    read = manager.read_file(ws["id"], {"path": "my file's [draft].md"})
    assert read["content"] == "spaced name" and read["bytes"] == 11


def test_symlink_escape_and_traversal_are_rejected(remote):
    manager, rootfs = remote
    ws = make_workspace(manager)
    outside = rootfs / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (rootfs / "work/site/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RemoteError) as err:
        manager.read_file(ws["id"], {"path": "link/secret.txt"})
    assert err.value.code == "path_escape"
    with pytest.raises(RemoteError):
        manager.read_file(ws["id"], {"path": "../outside/secret.txt"})


def test_write_preview_apply_and_single_use(remote):
    manager, rootfs = remote
    ws = make_workspace(manager)
    preview = manager.preview_write(ws["id"], {"path": "notes.txt", "content": "v1",
                                               "task_id": "a" * 32})
    assert preview["previous_sha256"] is None
    with pytest.raises(RemoteError):  # changed content does not match the approval
        manager.apply_write(ws["id"], {"approval": preview["approval"], "content": "tampered"})
    preview2 = manager.preview_write(ws["id"], {"path": "notes.txt", "content": "v1"})
    applied = manager.apply_write(ws["id"], {"approval": preview2["approval"], "content": "v1"})
    assert applied["written"] and (rootfs / "work/site/notes.txt").read_text() == "v1"
    with pytest.raises(RemoteError) as err:  # single use
        manager.apply_write(ws["id"], {"approval": preview2["approval"], "content": "v1"})
    assert err.value.code == "approval_invalid"
    # Stale digest: change the file, then try a preview made before the change.
    preview3 = manager.preview_write(ws["id"], {"path": "notes.txt", "content": "v2"})
    (rootfs / "work/site/notes.txt").write_text("someone else")
    with pytest.raises(RemoteError) as err2:
        manager.apply_write(ws["id"], {"approval": preview3["approval"], "content": "v2"})
    assert err2.value.code == "approval_stale"


def test_transfers_round_trip(remote, tmp_path):
    manager, rootfs = remote
    ws = make_workspace(manager)
    down = manager.preview_transfer(ws["id"], {"direction": "download", "path": "hello.txt",
                                               "local_path": str(tmp_path / "got.txt")})
    out = manager.apply_transfer(ws["id"], {"approval": down["approval"]})
    assert out["transferred"] and (tmp_path / "got.txt").read_text() == "hello remote"
    local = tmp_path / "up.txt"
    local.write_text("upload me")
    up = manager.preview_transfer(ws["id"], {"direction": "upload", "path": "up.txt",
                                             "local_path": str(local)})
    manager.apply_transfer(ws["id"], {"approval": up["approval"]})
    assert (rootfs / "work/site/up.txt").read_text() == "upload me"


def test_intelligence_job_stores_immutable_binding(remote, tmp_path):
    from bridle_engine.intelligence import IntelligenceStore
    manager, _ = remote
    ws = make_workspace(manager)
    store = IntelligenceStore(tmp_path / "intel", lambda *a, **k: "answer")
    store.workspace_resolver = manager.binding_for
    project = store.create_project("Site work")
    # Project default applies when the job names none.
    store.update_project(project["id"], {"workspace_ref": {"location": "remote",
                                                           "workspace_id": ws["id"]}})
    job = store.submit({"project_id": project["id"], "prompt": "summarize"})
    assert job["workspace"]["location"] == "remote"
    assert job["workspace"]["canonical_root"] == "/work/site"
    # Per-task override with a local binding wins and stays stored.
    local = store.submit({"project_id": project["id"], "prompt": "local one",
                          "workspace_binding": {"location": "local"}})
    assert local["workspace"] == {"location": "local", "inference_location": "local",
                                  "binding_version": 1}
    # A job naming a forgotten workspace fails at submit, not silently local.
    manager.forget(ws["connection_id"])
    with pytest.raises((ValueError, RemoteError)):
        store.submit({"project_id": project["id"], "prompt": "after forget",
                      "workspace_binding": {"location": "remote", "workspace_id": ws["id"]}})
    store.close()


def test_binding_snapshot_and_forget_invalidation(remote):
    manager, _ = remote
    ws = make_workspace(manager)
    snap = manager.binding_for({"location": "remote", "workspace_id": ws["id"]})
    assert snap["location"] == "remote" and snap["canonical_root"] == "/work/site"
    assert snap["inference_location"] == "local"
    manager.forget(ws["connection_id"])
    with pytest.raises(RemoteError):
        manager.binding_for({"location": "remote", "workspace_id": ws["id"]})
