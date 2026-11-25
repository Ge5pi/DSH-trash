"""Platform selection and default storage locations.

Linux keeps the freedesktop layout. macOS uses the user's real ``~/.Trash``.
Windows currently uses a DSH-managed portable trash (native Recycle Bin
integration is tracked separately), with no ``.trashinfo`` files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SUPPORTED = ("linux", "darwin", "windows")


def system() -> str:
    """Return the platform name, with a test-only override."""
    override = os.environ.get("DSH_TRASH_PLATFORM")
    if override in SUPPORTED:
        return override
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def default_data_home(platform: str | None = None, home: Path | None = None) -> Path:
    """Return the per-user base directory for state (and, on Linux, the trash)."""
    platform = platform or system()
    home = home or Path.home()
    if platform == "windows":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) if base else home / "AppData" / "Local"
    if platform == "darwin":
        return home / "Library" / "Application Support"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else home / ".local" / "share"


def is_freedesktop(platform: str | None = None) -> bool:
    return (platform or system()) == "linux"
