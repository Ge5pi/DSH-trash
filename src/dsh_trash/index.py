"""SQLite sidecar index: workspaces, entries, and crash-safe state transitions."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import IndexError as TrashIndexError
from .workspace import Workspace

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id           TEXT PRIMARY KEY,
  root         TEXT NOT NULL,
  root_dev     INTEGER NOT NULL,
  root_ino     INTEGER NOT NULL,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE (root_dev, root_ino)
);

CREATE TABLE IF NOT EXISTS entries (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  original_path   TEXT NOT NULL,
  relpath         TEXT NOT NULL,
  filename        TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'symlink')),
  size            INTEGER NOT NULL,
  mode            INTEGER NOT NULL,
  uid             INTEGER,
  gid             INTEGER,
  mtime_ns        INTEGER NOT NULL,
  ctime_ns        INTEGER NOT NULL,
  birthtime_ns    INTEGER,
  link_target     TEXT,
  sha256          TEXT,
  preview         TEXT,
  manifest_path   TEXT,
  trash_dir       TEXT NOT NULL,
  payload_name    TEXT NOT NULL,
  payload_path    TEXT NOT NULL,
  trashinfo_path  TEXT NOT NULL,
  trashed_at      TEXT NOT NULL,
  reason          TEXT,
  state           TEXT NOT NULL CHECK (state IN ('pending', 'active', 'restored', 'missing', 'failed')),
  restored_at     TEXT,
  restore_target  TEXT,
  restored_sha256 TEXT,
  error           TEXT
);

CREATE INDEX IF NOT EXISTS entries_workspace_time ON entries(workspace_id, trashed_at DESC);
CREATE INDEX IF NOT EXISTS entries_workspace_path ON entries(workspace_id, original_path);
CREATE INDEX IF NOT EXISTS entries_state ON entries(state);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Index:
    """One SQLite connection; use as a context manager."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._db = sqlite3.connect(path, timeout=5.0)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except sqlite3.Error as exc:
            raise TrashIndexError(f"cannot open trash index: {path}: {exc}") from exc

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise TrashIndexError(
                f"trash index schema v{version} is newer than supported v{SCHEMA_VERSION}"
            )
        if version == 0:
            with self._db:
                self._db.executescript(SCHEMA)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def ensure_workspace(self, workspace: Workspace) -> str:
        with self._db:
            row = self._db.execute(
                "SELECT id, root FROM workspaces WHERE root_dev = ? AND root_ino = ?",
                (workspace.dev, workspace.ino),
            ).fetchone()
            now = utc_now()
            if row is not None:
                if row["root"] != str(workspace.root):
                    self._db.execute(
                        "UPDATE workspaces SET root = ?, last_seen_at = ? WHERE id = ?",
                        (str(workspace.root), now, row["id"]),
                    )
                else:
                    self._db.execute(
                        "UPDATE workspaces SET last_seen_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                return str(row["id"])
            self._db.execute(
                "INSERT INTO workspaces (id, root, root_dev, root_ino, created_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (workspace.id, str(workspace.root), workspace.dev, workspace.ino, now, now),
            )
        return workspace.id

    def insert_pending(self, entry: dict[str, Any]) -> None:
        columns = ", ".join(entry)
        placeholders = ", ".join("?" for _ in entry)
        try:
            with self._db:
                self._db.execute(
                    f"INSERT INTO entries ({columns}) VALUES ({placeholders})",
                    tuple(entry.values()),
                )
        except sqlite3.Error as exc:
            raise TrashIndexError(f"cannot insert trash entry: {exc}") from exc

    def mark_active(self, entry_id: str, *, payload_name: str, payload_path: str, trashinfo_path: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE entries SET payload_name = ?, payload_path = ?, trashinfo_path = ?,"
                " state = 'active', error = NULL WHERE id = ?",
                (payload_name, payload_path, trashinfo_path, entry_id),
            )

    def mark_failed(self, entry_id: str, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE entries SET state = 'failed', error = ? WHERE id = ?",
                (error, entry_id),
            )

    def mark_restored(self, entry_id: str, *, restored_at: str, target: str, sha256: str | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE entries SET state = 'restored', restored_at = ?, restore_target = ?,"
                " restored_sha256 = ?, error = NULL WHERE id = ?",
                (restored_at, target, sha256, entry_id),
            )

    def mark_missing(self, entry_id: str, error: str = "payload is missing") -> None:
        with self._db:
            self._db.execute(
                "UPDATE entries SET state = 'missing', error = ? WHERE id = ?",
                (error, entry_id),
            )

    def get(self, entry_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row is not None else None

    def find_in_workspace(self, entry_id: str, workspace_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM entries WHERE id = ? AND workspace_id = ?",
            (entry_id, workspace_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_entries(
        self,
        workspace_id: str | None,
        *,
        limit: int = 100,
        state: str | None = "active",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entries WHERE 1 = 1"
        params: list[Any] = []
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY trashed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._db.execute(sql, params)]

    def count(self, workspace_id: str, *, state: str | None = "active") -> int:
        sql = "SELECT COUNT(*) FROM entries WHERE workspace_id = ?"
        params: list[Any] = [workspace_id]
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        return int(self._db.execute(sql, params).fetchone()[0])

    def workspaces(self) -> Iterable[dict[str, Any]]:
        return (dict(row) for row in self._db.execute("SELECT * FROM workspaces ORDER BY last_seen_at DESC"))

    def all_entries(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.execute("SELECT * FROM entries")]

    def find_by_trashinfo(self, trashinfo_path: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM entries WHERE trashinfo_path = ?", (trashinfo_path,)
        ).fetchone()
        return dict(row) if row is not None else None

    def delete(self, entry_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
