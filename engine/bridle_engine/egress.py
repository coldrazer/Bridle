"""Outbound-connection evidence: every network call the engine makes is
recorded (host + purpose only, never payloads) so the app can show what
left this Mac and why. In-memory and bounded; newest last."""

from __future__ import annotations

import threading
import time
from collections import deque
from urllib.parse import urlsplit


class EgressLog:
    def __init__(self, capacity: int = 500):
        self._events: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, destination: str, purpose: str) -> None:
        host = destination
        if "://" in destination:
            host = urlsplit(destination).hostname or destination
        with self._lock:
            self._events.append({"ts": time.time(), "host": host, "purpose": purpose})

    def list(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._events)[-max(1, min(limit, 500)):][::-1]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._events)


# One log per process: call sites in downloads/vision/operations/mesh record
# here; the engine exposes it at /control/egress.
LOG = EgressLog()


def record(destination: str, purpose: str) -> None:
    LOG.record(destination, purpose)
