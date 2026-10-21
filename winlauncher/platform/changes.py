"""Platform-independent filesystem watch events."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FolderChange:
    """The folder's direct entries (and anything below) must be reconciled."""

    folder: Path


@dataclass(frozen=True, slots=True)
class FileChange:
    """Only this file's metadata must be refreshed."""

    path: Path


@dataclass(frozen=True, slots=True)
class WatchFailed:
    root: Path
    message: str


@dataclass(frozen=True, slots=True)
class WatchRestored:
    """A failed watch works again; changes during the outage need a root repair."""

    root: Path


WatchEvent = FolderChange | FileChange | WatchFailed | WatchRestored
