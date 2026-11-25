from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dsh_trash.errors import ContainmentError, NotFoundError
from dsh_trash.workspace import Workspace, is_within, resolve_target, resolve_workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.ws = resolve_workspace(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolve_and_stable_id(self) -> None:
        again = resolve_workspace(self.root)
        self.assertEqual(self.ws.id, again.id)
        self.assertEqual(len(self.ws.id), 16)
        self.assertTrue(self.ws.root.is_absolute())

    def test_inside_and_outside(self) -> None:
        inside = self.root / "a" / "b.txt"
        inside.parent.mkdir()
        inside.write_text("x")
        self.assertTrue(is_within(self.ws.root, inside))
        self.assertFalse(is_within(self.ws.root, self.ws.root))
        self.assertFalse(is_within(self.ws.root, self.root.parent / "other"))
        self.assertFalse(is_within(self.ws.root, Path(str(self.root) + "2")))

    def test_target_parent_traversal_rejected(self) -> None:
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("x")
        with self.assertRaises(ContainmentError):
            resolve_target(outside, cwd=self.root, workspace=self.ws)
        with self.assertRaises(ContainmentError):
            resolve_target("../outside.txt", cwd=self.root, workspace=self.ws)

    def test_relative_target(self) -> None:
        (self.root / "sub").mkdir()
        target = self.root / "sub" / "f.txt"
        target.write_text("x")
        resolved = resolve_target("sub/f.txt", cwd=self.root, workspace=self.ws)
        self.assertEqual(resolved, target)

    def test_symlink_final_component_is_not_followed(self) -> None:
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("secret")
        link = self.root / "link.txt"
        link.symlink_to(outside)
        resolved = resolve_target("link.txt", cwd=self.root, workspace=self.ws)
        self.assertEqual(resolved, link)
        self.assertTrue(resolved.is_symlink())

    def test_protected_git(self) -> None:
        git = self.root / ".git"
        git.mkdir()
        with self.assertRaises(ContainmentError) as caught:
            resolve_target(".git", cwd=self.root, workspace=self.ws)
        self.assertEqual(caught.exception.code, "PROTECTED_PATH")

    def test_workspace_root_itself_rejected(self) -> None:
        with self.assertRaises(ContainmentError):
            resolve_target(".", cwd=self.root, workspace=self.ws)

    def test_missing_path(self) -> None:
        with self.assertRaises(NotFoundError):
            resolve_target("missing.txt", cwd=self.root, workspace=self.ws)

    def test_missing_workspace(self) -> None:
        with self.assertRaises(NotFoundError):
            resolve_workspace(Path(self._tmp.name) / "nope")

    def test_describe(self) -> None:
        self.assertIsInstance(self.ws, Workspace)
