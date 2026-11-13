"""Windows known folders under the names `%NAME%` and Explorer's `shell:` take.

Windows and installed programs register every known folder below
`FolderDescriptions` with a name, such as `Startup` or `Common Start Menu`;
`shell:Startup` in Explorer or the Run dialog opens it. Most are folders on disk
that `SHGetKnownFolderPath` locates, following redirection. Virtual ones, such
as the Recycle Bin or Network Connections, have no path: Explorer opens them by
their `shell:` address only, so WinLauncher reaches them through shortcuts that
`windows_tools` writes.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# Return the location without checking that it exists: a folder redirected to a
# server that does not answer would otherwise block. Navigation checks it safely.
KF_FLAG_DONT_VERIFY = 0x4000
FOLDER_DESCRIPTIONS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FolderDescriptions"
# Known folders (FOLDERID_ names) offered as %NAME% before the registered names,
# which call some of them differently, such as `Personal` for Documents.
VARIABLE_FOLDERS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Pictures",
    "Music",
    "Videos",
    "Startup",
    "CommonStartup",
    "SendTo",
    "Recent",
    "Templates",
    "StartMenu",
    "Programs",
    "SavedGames",
    "Favorites",
    "Links",
    "LocalAppDataLow",
    "UserProgramFiles",
    "QuickLaunch",
    "Fonts",
    "PublicDesktop",
    "PublicDocuments",
)
logger = logging.getLogger(__name__)


class FolderCategory(IntEnum):
    """How Windows stores a known folder (`KF_CATEGORY`)."""

    VIRTUAL = 1
    FIXED = 2
    COMMON = 3
    PER_USER = 4


@dataclass(frozen=True, slots=True)
class KnownFolder:
    """One registered known folder; `name` is what `shell:` takes."""

    name: str
    folder_id: str  # Its KNOWNFOLDERID, `{…}`.
    category: FolderCategory

    @property
    def virtual(self) -> bool:
        return self.category == FolderCategory.VIRTUAL


@functools.cache
def variable_folders() -> dict[str, Path]:
    """Folders `%NAME%` reaches beside environment variables, by uppercase name.

    The curated FOLDERID names come first, then every registered folder without
    the spaces in its name (`Common Desktop` as COMMONDESKTOP). Navigation shows
    one name per folder, the first, so the familiar names win.
    """
    named = {name.upper(): path for name in VARIABLE_FOLDERS if (path := known_folder(name))}
    for folder, path in _folder_paths().items():
        named.setdefault(folder.name.replace(" ", "").upper(), path)
    return named


def registered_folders() -> tuple[KnownFolder, ...]:
    """Every known folder registered on this computer, in registry order."""
    if os.name != "nt":
        return ()
    import winreg

    folders = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, FOLDER_DESCRIPTIONS) as descriptions:
        for index in range(winreg.QueryInfoKey(descriptions)[0]):
            folder_id = winreg.EnumKey(descriptions, index)
            if (folder := _read_folder(descriptions, folder_id)) is not None:
                folders.append(folder)
    return tuple(folders)


def _read_folder(descriptions: object, folder_id: str) -> KnownFolder | None:
    """The folder registered as `folder_id`; None when its entry is incomplete."""
    import winreg

    try:
        with winreg.OpenKey(descriptions, folder_id) as entry:
            name = winreg.QueryValueEx(entry, "Name")[0]
            category = FolderCategory(winreg.QueryValueEx(entry, "Category")[0])
    except (OSError, ValueError):
        return None
    return KnownFolder(name, folder_id, category) if isinstance(name, str) and name else None


@functools.cache
def _folder_paths() -> dict[KnownFolder, Path]:
    """The location of each registered folder that is not virtual and has one now."""
    try:
        folders = registered_folders()
    except OSError:
        logger.warning("Could not read the registered known folders", exc_info=True)
        return {}
    return {
        folder: path
        for folder in folders
        if not folder.virtual and (path := _folder_path(folder)) is not None
    }


def known_folder(name: str) -> Path | None:
    """The current location of a known folder by FOLDERID name, such as `Downloads`."""
    if os.name != "nt":
        return None
    import pywintypes
    from win32comext.shell import shell

    folder_id = getattr(shell, f"FOLDERID_{name}", None)
    if folder_id is None:
        return None  # Not known to this pywin32 or Windows version.
    try:
        return Path(shell.SHGetKnownFolderPath(folder_id, KF_FLAG_DONT_VERIFY, None))
    except pywintypes.com_error:
        logger.info("Known folder unavailable: %s", name)
        return None


def folder_locations() -> dict[str, Path]:
    """The location of each registered folder by its `shell:` name; virtual ones have none."""
    return {folder.name: path for folder, path in _folder_paths().items()}


def _folder_path(folder: KnownFolder) -> Path | None:
    """Where Windows keeps `folder`; None for one that does not exist for this user."""
    import pywintypes
    from win32comext.shell import shell

    try:
        folder_id = pywintypes.IID(folder.folder_id)
        return Path(shell.SHGetKnownFolderPath(folder_id, KF_FLAG_DONT_VERIFY, None))
    except Exception:  # Absent for this user, or a program registered a broken entry.
        return None
