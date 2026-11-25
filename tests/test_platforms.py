from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dsh_trash.engine import TrashEngine
from dsh_trash.platforms import default_data_home, system
from dsh_trash.trashdir import TrashPaths
from dsh_trash.workspace import resolve_workspace


class PlatformPathTests(unittest.TestCase):
    def test_system_override(self) -> None:
        with patch.dict(os.environ, {"DSH_TRASH_PLATFORM": "darwin"}):
            self.assertEqual(system(), "darwin")

    def test_linux_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = TrashPaths.create(Path(tmp) / "data", platform="linux", home=Path(tmp) / "home")
            self.assertTrue(paths.freedesktop)
            self.assertEqual(paths.trash, Path(tmp) / "data" / "Trash")
            self.assertEqual(paths.files, paths.trash / "files")
            self.assertEqual(paths.info, paths.trash / "info")

    def test_macos_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            paths = TrashPaths.create(Path(tmp) / "data", platform="darwin", home=home)
            self.assertFalse(paths.freedesktop)
            self.assertEqual(paths.trash, home / ".Trash")
            self.assertEqual(paths.files, home / ".Trash")
            self.assertIsNone(paths.info)

    def test_windows_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            paths = TrashPaths.create(data, platform="windows", home=Path(tmp) / "home")
            self.assertFalse(paths.freedesktop)
            self.assertEqual(paths.trash, data / "dsh-trash" / "Trash")
            self.assertEqual(paths.files, paths.trash / "files")
            self.assertIsNone(paths.info)

    def test_default_data_home(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(Path(tmp) / "local")}, clear=False):
                self.assertEqual(
                    default_data_home("windows", home), Path(tmp) / "local"
                )
            self.assertEqual(
                default_data_home("darwin", home), home / "Library" / "Application Support"
            )
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(Path(tmp) / "xdg")}, clear=False):
                self.assertEqual(default_data_home("linux", home), Path(tmp) / "xdg")


class PortableEngineTests(unittest.TestCase):
    def _roundtrip(self, platform: str) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            home = base / "home"
            home.mkdir()
            workspace_root = base / "ws"
            workspace_root.mkdir()
            workspace = resolve_workspace(workspace_root)
            engine = TrashEngine(data, platform=platform, home=home)

            source = workspace_root / "a.txt"
            source.write_text("portable", encoding="utf-8")
            result = engine.put(source, workspace)
            self.assertFalse(source.exists())

            if platform == "darwin":
                payload = home / ".Trash" / "a.txt"
            else:
                payload = data / "dsh-trash" / "Trash" / "files" / "a.txt"
            self.assertEqual(payload.read_text(encoding="utf-8"), "portable")
            self.assertFalse(list(data.glob("**/*.trashinfo")))

            listed = engine.list(workspace)
            self.assertEqual([entry["id"] for entry in listed], [result["id"]])
            self.assertEqual(engine.doctor()["issues"], [])

            engine.restore(result["id"], workspace)
            self.assertEqual(source.read_text(encoding="utf-8"), "portable")
            self.assertFalse(payload.exists())

    def test_macos_roundtrip(self) -> None:
        self._roundtrip("darwin")

    def test_windows_roundtrip(self) -> None:
        self._roundtrip("windows")


if __name__ == "__main__":
    unittest.main()
