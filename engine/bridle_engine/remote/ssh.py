"""System OpenSSH adapter: effective-config review, host trust, identity probe.

All local processes launch with executable/argument arrays; no user input is
ever concatenated into a local or remote shell string. The remote probe is a
fixed literal. BatchMode guarantees a locked key or missing agent fails with
an actionable error instead of hanging on an invisible password prompt.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import subprocess
import threading
from pathlib import Path

from .domain import HANDSHAKE_TIMEOUT, RemoteError

SSH = "/usr/bin/ssh"
KEYSCAN = "/usr/bin/ssh-keyscan"
# Fixed remote probe: emits labelled sections only, takes no input.
PROBE = ("printf 'BRIDLE_USER\\n'; id -un; printf 'BRIDLE_UID\\n'; id -u; "
         "printf 'BRIDLE_GID\\n'; id -g; printf 'BRIDLE_GROUPS\\n'; id -G; "
         "printf 'BRIDLE_HOME\\n'; pwd; printf 'BRIDLE_OS\\n'; uname -s; uname -m")


def _sanitize(text: str, limit: int = 300) -> str:
    cleaned = "".join(c if ord(c) >= 32 or c == "\n" else " " for c in text or "")
    return cleaned.strip()[:limit]


def classify_failure(stderr: str) -> RemoteError:
    text = (stderr or "").lower()
    if "remote host identification has changed" in text or "host key verification failed" in text:
        return RemoteError("the server's host key does not match the trusted key; "
                           "the connection was blocked", "host_key_changed", 409)
    if "permission denied" in text:
        return RemoteError("authentication failed; load the key into your SSH agent "
                           "(ssh-add) or check the identity file, then retry", "auth_failed", 401)
    if "connection timed out" in text or "operation timed out" in text:
        return RemoteError("the connection timed out; check the host and network, then retry",
                           "unreachable", 504)
    if "connection refused" in text or "could not resolve" in text or "no route to host" in text:
        return RemoteError("the host could not be reached; check hostname, port, and that SSH is enabled there",
                           "unreachable", 502)
    detail = _sanitize(stderr, 200)
    return RemoteError(f"SSH connection failed{f': {detail}' if detail else ''}", "ssh_failed", 502)


class SSHAdapter:
    def __init__(self, data_dir: Path, run=None, popen=None):
        self.data_dir = Path(data_dir)
        self.known_hosts = self.data_dir / "remote_known_hosts"
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen
        self._lock = threading.Lock()
        self._active: dict[str, set] = {}

    # ---- destinations and arguments --------------------------------------
    @staticmethod
    def destination(conn: dict) -> str:
        if conn.get("host_alias"):
            return conn["host_alias"]
        return f"{conn['username']}@{conn['hostname']}"

    def base_args(self, conn: dict) -> list[str]:
        args = [SSH, "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={HANDSHAKE_TIMEOUT}",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ForwardAgent=no",
                "-o", "ClearAllForwardings=yes",
                "-o", "PermitLocalCommand=no",
                "-o", "RequestTTY=no"]
        if conn.get("host_alias") is None and conn.get("port") not in (None, 22):
            args += ["-p", str(conn["port"])]
        if conn.get("auth_kind") == "identity_file":
            identity = str(Path(conn["identity_file"]).expanduser())
            if not Path(identity).is_file():
                raise RemoteError("the identity file no longer exists; choose it again", "auth_failed", 401)
            args += ["-i", identity, "-o", "IdentitiesOnly=yes"]
        else:
            args += ["-o", "IdentitiesOnly=no"]
        return args

    # ---- effective configuration ------------------------------------------
    def effective_config(self, conn: dict) -> dict:
        try:
            result = self._run([SSH, "-G", self.destination(conn)],
                                 capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteError("could not review the SSH configuration", "ssh_failed", 502) from exc
        if result.returncode != 0:
            raise RemoteError("the SSH destination could not be resolved from local configuration",
                              "invalid_destination", 400)
        values: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            key, _, rest = line.partition(" ")
            if key and key not in values:
                values[key] = rest.strip()
        effective = {"hostname": values.get("hostname", ""), "user": values.get("user", ""),
                     "port": int(values["port"]) if values.get("port", "").isdigit() else conn.get("port") or 22,
                     "uses_proxy": bool(values.get("proxycommand") or values.get("proxyjump")),
                     "trusted_user_config": bool(values.get("proxycommand") or values.get("proxyjump"))}
        effective["destination"] = (f"{effective['user']}@{effective['hostname']}:{effective['port']}"
                                    if effective["user"] else f"{effective['hostname']}:{effective['port']}")
        return effective

    # ---- host keys ---------------------------------------------------------
    def scan_host_key(self, conn: dict) -> dict:
        effective = self.effective_config(conn)
        host, port = effective["hostname"], effective["port"]
        try:
            result = self._run([KEYSCAN, "-T", "10", "-p", str(port), "-t", "ed25519,ecdsa,rsa", host],
                               capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteError("could not fetch the server host key; check the host and port",
                              "unreachable", 502) from exc
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                blob = base64.b64decode(fields[2], validate=True)
            except (binascii.Error, ValueError):
                continue
            digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
            return {"fingerprint": f"SHA256:{digest}", "key_type": fields[1],
                    "key_line": line, "effective": effective}
        raise RemoteError("the server did not present an SSH host key; check that SSH is enabled there",
                          "unreachable", 502)

    def trust_key(self, key_line: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        existing = self.known_hosts.read_text() if self.known_hosts.is_file() else ""
        if key_line not in existing.splitlines():
            with self.known_hosts.open("a", encoding="utf-8") as handle:
                handle.write(key_line + "\n")
            self.known_hosts.chmod(0o600)

    def trusted_fingerprint(self, conn: dict) -> str:
        return str(conn.get("approved_host_key_fingerprint") or "")

    # ---- probes and process lifecycle --------------------------------------
    def probe(self, conn: dict) -> dict:
        args = self.base_args(conn) + [self.destination(conn), PROBE]
        try:
            result = self._run(args, capture_output=True, text=True, timeout=HANDSHAKE_TIMEOUT + 5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RemoteError("the connection handshake timed out after 15 seconds",
                              "unreachable", 504) from exc
        except OSError as exc:
            raise RemoteError("could not launch the system SSH client", "ssh_failed", 502) from exc
        if result.returncode != 0:
            raise classify_failure(result.stderr or "")
        sections: dict[str, list[str]] = {}
        current = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("BRIDLE_"):
                current = line.strip()
                sections[current] = []
            elif current:
                sections[current].append(line.strip())
        try:
            identity = {"user": sections["BRIDLE_USER"][0],
                        "uid": int(sections["BRIDLE_UID"][0]),
                        "gid": int(sections["BRIDLE_GID"][0]),
                        "groups": [int(x) for x in sections["BRIDLE_GROUPS"][0].split()],
                        "home": sections["BRIDLE_HOME"][0],
                        "os": sections["BRIDLE_OS"][0],
                        "arch": sections["BRIDLE_OS"][1]}
        except (KeyError, IndexError, ValueError) as exc:
            raise RemoteError("the remote host answered but its identity probe was not understood",
                              "probe_failed", 502) from exc
        if not identity["home"].startswith("/"):
            raise RemoteError("the remote host did not report an absolute home directory",
                              "probe_failed", 502)
        return identity

    def open_sftp(self, conn: dict):
        """Launch an SFTP subsystem channel; returns the process (stdin/stdout are the protocol)."""
        destination = self.destination(conn)
        args = self.base_args(conn) + ["-s", destination, "sftp"]
        try:
            proc = self._popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise RemoteError("could not launch the system SSH client", "ssh_failed", 502) from exc
        with self._lock:
            self._active.setdefault(conn["id"], set()).add(proc)
        return proc

    def release(self, conn_id: str, proc) -> None:
        with self._lock:
            active = self._active.get(conn_id)
            if active:
                active.discard(proc)
                if not active:
                    self._active.pop(conn_id, None)

    def disconnect(self, conn_id: str) -> int:
        with self._lock:
            procs = list(self._active.pop(conn_id, set()))
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        return len(procs)

    def shutdown(self) -> None:
        with self._lock:
            ids = list(self._active)
        for conn_id in ids:
            self.disconnect(conn_id)

    @staticmethod
    def agent_available() -> bool:
        return bool(os.environ.get("SSH_AUTH_SOCK"))
