"""Freedesktop trash layout, locking, payload moves, and ``.trashinfo`` files."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

try:  # POSIX advisory locks
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # Windows byte-range locks
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

from .errors import TrashError, TrashIOError
from .platforms import default_data_home, is_freedesktop, system


@dataclass(frozen=True)
class TrashPaths:
    """All directories the CLI uses for one user on one platform."""

    data_home: Path
    platform: str
    home: Path

    @classmethod
    def create(
        cls,
        data_home: str | os.PathLike[str] | None = None,
        *,
        platform: str | None = None,
        home: str | os.PathLike[str] | None = None,
    ) -> "TrashPaths":
        platform = platform or system()
        home_path = Path(home).expanduser() if home is not None else Path.home()
        base = (
            Path(data_home).expanduser()
            if data_home is not None
            else default_data_home(platform, home_path)
        )
        return cls(data_home=base, platform=platform, home=home_path)

    @property
    def trash(self) -> Path:
        if self.platform == "darwin":
            return self.home / ".Trash"
        if self.platform == "windows":
            return self.state / "Trash"
        return self.data_home / "Trash"

    @property
    def files(self) -> Path:
        return self.trash if self.platform == "darwin" else self.trash / "files"

    @property
    def info(self) -> Path | None:
        return self.trash / "info" if self.platform == "linux" else None

    @property
    def freedesktop(self) -> bool:
        return is_freedesktop(self.platform)

    @property
    def state(self) -> Path:
        return self.data_home / "dsh-trash"

    @property
    def manifests(self) -> Path:
        return self.state / "manifests"

    @property
    def index(self) -> Path:
        return self.state / "index.sqlite3"

    @property
    def lock(self) -> Path:
        return self.state / "lock"

    def ensure(self) -> None:
        """Create the directory layout with private permissions."""
        directories = [self.files, self.manifests]
        if self.info is not None:
            directories.append(self.info)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state.chmod(0o700)
        except OSError:
            pass


def fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(lock_path: Path, timeout: float = 10.0):
    """Hold an exclusive inter-process lock for one trash mutation."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if fcntl is None and os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TrashError(
                        f"timed out waiting for trash lock: {lock_path}",
                        code="LOCK_TIMEOUT",
                    )
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _split_name(name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(name)
    if not stem:
        return name, ""
    return stem, ext


def reserve_name(files: Path, info: Path | None, basename: str, *, limit: int = 10_000) -> str:
    """Pick a payload name that collides with neither an existing payload nor its info."""
    if basename in ("", ".", ".."):
        basename = "file"
    stem, ext = _split_name(basename)
    candidate = basename
    counter = 0
    while (files / candidate).exists() or (
        info is not None and (info / f"{candidate}.trashinfo").exists()
    ):
        counter += 1
        if counter > limit:
            raise TrashIOError(f"cannot allocate a free trash name for: {basename}")
        candidate = f"{stem}.{counter}{ext}"
    return candidate


def write_trashinfo(info_dir: Path, name: str, original: Path, when: datetime | None = None) -> Path:
    """Write ``info/<name>.trashinfo`` atomically with 0600 permissions."""
    when = when or datetime.now().astimezone()
    encoded = quote(str(original), safe="/")
    payload = (
        "[Trash Info]\n"
        f"Path={encoded}\n"
        f"DeletionDate={when.strftime('%Y-%m-%dT%H:%M:%S')}\n"
    ).encode("utf-8")
    final = info_dir / f"{name}.trashinfo"
    tmp = info_dir / f".tmp-{os.getpid()}-{time.time_ns()}"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        os.chmod(final, 0o600)
        fsync_dir(info_dir)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise TrashIOError(f"cannot write trash info file: {final}: {exc}") from exc
    return final


def _copy_any(src: Path, dst: Path) -> None:
    st = os.lstat(src)
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), dst)
    elif stat.S_ISDIR(st.st_mode):
        shutil.copytree(src, dst, symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def _remove_any(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def move_path(src: Path, dst: Path) -> None:
    """Move *src* to *dst*, falling back to copy+fsync+unlink across devices.

    Windows and cross-device renames fail with ``EXDEV``/``winerror 17``; the
    fallback also covers filesystems that refuse an in-place rename.
    """
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass

    tmp = dst.parent / f".partial-{os.getpid()}-{time.time_ns()}"
    try:
        _copy_any(src, tmp)
        if tmp.is_file() and not tmp.is_symlink():
            with tmp.open("rb") as handle:
                os.fsync(handle.fileno())
        os.replace(tmp, dst)
        fsync_dir(dst.parent)
    except OSError as exc:
        with contextlib.suppress(OSError):
            _remove_any(tmp)
        raise TrashIOError(f"cannot copy to trash: {src} -> {dst}: {exc}") from exc
    try:
        _remove_any(src)
    except OSError as exc:
        raise TrashIOError(f"copied to trash but cannot remove source: {src}: {exc}") from exc


def restore_payload(payload: Path, target: Path) -> None:
    """Move a payload back; caller has already validated the target."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    move_path(payload, target)


def remove_trashinfo(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise TrashIOError(f"cannot remove trash info file: {path}: {exc}") from exc

