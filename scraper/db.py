import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

STATUSES = {
    "downloading",
    "download_error",
    "uploading",
    "upload_error",
    "processing",
    "process_error",
    "done",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    channel_url         TEXT,
    url                 TEXT NOT NULL,
    title               TEXT,
    status              TEXT NOT NULL,
    task_id             TEXT,
    content_record_id   TEXT,
    error_message       TEXT,
    log_error           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_url);
"""


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def exists(self, video_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            return row is not None

    def get(self, video_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()

    def list_by_status(self, statuses: list[str]) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(statuses))
        with self._connect() as conn:
            return conn.execute(
                f"SELECT * FROM videos WHERE status IN ({placeholders})",
                statuses,
            ).fetchall()

    def insert(
        self,
        video_id: str,
        url: str,
        channel_url: str,
        title: Optional[str],
        status: str,
    ) -> None:
        assert status in STATUSES
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO videos (video_id, url, channel_url, title, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (video_id, url, channel_url, title, status),
            )
        log.debug("db insert: [%s] status=%s url=%s", video_id, status, url)

    def update(
        self,
        video_id: str,
        *,
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        content_record_id: Optional[str] = None,
        error_message: Optional[str] = None,
        log_error: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        if status is not None:
            assert status in STATUSES
        fields: list[str] = []
        values: list[object] = []
        for name, value in [
            ("status", status),
            ("task_id", task_id),
            ("content_record_id", content_record_id),
            ("error_message", error_message),
            ("log_error", log_error),
            ("title", title),
        ]:
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if not fields:
            return
        fields.append("updated_at = datetime('now')")
        values.append(video_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE videos SET {', '.join(fields)} WHERE video_id = ?",
                values,
            )
        changed = {
            k: v for k, v in [
                ("status", status),
                ("task_id", task_id),
                ("content_record_id", content_record_id),
                ("title", title),
            ] if v is not None
        }
        if error_message is not None:
            changed["error_message"] = error_message[:120]
        log.debug("db update: [%s] %s", video_id, changed)

    def delete(self, video_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
        log.debug("db delete: [%s]", video_id)
