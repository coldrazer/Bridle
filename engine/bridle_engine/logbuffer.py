"""In-memory log ring buffer so the app's Dev Tools panel can tail engine logs."""

from __future__ import annotations

import logging
import threading
from collections import deque


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._lines: deque[dict] = deque(maxlen=capacity)
        self._seq = 0
        self._lock2 = threading.Lock()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - never let logging break the server
            message = record.getMessage()
        with self._lock2:
            self._seq += 1
            self._lines.append({
                "seq": self._seq,
                "ts": record.created,
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": message,
            })

    def since(self, after: int, limit: int = 500) -> tuple[list[dict], int]:
        with self._lock2:
            lines = [l for l in self._lines if l["seq"] > after][:limit]
            return lines, (lines[-1]["seq"] if lines else max(after, 0))
