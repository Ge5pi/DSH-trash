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
from dsh_trash.errors import ContainmentError, TrashIOError
from dsh_trash.index import Index
from dsh_trash.workspace import resolve_workspace


class PutTests(unittest.TestCase):
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

    def test_put_file(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("hello world", encoding="utf-8")
        result = self.engine.put(src, self.ws, cwd=self.ws_root)

        self.assertFalse(src.exists())
        self.assertEqual(result["relpath"], "a.txt")
        self.assertEqual(result["kind"], "file")
        self.assertEqual(result["size"], 11)
        payload = self.data / "Trash" / "files" / result["payload_name"]
        self.assertEqual(payload.read_text(), "hello world")
        info = self.data / "Trash" / "info" / f"{result['payload_name']}.trashinfo"
        self.assertTrue(info.is_file())
        self.assertIn("Path=", info.read_text())

        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            entry = index.get(result["id"])
        assert entry is not None
        self.assertEqual(entry["state"], "active")
        self.assertEqual(entry["workspace_id"], self.ws.id)
        self.assertEqual(entry["original_path"], str(src))
        self.assertEqual(entry["relpath"], "a.txt")
        self.assertEqual(entry["preview"], "hello world")
        self.assertEqual(entry["sha256"], result["sha256"])

    def test_put_directory_manifest(self) -> None:
        src = self.ws_root / "dist"
        (src / "sub").mkdir(parents=True)
        (src / "a.js").write_text("aaa")
        (src / "sub" / "b.js").write_text("bb")
        result = self.engine.put(src, self.ws, cwd=self.ws_root)
        self.assertFalse(src.exists())
        self.assertEqual(result["kind"], "dir")
        self.assertEqual(result["size"], 5)

        with Index(self.data / "dsh-trash" / "index.sqlite3") as index:
            entry = index.get(result["id"])
        assert entry is not None
        manifest_path = Path(entry["manifest_path"])
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["totals"]["files"], 2)
        self.assertTrue((self.data / "Trash" / "files" / result["payload_name"] / "a.js").exists())

    def test_put_symlink(self) -> None:
        target = self.base / "outside.txt"
        target.write_text("secret")
        link = self.ws_root / "link.txt"
        link.symlink_to(target)
        result = self.engine.put(link, self.ws, cwd=self.ws_root)
        self.assertFalse(link.is_symlink())
        payload = self.data / "Trash" / "files" / result["payload_name"]
        self.assertTrue(payload.is_symlink())
        self.assertEqual(os.readlink(payload), str(target))

    def test_put_collision_names(self) -> None:
        (self.ws_root / "a").mkdir()
        (self.ws_root / "b").mkdir()
        first = self.ws_root / "a" / "x.txt"
        second = self.ws_root / "b" / "x.txt"
        first.write_text("1")
        second.write_text("2")
        r1 = self.engine.put(first, self.ws)
        r2 = self.engine.put(second, self.ws)
        self.assertEqual(r1["payload_name"], "x.txt")
        self.assertEqual(r2["payload_name"], "x.1.txt")
        files = self.data / "Trash" / "files"
        self.assertEqual((files / "x.txt").read_text(), "1")
        self.assertEqual((files / "x.1.txt").read_text(), "2")

    def test_dry_run_writes_nothing(self) -> None:
        src = self.ws_root / "a.txt"
        src.write_text("x")
        result = self.engine.put(src, self.ws, cwd=self.ws_root, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertTrue(src.exists())
        self.assertFalse((self.data / "Trash").exists())
        self.assertFalse((self.data / "dsh-trash").exists())

    def test_outside_workspace_rejected(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_text("x")
        with self.assertRaises(ContainmentError):
            self.engine.put(outside, self.ws, cwd=self.ws_root)
        self.assertTrue(outside.exists())

    def test_git_rejected(self) -> None:
        git = self.ws_root / ".git"
        git.mkdir()
        with self.assertRaises(ContainmentError) as caught:
            self.engine.put(git, self.ws, cwd=self.ws_root)
        self.assertEqual(caught.exception.code, "PROTECTED_PATH")

    def test_special_file_rejected(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo unavailable")
        fifo = self.ws_root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(TrashIOError):
            self.engine.put(fifo, self.ws, cwd=self.ws_root)

    def test_cli_json_put(self) -> None:
        src = self.ws_root / "cli.txt"
        src.write_text("cli")
        old = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.data)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["put", str(src), "--workspace", str(self.ws_root), "--json"])
        finally:
            if old is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["relpath"], "cli.txt")
        self.assertFalse(src.exists())
        self.assertTrue((self.data / "Trash" / "files" / payload["payload_name"]).exists())

    def test_cli_error_json(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_text("x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["put", str(outside), "--workspace", str(self.ws_root), "--json"])
        self.assertEqual(code, 5)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["error"]["code"], "OUTSIDE_WORKSPACE")
