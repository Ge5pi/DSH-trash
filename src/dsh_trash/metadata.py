"""Metadata collection: stat fields, content digests, previews, manifests."""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from pathlib import Path
from typing import Any

from .errors import TrashIOError

MAX_PREVIEW_CHARS = 4096
PREVIEW_READ_BYTES = 16 * 1024
MANIFEST_MAX_ENTRIES = 100_000
MANIFEST_MAX_HASH_BYTES = 2 * 1024**3
CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Stream a file through SHA-256 in fixed-size chunks."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise TrashIOError(f"cannot read file for hashing: {path}: {exc}") from exc
    return digest.hexdigest()


def read_preview(path: Path, *, max_chars: int = MAX_PREVIEW_CHARS) -> tuple[str | None, bool]:
    """Return a bounded UTF-8 preview and whether it was truncated.

    Binary data (NUL bytes) and invalid UTF-8 yield ``None``: a preview is a
    convenience, never a reason to fail a trash operation.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(PREVIEW_READ_BYTES + 1)
    except OSError:
        return None, False
    if b"\x00" in raw:
        return None, False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, False
    truncated = len(raw) > PREVIEW_READ_BYTES or len(text) > max_chars
    return text[:max_chars], truncated


def birthtime_ns(st: os.stat_result) -> int | None:
    """Best-effort creation time; Linux ``os.stat`` does not expose statx btime."""
    value = getattr(st, "st_birthtime_ns", None)
    if value:
        return int(value)
    value = getattr(st, "st_birthtime", None)
    if value:
        return int(value * 1_000_000_000)
    return None


def _entry(st: os.stat_result, *, name: str, kind: str) -> dict[str, Any]:
    return {
        "path": name,
        "kind": kind,
        "size": int(st.st_size),
        "mode": stat_mod.S_IMODE(st.st_mode),
        "mtime_ns": int(st.st_mtime_ns),
    }


def _walk_manifest(root: Path, *, hash_files: bool) -> dict[str, Any]:
    """Build a bounded tree manifest without following symlinks."""
    entries: list[dict[str, Any]] = []
    totals = {"files": 0, "dirs": 0, "symlinks": 0, "bytes": 0}
    hashed_bytes = 0
    truncated = False

    def visit(directory: Path, prefix: str) -> None:
        nonlocal hashed_bytes, truncated
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise TrashIOError(f"cannot read directory for manifest: {directory}: {exc}") from exc
        for child in children:
            rel = f"{prefix}{child.name}"
            if len(entries) >= MANIFEST_MAX_ENTRIES:
                truncated = True
                return
            try:
                st = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise TrashIOError(f"cannot stat manifest entry: {child.path}: {exc}") from exc
            if child.is_symlink():
                item = _entry(st, name=rel, kind="symlink")
                try:
                    item["link_target"] = os.readlink(child.path)
                except OSError:
                    item["link_target"] = None
                totals["symlinks"] += 1
                entries.append(item)
            elif child.is_dir(follow_symlinks=False):
                item = _entry(st, name=rel, kind="dir")
                totals["dirs"] += 1
                entries.append(item)
                visit(Path(child.path), f"{rel}/")
            else:
                item = _entry(st, name=rel, kind="file")
                totals["files"] += 1
                totals["bytes"] += int(st.st_size)
                if hash_files and hashed_bytes + int(st.st_size) <= MANIFEST_MAX_HASH_BYTES:
                    item["sha256"] = sha256_file(Path(child.path))
                    hashed_bytes += int(st.st_size)
                else:
                    item["sha256"] = None
                    if hash_files:
                        truncated = True
                entries.append(item)

    visit(root, "")
    return {
        "version": 1,
        "truncated": truncated,
        "totals": totals,
        "entries": entries,
    }


def collect(path: Path, *, preview: bool = True, hash_files: bool = True) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Collect all metadata for a file, directory, or symlink.

    Returns ``(metadata, manifest)``; ``manifest`` is non-``None`` only for a
    directory. Raises ``TrashIOError`` for unsupported special files.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise TrashIOError(f"cannot stat path: {path}: {exc}") from exc

    if stat_mod.S_ISREG(st.st_mode):
        kind = "file"
    elif stat_mod.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat_mod.S_ISLNK(st.st_mode):
        kind = "symlink"
    else:
        raise TrashIOError(f"unsupported file type (only file, dir, symlink): {path}")

    meta: dict[str, Any] = {
        "kind": kind,
        "size": int(st.st_size),
        "mode": stat_mod.S_IMODE(st.st_mode),
        "uid": int(st.st_uid),
        "gid": int(st.st_gid),
        "mtime_ns": int(st.st_mtime_ns),
        "ctime_ns": int(st.st_ctime_ns),
        "birthtime_ns": birthtime_ns(st),
        "link_target": None,
        "sha256": None,
        "preview": None,
        "preview_truncated": False,
        "manifest": None,
    }

    manifest = None
    if kind == "file":
        if hash_files:
            meta["sha256"] = sha256_file(path)
        if preview:
            meta["preview"], meta["preview_truncated"] = read_preview(path)
    elif kind == "symlink":
        try:
            meta["link_target"] = os.readlink(path)
        except OSError:
            meta["link_target"] = None
        meta["size"] = len(meta["link_target"]) if meta["link_target"] else int(st.st_size)
    else:
        manifest = _walk_manifest(path, hash_files=hash_files)
        meta["size"] = int(manifest["totals"]["bytes"])
        meta["manifest"] = manifest

    return meta, manifest
