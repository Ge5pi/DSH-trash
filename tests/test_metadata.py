from __future__ import annotations

import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dsh_trash.errors import TrashIOError
from dsh_trash.metadata import collect, read_preview, sha256_file


class MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sha256(self) -> None:
        path = self.root / "a.txt"
        data = b"hello\n" * 1000
        path.write_bytes(data)
        self.assertEqual(sha256_file(path), hashlib.sha256(data).hexdigest())

    def test_preview_text(self) -> None:
        path = self.root / "a.txt"
        path.write_text("hello world", encoding="utf-8")
        text, truncated = read_preview(path)
        self.assertEqual(text, "hello world")
        self.assertFalse(truncated)

    def test_preview_binary(self) -> None:
        path = self.root / "a.bin"
        path.write_bytes(b"\x00\x01\x02")
        self.assertEqual(read_preview(path), (None, False))

    def test_preview_invalid_utf8(self) -> None:
        path = self.root / "a.bin"
        path.write_bytes(b"\xff\xfe\x00a")
        self.assertEqual(read_preview(path), (None, False))

    def test_preview_truncated(self) -> None:
        path = self.root / "a.txt"
        path.write_text("x" * 5000, encoding="utf-8")
        text, truncated = read_preview(path)
        self.assertEqual(len(text or ""), 4096)
        self.assertTrue(truncated)

    def test_file_metadata(self) -> None:
        path = self.root / "a.txt"
        path.write_text("hi", encoding="utf-8")
        os.chmod(path, 0o640)
        meta, manifest = collect(path)
        self.assertIsNone(manifest)
        self.assertEqual(meta["kind"], "file")
        self.assertEqual(meta["size"], 2)
        self.assertEqual(meta["mode"], 0o640)
        self.assertEqual(meta["preview"], "hi")
        self.assertIsNotNone(meta["sha256"])
        self.assertIsNone(meta["link_target"])

    def test_symlink_metadata(self) -> None:
        target = self.root / "target.txt"
        target.write_text("data")
        link = self.root / "link"
        link.symlink_to(target)
        meta, _ = collect(link)
        self.assertEqual(meta["kind"], "symlink")
        self.assertEqual(meta["link_target"], str(target))
        self.assertEqual(meta["size"], len(str(target)))

    def test_directory_manifest(self) -> None:
        d = self.root / "tree"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_text("aaa")
        (d / "sub" / "b.txt").write_text("bb")
        (d / "sub" / "link").symlink_to(d / "a.txt")
        meta, manifest = collect(d)
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(meta["kind"], "dir")
        self.assertEqual(meta["size"], 5)
        self.assertEqual(manifest["totals"]["files"], 2)
        self.assertEqual(manifest["totals"]["dirs"], 1)
        self.assertEqual(manifest["totals"]["symlinks"], 1)
        paths = {entry["path"] for entry in manifest["entries"]}
        self.assertEqual(paths, {"a.txt", "sub", "sub/b.txt", "sub/link"})
        self.assertFalse(manifest["truncated"])

    def test_directory_manifest_does_not_follow_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        d = self.root / "tree"
        d.mkdir()
        (d / "link").symlink_to(outside)
        _, manifest = collect(d)
        assert manifest is not None
        self.assertEqual(manifest["totals"]["files"], 0)
        self.assertEqual(manifest["totals"]["symlinks"], 1)

    def test_special_file_rejected(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo unavailable")
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(TrashIOError):
            collect(fifo)

    def test_birthtime_is_available_or_null(self) -> None:
        path = self.root / "born.txt"
        path.write_text("x")
        meta, _ = collect(path)
        self.assertTrue(meta["birthtime_ns"] is None or meta["birthtime_ns"] > 0)
