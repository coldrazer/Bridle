"""Request history in SQLite. Prompt/response text is stored only when log_content is on."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_ROWS = 10_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT '',
    remote TEXT NOT NULL DEFAULT '',
    status INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    tps REAL NOT NULL DEFAULT 0,
    finish_reason TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    prompt TEXT,
    response TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
"""


@dataclass
class RequestRecord:
    endpoint: str
    status: int
    model: str = ""
    client: str = ""
    remote: str = ""
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    tps: float = 0.0
    finish_reason: str = ""
    error: str = ""
    prompt: str | None = None
    response: str | None = None
    ts: float = 0.0


class RequestHistory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._inserts = 0

    def add(self, rec: RequestRecord, keep_content: bool) -> None:
        row = asdict(rec)
        row["ts"] = rec.ts or time.time()
        if not keep_content:
            row["prompt"] = row["response"] = None
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._db.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", tuple(row.values()))
            self._inserts += 1
            if self._inserts % 200 == 0:
                self._db.execute("DELETE FROM requests WHERE id <= (SELECT MAX(id) FROM requests) - ?", (MAX_ROWS,))

    def list(self, limit: int = 100, before_id: int | None = None, model: str = "", client: str = "") -> list[dict]:
        sql = "SELECT * FROM requests WHERE 1=1"
        args: list = []
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        if model:
            sql += " AND model = ?"
            args.append(model)
        if client:
            sql += " AND client = ?"
            args.append(client)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with self._lock:
            cur = self._db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def summary(self, since_seconds: int = 86400) -> dict:
        since = time.time() - since_seconds
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(completion_tokens),0), COALESCE(SUM(prompt_tokens),0), "
                "COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END),0), COALESCE(AVG(NULLIF(tps,0)),0) "
                "FROM requests WHERE ts >= ?", (since,)).fetchone()
        return {"requests": row[0], "completion_tokens": row[1], "prompt_tokens": row[2],
                "errors": row[3], "avg_tps": round(row[4], 1), "window_seconds": since_seconds}

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM requests")
