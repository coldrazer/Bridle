"""Bonjour (mDNS) advertising and browsing of Bridle servers on the local network.

Other devices see "Bridle on <host>" without typing an IP; Bridle instances also see each
other, which is the foundation for multi-Mac clustering."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

log = logging.getLogger("bridle.discovery")
SERVICE = "_bridle._tcp.local."


class Discovery:
    def __init__(self, hostname: str, port: int, props: dict[str, str]):
        self.hostname = hostname
        self.port = port
        self._props = dict(props)
        self._zc = None
        self._info = None
        self._browser = None
        self._peers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.error = ""

    def start(self, advertise_address: str | None) -> None:
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
        except ImportError:
            self.error = "zeroconf package not installed"
            return
        try:
            self._zc = Zeroconf()
            if advertise_address:
                self._info = ServiceInfo(
                    SERVICE, f"Bridle on {self.hostname}.{SERVICE}",
                    addresses=[socket.inet_aton(advertise_address)], port=self.port,
                    properties=self._props, server=f"{self.hostname}.local.")
                self._zc.register_service(self._info, allow_name_change=True)
                log.info("advertising %s on %s:%d", SERVICE, advertise_address, self.port)
            self._browser = ServiceBrowser(self._zc, SERVICE, handlers=[self._on_change])
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            self.error = str(exc)
            log.warning("bonjour unavailable: %s", exc)

    def update(self, props: dict[str, str]) -> None:
        if self._zc is None or self._info is None or props == self._props:
            return
        from zeroconf import ServiceInfo

        self._props = dict(props)
        self._info = ServiceInfo(self._info.type, self._info.name, addresses=self._info.addresses,
                                 port=self._info.port, properties=self._props, server=self._info.server)
        try:
            self._zc.update_service(self._info)
        except Exception as exc:  # noqa: BLE001
            log.debug("bonjour update failed: %s", exc)

    def _on_change(self, zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        if self._info is not None and name == self._info.name:
            return  # ourselves
        if state_change.name == "Removed":
            with self._lock:
                self._peers.pop(name, None)
            return
        info = zeroconf.get_service_info(service_type, name, timeout=1500)
        if info is None:
            return
        props = {k.decode(): (v.decode() if isinstance(v, bytes) else v) for k, v in (info.properties or {}).items()}
        with self._lock:
            self._peers[name] = {
                "name": name.replace("." + SERVICE, ""),
                "addresses": info.parsed_addresses(), "port": info.port, "properties": props,
                "seen_at": time.time(),
            }

    def peers(self) -> list[dict]:
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: p["name"])

    def stop(self) -> None:
        if self._zc is None:
            return
        try:
            if self._info is not None:
                self._zc.unregister_service(self._info)
            self._zc.close()
        except Exception:  # noqa: BLE001
            pass
        self._zc = None
