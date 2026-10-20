"""Paths beyond Windows' 260-character MAX_PATH limit.

Unless long paths are enabled system-wide, Windows rejects longer paths with
"The system cannot find the path specified" (WinError 3). The extended-length
form `\\\\?\\C:\\...` (or `\\\\?\\UNC\\server\\share\\...`) works everywhere. PathScout
uses it only for its own file-system calls; catalog entries and the UI keep the
ordinary path.
"""

from __future__ import annotations

import os
from pathlib import Path

# A folder listing appends `\*` and Windows reserves room for an 8.3 name, so
# switch well before 260 characters.
LONG_PATH_THRESHOLD = 240
_PREFIX = "\\\\?\\"
_UNC_PREFIX = _PREFIX + "UNC\\"


def os_path(path: Path | str) -> str:
    """`path` for `os` calls: extended-length form when it is long on Windows."""
    text = os.fspath(path)
    if os.name != "nt" or len(text) < LONG_PATH_THRESHOLD or text.startswith(_PREFIX):
        return text
    # Extended-length paths are used verbatim: no `/`, `.` or `..` components.
    text = os.path.normpath(text)
    if text.startswith("\\\\"):
        return _UNC_PREFIX + text[2:]
    return _PREFIX + text


def ordinary_path(text: str) -> Path:
    """The path as users and the catalog see it, without an extended-length prefix."""
    if text.startswith(_UNC_PREFIX):
        return Path("\\\\" + text[len(_UNC_PREFIX) :])
    if text.startswith(_PREFIX):
        return Path(text[len(_PREFIX) :])
    return Path(text)


def stat(path: Path | str) -> os.stat_result:
    return os.stat(os_path(path))


def lstat(path: Path | str) -> os.stat_result:
    return os.lstat(os_path(path))


def scandir(path: Path | str):
    """Entries keep the extended form in `entry.path`; use `ordinary_path` on it."""
    return os.scandir(os_path(path))
