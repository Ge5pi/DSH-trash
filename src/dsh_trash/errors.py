"""Structured CLI errors with stable exit codes.

The CLI is consumed by the DSH plugin and by humans; both need a stable
machine-readable code rather than a Python traceback.
"""

from __future__ import annotations

from typing import Any


class TrashError(Exception):
    """Base error carrying a stable code and process exit status."""

    code = "TRASH_ERROR"
    exit_code = 6

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        exit_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class UsageError(TrashError):
    code = "INVALID_ARGUMENT"
    exit_code = 2


class NotFoundError(TrashError):
    code = "NOT_FOUND"
    exit_code = 3


class ConflictError(TrashError):
    code = "CONFLICT"
    exit_code = 4


class ContainmentError(TrashError):
    code = "OUTSIDE_WORKSPACE"
    exit_code = 5


class TrashIOError(TrashError):
    code = "TRASH_IO_ERROR"
    exit_code = 6


class IndexError(TrashError):
    code = "INDEX_ERROR"
    exit_code = 7
