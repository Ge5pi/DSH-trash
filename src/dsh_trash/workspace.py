"""Workspace identity and path containment.

A workspace is identified by the ``(st_dev, st_ino)`` pair of its canonical
root, so renaming the directory in place does not change its identity. All
mutating operations must land inside that root; the final path component is
never resolved through a symlink, because a symlink itself is a valid trash
target.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ContainmentError, NotFoundError, UsageError

PROTECTED_NAMES = frozenset({".git"})


@dataclass(frozen=True)
class Workspace:
    """A canonical workspace root and its stable filesystem identity."""

    root: Path
    dev: int
    ino: int

    @property
    def id(self) -> str:
        return hashlib.sha256(f"{self.dev}:{self.ino}".encode()).hexdigest()[:16]


def resolve_workspace(path: str | os.PathLike[str]) -> Workspace:
    """Canonicalize an existing workspace directory.

    Raises ``NotFoundError`` when the path does not exist and ``UsageError``
    when it is not a directory.
    """
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError(f"workspace does not exist: {path}") from exc
    if not root.is_dir():
        raise UsageError(f"workspace is not a directory: {root}")
    st = root.stat()
    return Workspace(root=root, dev=st.st_dev, ino=st.st_ino)


def is_within(root: Path, target: Path) -> bool:
    """True when *target* is strictly below *root* (both absolute)."""
    try:
        return os.path.commonpath([str(root), str(target)]) == str(root) and target != root
    except ValueError:  # different drives on Windows
        return False


def resolve_target(
    path: str | os.PathLike[str],
    *,
    cwd: Path,
    workspace: Workspace,
    protected_names: frozenset[str] = PROTECTED_NAMES,
) -> Path:
    """Resolve a user path to a mutation target inside *workspace*.

    The parent directory is canonicalized; the final component is kept as-is
    and inspected with ``lstat`` so a symlink is trashed as a link rather than
    followed to its target.
    """
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    if raw.name in ("", ".", ".."):
        raise UsageError(f"invalid target name: {path}")
    try:
        parent = raw.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise NotFoundError(f"parent directory does not exist: {raw.parent}") from exc
    if not parent.is_dir():
        raise UsageError(f"parent is not a directory: {parent}")
    target = parent / raw.name

    if not is_within(workspace.root, target):
        raise ContainmentError(
            f"path is outside the session workspace: {target}",
            details={"workspace": str(workspace.root), "path": str(target)},
        )
    if target.name in protected_names:
        raise ContainmentError(
            f"refusing to trash protected path: {target.name}",
            code="PROTECTED_PATH",
        )
    try:
        os.lstat(target)
    except FileNotFoundError as exc:
        raise NotFoundError(f"path does not exist: {target}") from exc
    return target


def describe(workspace: Workspace) -> dict[str, str | int]:
    return {"id": workspace.id, "root": str(workspace.root), "dev": workspace.dev, "ino": workspace.ino}
