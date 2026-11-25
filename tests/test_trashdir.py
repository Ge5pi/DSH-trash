from __future__ import annotations

import os
import stat
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import quote

from dsh_trash.trashdir import (
    TrashPaths,
    move_path,
    reserve_name,
    write_trashinfo,
)


class TrashDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = TrashPaths.create(self.root / "data")
        self.paths.ensure()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_paths_layout_and_permissions(self) -> None:
        self.assertEqual(self.paths.files, self.root / "data" / "Trash" / "files")
        self.assertTrue(self.paths.files.is_dir())
        self.assertTrue(self.paths.info.is_dir())
        mode = stat.S_IMODE(self.paths.state.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_reserve_name_collisions(self) -> None:
        name = reserve_name(self.paths.files, self.paths.info, "a.txt")
        self.assertEqual(name, "a.txt")
        (self.paths.files / "a.txt").write_text("x")
        self.assertEqual(reserve_name(self.paths.files, self.paths.info, "a.txt"), "a.1.txt")
        (self.paths.files / "a.1.txt").write_text("x")
        self.assertEqual(reserve_name(self.paths.files, self.paths.info, "a.txt"), "a.2.txt")

    def test_reserve_name_hidden_file(self) -> None:
        self.assertEqual(reserve_name(self.paths.files, self.paths.info, ".bashrc"), ".bashrc")
        (self.paths.files / ".bashrc").write_text("x")
        self.assertEqual(reserve_name(self.paths.files, self.paths.info, ".bashrc"), ".bashrc.1")

    def test_reserve_name_checks_info_side(self) -> None:
        (self.paths.info / "a.txt.trashinfo").write_text("x")
        self.assertEqual(reserve_name(self.paths.files, self.paths.info, "a.txt"), "a.1.txt")

    def test_write_trashinfo(self) -> None:
        original = self.root / "ws dir" / "файл.txt"
        when = datetime(2026, 11, 25, 13, 45, 12)
        path = write_trashinfo(self.paths.info, "a.txt", original, when)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("[Trash Info]\n", text)
        self.assertIn(f"Path={quote(str(original), safe='/')}\n", text)
        self.assertIn("DeletionDate=2026-11-25T13:45:12\n", text)

    def test_move_same_device_file_dir_symlink(self) -> None:
        payload = self.paths.files
        file_src = self.root / "f.txt"
        file_src.write_text("hello")
        move_path(file_src, payload / "f.txt")
        self.assertFalse(file_src.exists())
        self.assertEqual((payload / "f.txt").read_text(), "hello")

        dir_src = self.root / "d"
        (dir_src / "sub").mkdir(parents=True)
        (dir_src / "sub" / "x").write_text("x")
        move_path(dir_src, payload / "d")
        self.assertFalse(dir_src.exists())
        self.assertEqual((payload / "d" / "sub" / "x").read_text(), "x")

        link_src = self.root / "l"
        link_src.symlink_to("/tmp/nope")
        move_path(link_src, payload / "l")
        self.assertTrue((payload / "l").is_symlink())
        self.assertEqual(os.readlink(payload / "l"), "/tmp/nope")

    def test_move_cross_device_fallback(self) -> None:
        payload = self.paths.files
        file_src = self.root / "f.txt"
        file_src.write_text("hello")
        dir_src = self.root / "d"
        dir_src.mkdir()
        (dir_src / "x").write_text("x")
        link_src = self.root / "l"
        link_src.symlink_to(file_src)

        with patch("dsh_trash.trashdir.same_device", return_value=False):
            move_path(file_src, payload / "f.txt")
            move_path(dir_src, payload / "d")
            move_path(link_src, payload / "l")

        self.assertFalse(file_src.exists())
        self.assertEqual((payload / "f.txt").read_text(), "hello")
        self.assertFalse(dir_src.exists())
        self.assertEqual((payload / "d" / "x").read_text(), "x")
        self.assertTrue((payload / "l").is_symlink())
        self.assertEqual(os.readlink(payload / "l"), str(file_src))
        leftovers = [p.name for p in payload.iterdir() if p.name.startswith(".partial-")]
        self.assertEqual(leftovers, [])
