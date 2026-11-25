# dsh-trash

A reversible trash for workspace files in [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH).

`dsh-trash` is a CLI and a DSH plugin that move files into the system freedesktop trash, keep extended metadata in a SQLite sidecar index, and restore entries without mixing different workspaces. The repository directory is historically called `gio_trash`; the project and CLI are `dsh-trash`.

## Why

In `workspace-write` mode DSH only allows the agent to modify files inside the workspace. `rm` inside the workspace works, but it is irreversible, while moving a file to `~/.local/share/Trash` is a write outside the workspace that the sandbox blocks.

`dsh-trash` closes that gap: deletion becomes reversible and the sandbox is not weakened. The plugin runs the CLI as a trusted host process and re-checks the sandbox mode and the workspace boundary itself.

## Features

- **Files, directories, and symlinks** go to the trash as a whole; a symlink is not dereferenced.
- **Freedesktop compatibility**: the payload is stored in `Trash/files`, with a matching `Trash/info/*.trashinfo` holding the original path and deletion date.
- **Metadata**: workspace, absolute and relative path, filename, size, SHA-256, mtime/ctime, mode, owner, text preview, and a directory manifest.
- **Isolated restore**: `list`/`restore` only see entries of their own workspace, and the target always stays inside its root.
- **Conflict safety**: an existing file is never silently overwritten. `--force` is required, and the existing file is moved to the trash first.
- **Maintenance**: `doctor` reconciles the index with the trash, `empty` removes old entries.
- **Cross-filesystem support**: when the trash lives on another device, the CLI uses copy + fsync + unlink.
- **DSH tools**: `trash`, `trash_list`, and `trash_restore`, guarded by `sandboxPolicy`.

## Requirements

- Linux (v1).
- Python 3.11+.
- Node.js 20+ — only for the DSH plugin.

## Installation

```sh
python3 -m pip install -e .
dsh-trash version
```

The `dsh-trash` executable must be in `PATH`, because the plugin starts it by name. Override the path with `cliPath` in the plugin config.

Run from source without installing:

```sh
PYTHONPATH=src python3 -m dsh_trash version
```

## CLI

Every command accepts `--json` and prints a stable machine-readable contract. The workspace defaults to the current directory; run commands from the workspace root or pass `--workspace` explicitly.

```sh
# move to the trash
dsh-trash put ./dist --workspace . --reason "cleanup" --json

# what is in this workspace's trash
dsh-trash list --workspace .
dsh-trash show <id> --json

# restore an entry
dsh-trash restore <id> --workspace .

# restore somewhere else inside the workspace
dsh-trash restore <id> --workspace . --to build/dist

# replace an existing file (the old one goes to the trash)
dsh-trash restore <id> --workspace . --force

# reconcile and clean up
dsh-trash doctor --fix
dsh-trash empty --workspace . --older-than 30d --yes
```

### Commands

| Command | Description |
|---------|-------------|
| `put PATH` | Move a path to the trash and record metadata |
| `list` | List workspace entries (only `active` by default) |
| `show ID` | Print full metadata for one entry |
| `restore ID` | Restore an entry into the workspace |
| `empty` | Delete entries; without `--yes` it is a dry run |
| `doctor` | Reconcile the index, payloads, and `.trashinfo` files |
| `version` | Print version and storage paths |

Useful flags: `put --dry-run`, `put --no-preview`, `list --all`, `list --state`, `doctor --deep`, `empty --older-than`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 2 | invalid arguments |
| 3 | entry not found / wrong workspace |
| 4 | conflict |
| 5 | path outside the workspace or a protected path |
| 6 | filesystem or trash error |
| 7 | index error |

With `--json`, failures are printed as `{"error": {"code": "...", "message": "..."}}`.

## DSH plugin

```sh
dsh plugin --profile trash add ./dsh-plugin
dsh --profile trash
```

| Tool | Purpose |
|------|---------|
| `trash` | Move a workspace path to the trash and return its id |
| `trash_list` | List active entries of the current workspace |
| `trash_restore` | Restore an entry by id; `force` replaces an existing file |

Behavior:

- in `read-only` mode the tools refuse to run;
- in `workspace-write` mode only paths inside `workspaceRoot` are accepted;
- `.git` is protected;
- the CLI performs the write to `Trash` as a trusted host process, not through `ctx.fs`, so the sandbox is not weakened;
- `empty` is intentionally not exposed to the model — clearing the trash remains a human decision.

Default config (`dsh-plugin/cordis.patch.yml`):

```yaml
cliPath: dsh-trash
timeoutMs: 20000
preview: true
```

## How it works

```
model → DSH plugin → dsh-trash CLI → Trash/files + Trash/info
                                          │
                                   SQLite index and manifests
```

- **Trash**: `$XDG_DATA_HOME/Trash` (default `~/.local/share/Trash`).
- **Index**: `$XDG_DATA_HOME/dsh-trash/index.sqlite3`, mode 0600; the directory is 0700.
- **Directory manifests**: `$XDG_DATA_HOME/dsh-trash/manifests/<id>.json`.
- **Workspace identity** is the `(st_dev, st_ino)` pair of the root, so renaming the directory keeps the same workspace id.
- **Restore** uses the relative path from the current workspace root, not a stale absolute path.
- **Entry states**: `pending → active → restored`, plus `missing` and `failed` for diagnostics.

Detailed architecture, data model, and design rationale live in [PLAN.md](PLAN.md).

## Limitations

- v1 supports Linux only. Topdir trash for separate mounts, Windows, and macOS are planned for v1.1.
- `birthtime` is `NULL` on Linux for now: `os.stat` does not expose statx btime.
- The trash is shared with other applications. If it is emptied externally, `doctor` reports `missing` and the payload cannot be recovered from the index.
- One `trash` call handles one path; bulk cleanup means several calls.
- There is no automatic retention policy, only manual `empty`.
- Text previews are enabled by default; use `--no-preview` for sensitive files.
- The DSH bundle is implemented and its helpers are tested, but it has not yet been exercised end-to-end in a live DSH.

## Testing

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
cd dsh-plugin && node --test test/*.test.js
```

Currently: 60 Python tests and 6 plugin helper tests.

## Layout

```
src/dsh_trash/     Python CLI: workspace, metadata, trashdir, index, engine, cli
tests/             unittest: put, restore, maintenance, workspace isolation
dsh-plugin/        DSH bundle: index.js, helpers.js, cordis.patch.yml
PLAN.md            architecture, contracts, security, and v1.1 stages
```

## License

MIT — see [LICENSE](LICENSE).
