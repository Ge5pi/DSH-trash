"""``dsh-trash`` command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .engine import TrashEngine
from .errors import TrashError, TrashIOError
from .trashdir import TrashPaths
from .workspace import resolve_workspace


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def _human_put(result: dict[str, Any]) -> str:
    if result.get("dry_run"):
        return f"Would trash {result['relpath']} ({result['kind']}, {result['size']} bytes)"
    return f"Trashed {result['relpath']} ({result['kind']}, {result['size']} bytes) as {result['id']}"


def _human_version(info: dict[str, Any]) -> str:
    return (
        f"dsh-trash {info['version']} ({info['platform']}/{info['backend']})\n"
        f"data:  {info['data_dir']}\n"
        f"trash: {info['trash_dir']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-trash",
        description="Reversible, workspace-scoped trash for DSH and humans.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    put = sub.add_parser("put", help="move a file or directory to the trash")
    put.add_argument("path", help="path to trash (relative to cwd or absolute)")
    put.add_argument("--workspace", help="workspace root (default: current directory)")
    put.add_argument("--reason", help="free-form reason recorded in metadata")
    put.add_argument("--dry-run", action="store_true", help="validate and print metadata without moving")
    put.add_argument("--no-preview", action="store_true", help="do not store a text preview")
    put.add_argument("--json", action="store_true", help="machine-readable output")

    list_cmd = sub.add_parser("list", help="list trash entries")
    list_cmd.add_argument("--workspace", help="workspace root (default: current directory)")
    list_cmd.add_argument("--all", action="store_true", help="do not filter by workspace")
    list_cmd.add_argument("--limit", type=int, default=100, help="maximum entries (default 100)")
    list_cmd.add_argument(
        "--state",
        default="active",
        choices=("active", "restored", "missing", "failed", "pending", "all"),
        help="entry state filter (default active)",
    )
    list_cmd.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show full metadata for one entry")
    show.add_argument("id")
    show.add_argument("--workspace", help="require the entry to belong to this workspace")
    show.add_argument("--json", action="store_true")

    restore = sub.add_parser("restore", help="restore an entry into the workspace")
    restore.add_argument("id")
    restore.add_argument("--workspace", help="workspace root (default: current directory)")
    restore.add_argument("--to", help="target path inside the workspace")
    restore.add_argument("--force", action="store_true", help="trash an existing target and replace it")
    restore.add_argument("--json", action="store_true")

    empty = sub.add_parser("empty", help="delete trashed entries")
    empty.add_argument("--workspace", help="workspace root (default: current directory)")
    empty.add_argument("--older-than", help="only entries older than e.g. 30d, 12h, 10m")
    empty.add_argument("--yes", action="store_true", help="actually delete (otherwise dry-run)")
    empty.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="reconcile the index with the trash directory")
    doctor.add_argument("--fix", action="store_true", help="mark missing payloads as missing")
    doctor.add_argument("--deep", action="store_true", help="also verify SHA-256 of files")
    doctor.add_argument("--json", action="store_true")

    version = sub.add_parser("version", help="print version and storage paths")
    version.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        if args.command == "version":
            paths = TrashPaths.create()
            backend = "freedesktop" if paths.freedesktop else (
                "macos" if paths.platform == "darwin" else "portable"
            )
            info = {
                "version": __version__,
                "platform": paths.platform,
                "backend": backend,
                "data_dir": str(paths.state),
                "trash_dir": str(paths.trash),
            }
            _emit(info if as_json else _human_version(info), as_json=as_json)
            return 0

        if args.command == "put":
            workspace_path = args.workspace or Path.cwd()
            workspace = resolve_workspace(workspace_path)
            engine = TrashEngine()
            result = engine.put(
                args.path,
                workspace,
                reason=args.reason,
                preview=not args.no_preview,
                dry_run=args.dry_run,
            )
            _emit(result if as_json else _human_put(result), as_json=as_json)
            return 0

        if args.command == "list":
            engine = TrashEngine()
            workspace = resolve_workspace(args.workspace or Path.cwd())
            state = None if args.state == "all" else args.state
            entries = engine.list(
                workspace,
                limit=args.limit,
                state=state,
                all_workspaces=args.all,
            )
            if as_json:
                _emit({"entries": entries, "total": len(entries)}, as_json=True)
            elif not entries:
                print("No trash entries.")
            else:
                for entry in entries:
                    print(
                        f"{entry['id']}  {entry['state']:<8}  {entry['size']:>10}  "
                        f"{entry['kind']:<7}  {entry['trashed_at']}  {entry['relpath']}"
                    )
            return 0

        if args.command == "show":
            engine = TrashEngine()
            workspace = resolve_workspace(args.workspace) if args.workspace else None
            entry = engine.show(args.id, workspace)
            _emit(entry if as_json else json.dumps(entry, ensure_ascii=False, indent=2), as_json=as_json)
            return 0

        if args.command == "restore":
            engine = TrashEngine()
            workspace = resolve_workspace(args.workspace or Path.cwd())
            result = engine.restore(args.id, workspace, force=args.force, to=args.to)
            if as_json:
                _emit(result, as_json=True)
            else:
                print(f"Restored {result['relpath']} to {result['path']}")
            return 0

        if args.command == "empty":
            engine = TrashEngine()
            workspace = resolve_workspace(args.workspace or Path.cwd())
            result = engine.empty(
                workspace,
                older_than=args.older_than,
                dry_run=not args.yes,
            )
            if as_json:
                _emit(result, as_json=True)
            elif result["dry_run"]:
                print(f"Would delete {result['deleted']} entries ({result['freed_bytes']} bytes); pass --yes")
            else:
                print(f"Deleted {result['deleted']} entries ({result['freed_bytes']} bytes)")
            return 0

        if args.command == "doctor":
            engine = TrashEngine()
            result = engine.doctor(fix=args.fix, deep=args.deep)
            if as_json:
                _emit(result, as_json=True)
            elif not result["issues"]:
                print(f"No issues ({result['checked']} entries checked).")
            else:
                for issue in result["issues"]:
                    print(f"{issue['kind']}: {json.dumps(issue, ensure_ascii=False)}")
            return 0

        raise TrashError(f"unknown command: {args.command}")
    except TrashError as exc:
        if as_json:
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"dsh-trash: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        error = TrashIOError(f"{exc.strerror or exc}: {exc.filename or ''}".strip())
        if as_json:
            print(json.dumps(error.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"dsh-trash: {error.message}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
