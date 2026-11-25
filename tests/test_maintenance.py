from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dsh_trash.cli import main
from dsh_trash.engine import TrashEngine
from dsh_trash.index import Index
from dsh_trash.workspace import resolve_workspace


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.data = self.base / "data"
        self.ws_root = self.base / "ws"
        self.ws_root.mkdir()
        self.ws = resolve_workspace(self.ws_root)
        self.engine = TrashEngine(self.data)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _put(self, name: str = "a.txt", content: str = "x") -> dict:
        src = self.ws_root / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(content)
        return self.engine.put(src, self.ws)

    def test_doctor_clean(self) -> None:
        self._put()
        result = self.engine.doctor()
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["checked"], 1)

    def test_doctor_missing_payload_and_fix(self) -> None:
        result = self._put()
        payload = self.data / "Trash" / "files" / result["payload_name"]
        payload.unlink()
        report = self.engine.doctor()
        self.assertEqual(report["issues"][0]["kind"], "missing_payload")
        self.engine.doctor(fix=True)
        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            self.assertEqual(index.get(result["id"])["state"], "missing")

    def test_doctor_orphan_trashinfo(self) -> None:
        self._put()
        orphan = self.data / "Trash" / "info" / "orphan.trashinfo"
        orphan.write_text("[Trash Info]\nPath=/tmp/x\nDeletionDate=2026-01-01T00:00:00\n")
        report = self.engine.doctor()
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("orphan_trashinfo", kinds)

    def test_doctor_pending(self) -> None:
        result = self._put()
        with sqlite3.connect(self.data / "dsh-trash" / "index.sqlite3") as db:
            db.execute("UPDATE entries SET state = 'pending' WHERE id = ?", (result["id"],))
        report = self.engine.doctor()
        kinds = {issue["kind"] for issue in report["issues"]}
        self.assertIn("pending", kinds)

    def test_empty_dry_run(self) -> None:
        result = self._put()
        report = self.engine.empty(self.ws, dry_run=True)
        self.assertEqual(report["deleted"], 1)
        self.assertTrue((self.data / "Trash" / "files" / result["payload_name"]).exists())
        self.assertEqual(len(self.engine.list(self.ws)), 1)

    def test_empty_deletes_payload_and_entry(self) -> None:
        result = self._put()
        report = self.engine.empty(self.ws)
        self.assertEqual(report["deleted"], 1)
        self.assertFalse((self.data / "Trash" / "files" / result["payload_name"]).exists())
        self.assertFalse((self.data / "Trash" / "info" / f"{result['payload_name']}.trashinfo").exists())
        self.assertEqual(self.engine.list(self.ws), [])
        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            self.assertIsNone(index.get(result["id"]))

    def test_empty_is_workspace_scoped(self) -> None:
        self._put()
        other_root = self.base / "other"
        other_root.mkdir()
        other = resolve_workspace(other_root)
        self.assertEqual(self.engine.empty(other)["deleted"], 0)
        self.assertEqual(len(self.engine.list(self.ws)), 1)

    def test_empty_older_than(self) -> None:
        result = self._put()
        with sqlite3.connect(self.data / "dsh-trash" / "index.sqlite3") as db:
            db.execute(
                "UPDATE entries SET trashed_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
                (result["id"],),
            )
        self.assertEqual(self.engine.empty(self.ws, older_than="1d")["deleted"], 1)

    def test_empty_older_than_skips_recent(self) -> None:
        self._put()
        self.assertEqual(self.engine.empty(self.ws, older_than="1d")["deleted"], 0)

    def test_cli_empty_requires_yes(self) -> None:
        self._put()
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.data)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["empty", "--workspace", str(self.ws_root)]), 0)
            self.assertIn("Would delete 1", out.getvalue())
            self.assertEqual(len(self.engine.list(self.ws)), 1)
        finally:
            if old is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old

    def test_cli_doctor_json(self) -> None:
        self._put()
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.data)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["doctor", "--json"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["issues"], [])
        finally:
            if old is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old


    def test_doctor_fix_recovers_pending_with_payload(self) -> None:
        result = self._put()
        info = self.data / "Trash" / "info" / f"{result['payload_name']}.trashinfo"
        info.unlink()
        with sqlite3.connect(self.data / "dsh-trash" / "index.sqlite3") as db:
            db.execute("UPDATE entries SET state = 'pending' WHERE id = ?", (result["id"],))
        self.engine.doctor(fix=True)
        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            self.assertEqual(index.get(result["id"])["state"], "active")
        self.assertTrue(info.is_file())

    def test_doctor_fix_fails_pending_without_payload(self) -> None:
        result = self._put()
        (self.data / "Trash" / "files" / result["payload_name"]).unlink()
        with sqlite3.connect(self.data / "dsh-trash" / "index.sqlite3") as db:
            db.execute("UPDATE entries SET state = 'pending' WHERE id = ?", (result["id"],))
        self.engine.doctor(fix=True)
        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            self.assertEqual(index.get(result["id"])["state"], "failed")


if __name__ == "__main__":
    unittest.main()
