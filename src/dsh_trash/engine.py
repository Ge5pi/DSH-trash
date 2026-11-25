"""Trash engine: ``put``, ``restore``, listing, and crash-safe transitions."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from .errors import ConflictError, ContainmentError, NotFoundError, TrashError, TrashIOError
from .index import Index, utc_now
from .metadata import collect, sha256_file
from .trashdir import (
    TrashPaths,
    locked,
    move_path,
    remove_trashinfo,
    reserve_name,
    write_trashinfo,
)
from .workspace import Workspace, is_within, resolve_target

_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SUMMARY_FIELDS = ("id", "relpath", "kind", "size", "trashed_at", "state", "sha256")


def new_id(now_ms: int | None = None) -> str:
    """Generate a lexicographically sortable, dependency-free trash id."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = ((ms & ((1 << 41) - 1)) << 39) | secrets.randbits(39)
    chars: list[str] = []
    for _ in range(16):
        chars.append(_ID_ALPHABET[value & 0x1F])
        value >>= 5
    return "t_" + "".join(reversed(chars))


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Write a directory manifest atomically."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.parent / f".tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise TrashIOError(f"cannot write manifest: {path}: {exc}") from exc


def summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in SUMMARY_FIELDS if key in entry}


class TrashEngine:
    """One filesystem trash store rooted at a given ``XDG_DATA_HOME``."""

    def __init__(self, data_home: str | os.PathLike[str] | None = None) -> None:
        self.paths = TrashPaths.create(data_home)

    # -- put ---------------------------------------------------------

    def put(
        self,
        path: str | os.PathLike[str],
        workspace: Workspace,
        *,
        cwd: Path | None = None,
        reason: str | None = None,
        preview: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        target = resolve_target(path, cwd=cwd or Path.cwd(), workspace=workspace)
        relpath = target.relative_to(workspace.root).as_posix()
        meta, manifest = collect(target, preview=preview)

        if dry_run:
            return {
                "dry_run": True,
                "workspace": str(workspace.root),
                "path": str(target),
                "relpath": relpath,
                "kind": meta["kind"],
                "size": meta["size"],
                "sha256": meta["sha256"],
            }

        self.paths.ensure()
        with locked(self.paths.lock):
            with Index(self.paths.index) as index:
                return self._put_locked(
                    target,
                    workspace,
                    index,
                    relpath=relpath,
                    meta=meta,
                    manifest=manifest,
                    reason=reason,
                )

    def _put_locked(
        self,
        target: Path,
        workspace: Workspace,
        index: Index,
        *,
        relpath: str,
        meta: dict[str, Any],
        manifest: dict[str, Any] | None,
        reason: str | None,
    ) -> dict[str, Any]:
        entry_id = new_id()
        trashed_at = utc_now()
        manifest_path = None
        if manifest is not None:
            manifest_file = self.paths.manifests / f"{entry_id}.json"
            write_manifest(manifest_file, manifest)
            manifest_path = str(manifest_file)

        pending: dict[str, Any] = {
            "id": entry_id,
            "workspace_id": "",
            "original_path": str(target),
            "relpath": relpath,
            "filename": target.name,
            "kind": meta["kind"],
            "size": meta["size"],
            "mode": meta["mode"],
            "uid": meta["uid"],
            "gid": meta["gid"],
            "mtime_ns": meta["mtime_ns"],
            "ctime_ns": meta["ctime_ns"],
            "birthtime_ns": meta["birthtime_ns"],
            "link_target": meta["link_target"],
            "sha256": meta["sha256"],
            "preview": meta["preview"],
            "manifest_path": manifest_path,
            "trash_dir": str(self.paths.trash),
            "payload_name": "",
            "payload_path": "",
            "trashinfo_path": "",
            "trashed_at": trashed_at,
            "reason": reason or None,
            "state": "pending",
        }

        payload_name = ""
        moved = False
        info_path: Path | None = None
        try:
            pending["workspace_id"] = index.ensure_workspace(workspace)
            index.insert_pending(pending)
            payload_name = reserve_name(self.paths.files, self.paths.info, target.name)
            payload = self.paths.files / payload_name
            move_path(target, payload)
            moved = True
            info_path = write_trashinfo(self.paths.info, payload_name, target)
            index.mark_active(
                entry_id,
                payload_name=payload_name,
                payload_path=str(payload),
                trashinfo_path=str(info_path),
            )
        except Exception as exc:
            message = getattr(exc, "message", str(exc))
            if moved:
                self._rollback(target, self.paths.files / payload_name, info_path, manifest_path)
            elif manifest_path is not None:
                with contextlib.suppress(OSError):
                    Path(manifest_path).unlink()
            with contextlib.suppress(Exception):
                index.mark_failed(entry_id, message)
            raise

        return {
            "id": entry_id,
            "workspace": str(workspace.root),
            "path": str(target),
            "relpath": relpath,
            "kind": meta["kind"],
            "size": meta["size"],
            "sha256": meta["sha256"],
            "trashed_at": trashed_at,
            "payload_name": payload_name,
        }

    def _rollback(self, target: Path, payload: Path, info_path: Path | None, manifest_path: str | None) -> None:
        """Best-effort undo after a failed put; never mask the original error."""
        if info_path is not None:
            with contextlib.suppress(OSError):
                info_path.unlink()
        if payload.exists() or payload.is_symlink():
            with contextlib.suppress(Exception):
                move_path(payload, target)
        if manifest_path is not None:
            with contextlib.suppress(OSError):
                Path(manifest_path).unlink()

    # -- restore -----------------------------------------------------

    def restore(
        self,
        entry_id: str,
        workspace: Workspace,
        *,
        force: bool = False,
        to: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        self.paths.ensure()
        with locked(self.paths.lock):
            with Index(self.paths.index) as index:
                workspace_id = index.ensure_workspace(workspace)
                entry = index.find_in_workspace(entry_id, workspace_id)
                if entry is None:
                    raise NotFoundError(f"trash entry not found in this workspace: {entry_id}")
                if entry["state"] != "active":
                    raise ConflictError(
                        f"trash entry is not active (state={entry['state']}): {entry_id}"
                    )
                payload = Path(entry["payload_path"])
                if not (payload.exists() or payload.is_symlink()):
                    index.mark_missing(entry_id)
                    raise NotFoundError(f"trash payload is missing: {entry_id}")

                if entry["sha256"] and payload.is_file() and not payload.is_symlink():
                    actual = sha256_file(payload)
                    if actual != entry["sha256"]:
                        raise TrashIOError(
                            f"trash payload checksum mismatch: {entry_id}",
                            code="DIGEST_MISMATCH",
                        )

                target = self._restore_target(workspace, entry, to)
                if target.exists() or target.is_symlink():
                    if not force:
                        raise ConflictError(
                            f"target already exists: {target}; pass --force to replace it",
                            details={"path": str(target)},
                        )
                    replaced_meta, replaced_manifest = collect(target)
                    self._put_locked(
                        target,
                        workspace,
                        index,
                        relpath=target.relative_to(workspace.root).as_posix(),
                        meta=replaced_meta,
                        manifest=replaced_manifest,
                        reason="replaced-by-restore",
                    )

                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                move_path(payload, target)
                with contextlib.suppress(TrashError):
                    remove_trashinfo(Path(entry["trashinfo_path"]))
                restored_at = utc_now()
                restored_sha = entry["sha256"]
                index.mark_restored(
                    entry_id,
                    restored_at=restored_at,
                    target=str(target),
                    sha256=restored_sha,
                )

        return {
            "id": entry_id,
            "workspace": str(workspace.root),
            "path": str(target),
            "relpath": target.relative_to(workspace.root).as_posix(),
            "sha256": restored_sha,
            "restored_at": restored_at,
        }

    def _restore_target(
        self,
        workspace: Workspace,
        entry: dict[str, Any],
        to: str | os.PathLike[str] | None,
    ) -> Path:
        if to is None:
            raw = workspace.root / entry["relpath"]
        else:
            raw = Path(to).expanduser()
            if not raw.is_absolute():
                raw = workspace.root / raw
            raw = Path(os.path.abspath(raw))
        # Resolve existing symlinked ancestors while allowing missing leaf dirs.
        parent = Path(os.path.realpath(raw.parent))
        target = parent / raw.name
        if target == workspace.root or not is_within(workspace.root, target):
            raise ContainmentError(
                f"restore target is outside the session workspace: {target}",
                details={"workspace": str(workspace.root), "path": str(target)},
            )
        if target.name in (".git",):
            raise ContainmentError(f"refusing to restore over protected path: {target.name}", code="PROTECTED_PATH")
        return target

    # -- read --------------------------------------------------------

    def list(
        self,
        workspace: Workspace,
        *,
        limit: int = 100,
        state: str | None = "active",
        all_workspaces: bool = False,
    ) -> list[dict[str, Any]]:
        with Index(self.paths.index) as index:
            workspace_id = index.ensure_workspace(workspace)
            entries = index.list_entries(
                None if all_workspaces else workspace_id,
                limit=limit,
                state=state,
            )
            return [summary(entry) for entry in entries]

    def show(self, entry_id: str, workspace: Workspace | None = None) -> dict[str, Any]:
        with Index(self.paths.index) as index:
            if workspace is not None:
                entry = index.find_in_workspace(entry_id, index.ensure_workspace(workspace))
            else:
                entry = index.get(entry_id)
        if entry is None:
            raise NotFoundError(f"trash entry not found: {entry_id}")
        return entry

    # -- maintenance -------------------------------------------------

    def empty(
        self,
        workspace: Workspace,
        *,
        older_than: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete active entries for one workspace, optionally older than a duration."""
        cutoff = None if older_than is None else _cutoff(older_than)
        self.paths.ensure()
        deleted = 0
        freed = 0
        with locked(self.paths.lock):
            with Index(self.paths.index) as index:
                workspace_id = index.ensure_workspace(workspace)
                entries = index.list_entries(workspace_id, limit=1_000_000, state="active")
                for entry in entries:
                    if cutoff is not None and entry["trashed_at"] >= cutoff:
                        continue
                    deleted += 1
                    freed += int(entry["size"] or 0)
                    if dry_run:
                        continue
                    payload = Path(entry["payload_path"])
                    if payload.is_dir() and not payload.is_symlink():
                        shutil.rmtree(payload, ignore_errors=True)
                    else:
                        with contextlib.suppress(OSError):
                            payload.unlink()
                    with contextlib.suppress(OSError):
                        Path(entry["trashinfo_path"]).unlink()
                    if entry["manifest_path"]:
                        with contextlib.suppress(OSError):
                            Path(entry["manifest_path"]).unlink()
                    index.delete(entry["id"])
        return {"deleted": deleted, "freed_bytes": freed, "dry_run": dry_run}

    def doctor(self, *, fix: bool = False, deep: bool = False) -> dict[str, Any]:
        """Reconcile the index with the payloads and info files on disk."""
        self.paths.ensure()
        issues: list[dict[str, Any]] = []
        with Index(self.paths.index) as index:
            entries = index.all_entries()
            known_info = {entry["trashinfo_path"] for entry in entries if entry["trashinfo_path"]}
            for entry in entries:
                entry_id = entry["id"]
                if entry["state"] == "pending":
                    issues.append({"kind": "pending", "id": entry_id, "message": "put did not finish"})
                    continue
                if entry["state"] != "active":
                    continue
                payload = Path(entry["payload_path"])
                if not (payload.exists() or payload.is_symlink()):
                    issues.append({"kind": "missing_payload", "id": entry_id, "path": str(payload)})
                    if fix:
                        index.mark_missing(entry_id)
                    continue
                info = Path(entry["trashinfo_path"])
                if entry["trashinfo_path"] and not info.exists():
                    issues.append({"kind": "missing_trashinfo", "id": entry_id, "path": str(info)})
                if entry["kind"] == "file" and not payload.is_symlink():
                    size = payload.stat().st_size
                    if size != entry["size"]:
                        issues.append(
                            {
                                "kind": "size_mismatch",
                                "id": entry_id,
                                "expected": entry["size"],
                                "actual": size,
                            }
                        )
                    elif deep and entry["sha256"]:
                        if sha256_file(payload) != entry["sha256"]:
                            issues.append({"kind": "digest_mismatch", "id": entry_id})
            for info in sorted(self.paths.info.glob("*.trashinfo")):
                if str(info) not in known_info:
                    issues.append({"kind": "orphan_trashinfo", "path": str(info)})
        return {"issues": issues, "checked": len(entries)}


def _cutoff(older_than: str) -> str:
    """Parse ``30d``/``12h``/``10m``/``45s`` into a UTC ISO cutoff."""
    import re
    from datetime import datetime, timedelta, timezone

    match = re.fullmatch(r"(\d+)([smhd])", older_than.strip())
    if match is None:
        raise TrashError(
            f"invalid --older-than duration: {older_than} (expected e.g. 30d, 12h, 10m)",
            code="INVALID_ARGUMENT",
            exit_code=2,
        )
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    value = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
