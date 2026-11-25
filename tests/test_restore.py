from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dsh_trash.cli import main
from dsh_trash.engine import TrashEngine
from dsh_trash.errors import ConflictError, ContainmentError, NotFoundError, TrashIOError
from dsh_trash.index import Index
from dsh_trash.workspace import resolve_workspace


class RestoreTests(unittest.TestCase):
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

    def _entry(self, entry_id: str) -> dict:
        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            entry = index.get(entry_id)
        assert entry is not None
        return entry

    def test_file_roundtrip(self) -> None:
        src = self.ws_root / "sub" / "a.txt"
        src.parent.mkdir()
        src.write_text("hello", encoding="utf-8")
        os.chmod(src, 0o640)
        old_mtime = src.stat().st_mtime_ns
        result = self.engine.put(src, self.ws, cwd=self.ws_root)
        self.assertFalse(src.exists())

        restored = self.engine.restore(result["id"], self.ws)
        self.assertEqual(restored["path"], str(src))
        self.assertEqual(restored["relpath"], "sub/a.txt")
        self.assertTrue(src.exists())
        self.assertEqual(src.read_text(), "hello")
        self.assertEqual(src.stat().st_mode & 0o777, 0o640)
        self.assertEqual(src.stat().st_mtime_ns, old_mtime)
        self.assertFalse((self.data / "Trash" / "info" / f"{result['payload_name']}.trashinfo").exists())
        self.assertEqual(self._entry(result["id"])["state"], "restored")
        self.assertEqual(self.engine.list(self.ws), [])

    def test_directory_roundtrip(self) -> None:
        src = self.ws_root / "dist"
        (src / "sub").mkdir(parents=True)
        (src / "a.js").write_text("aaa")
        (src / "sub" / "b.js").write_text("bb")
        result = self.engine.put(src, self.ws)
        self.assertFalse(src.exists())
        self.engine.restore(result["id"], self.ws)
        self.assertTrue((src / "a.js").is_file())
        self.assertEqual((src / "sub" / "b.js").read_text(), "bb")

    def test_symlink_roundtrip(self) -> None:
        target = self.base / "outside.txt"
        target.write_text("secret")
        link = self.ws_root / "link"
        link.symlink_to(target)
        result = self.engine.put(link, self.ws)
        self.engine.restore(result["id"], self.ws)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(target))

    def test_workspace_isolation(self) -> None:
        other_root = self.base / "other"
        other_root.mkdir()
        other = resolve_workspace(other_root)
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws)
        self.assertEqual(self.engine.list(other), [])
        with self.assertRaises(NotFoundError):
            self.engine.restore(result["id"], other)
        self.assertFalse(src.exists())

    def test_conflict_without_force(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("old")
        result = self.engine.put(src, self.ws)
        src.write_text("new")
        with self.assertRaises(ConflictError):
            self.engine.restore(result["id"], self.ws)
        self.assertEqual(src.read_text(), "new")

    def test_conflict_with_force_moves_existing_to_trash(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("old")
        result = self.engine.put(src, self.ws)
        src.write_text("new")
        restored = self.engine.restore(result["id"], self.ws, force=True)
        self.assertEqual(restored["path"], str(src))
        self.assertEqual(src.read_text(), "old")
        active = self.engine.list(self.ws)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["relpath"], "a.txt")
        replaced = self._entry(active[0]["id"])
        self.assertEqual(replaced["reason"], "replaced-by-restore")

    def test_restore_to_path_inside_workspace(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws)
        restored = self.engine.restore(result["id"], self.ws, to="nested/dir/a.txt")
        self.assertEqual(restored["relpath"], "nested/dir/a.txt")
        self.assertEqual((self.ws_root / "nested" / "dir" / "a.txt").read_text(), "x")

    def test_restore_to_outside_rejected(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws)
        with self.assertRaises(ContainmentError):
            self.engine.restore(result["id"], self.ws, to="../outside.txt")
        with self.assertRaises(ContainmentError):
            self.engine.restore(result["id"], self.ws, to=str(self.base / "outside.txt"))

    def test_missing_payload(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws)
        (self.data / "Trash" / "files" / result["payload_name"]).unlink()
        with self.assertRaises(NotFoundError):
            self.engine.restore(result["id"], self.ws)
        self.assertEqual(self._entry(result["id"])["state"], "missing")

    def test_checksum_mismatch(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws)
        payload = self.data / "Trash" / "files" / result["payload_name"]
        payload.write_text("tampered")
        with self.assertRaises(TrashIOError) as caught:
            self.engine.restore(result["id"], self.ws)
        self.assertEqual(caught.exception.code, "DIGEST_MISMATCH")
        self.assertFalse(src.exists())

    def test_workspace_rename_keeps_identity(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws)
        renamed = self.base / "ws2"
        self.ws_root.rename(renamed)
        new_ws = resolve_workspace(renamed)
        self.assertEqual(new_ws.id, self.ws.id)
        entries = self.engine.list(new_ws)
        self.assertEqual([entry["id"] for entry in entries], [result["id"]])
        self.engine.restore(result["id"], new_ws)
        self.assertEqual((renamed / "a.txt").read_text(), "x")

    def test_cli_list_and_restore(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.data)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["put", str(src), "--workspace", str(self.ws_root)]), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["list", "--workspace", str(self.ws_root), "--json"]), 0)
            listed = json.loads(out.getvalue())
            self.assertEqual(listed["total"], 1)
            entry_id = listed["entries"][0]["id"]

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(["restore", entry_id, "--workspace", str(self.ws_root), "--json"]), 0
                )
            restored = json.loads(out.getvalue())
            self.assertEqual(restored["relpath"], "a.txt")
            self.assertEqual(src.read_text(), "x")
        finally:
            if old is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old
