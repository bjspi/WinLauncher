"""Windows shell actions and installed application discovery."""

from __future__ import annotations

import functools
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

from winlauncher.catalog.model import path_key
from winlauncher.platform.store_apps import store_app_folder

APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
START_MENU_SUBFOLDER = "Microsoft/Windows/Start Menu/Programs"
# Known folders (FOLDERID_ names) Explorer shows with their own icon, wherever
# they were redirected to.
SPECIAL_FOLDERS = ("Profile", "Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")
# Known folders offered as %NAME% shortcuts beside the environment variables.
FOLDER_SHORTCUTS = (
    *SPECIAL_FOLDERS[1:],
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


@functools.cache
def special_folders() -> frozenset[str]:
    """Path keys of the user's known folders, such as Desktop and Downloads.

    Asked once: Windows resolves redirection (OneDrive, another drive) itself.
    """
    return frozenset(path_key(path) for name in SPECIAL_FOLDERS if (path := known_folder(name)))


@functools.cache
def folder_shortcuts() -> dict[str, Path]:
    """Known folders by `%NAME%` shortcut name, such as DOWNLOADS or SENDTO."""
    return {name.upper(): path for name in FOLDER_SHORTCUTS if (path := known_folder(name))}


def known_folder(name: str) -> Path | None:
    """The current location of a Windows known folder, such as `Downloads`."""
    if os.name != "nt":
        return None
    import pywintypes
    from win32comext.shell import shell

    folder_id = getattr(shell, f"FOLDERID_{name}", None)
    if folder_id is None:
        return None  # Not known to this pywin32 or Windows version.
    try:
        return Path(shell.SHGetKnownFolderPath(folder_id, 0, None))
    except pywintypes.com_error:
        logger.info("Known folder unavailable: %s", name)
        return None


def application_roots() -> tuple[Path, ...]:
    """The per-user and all-users Start menu program folders and the Store app shortcuts."""
    folders = [
        Path(value) / START_MENU_SUBFOLDER
        for variable in ("APPDATA", "PROGRAMDATA")
        if (value := os.environ.get(variable))
    ]
    folders.append(store_app_folder())
    return tuple(folder for folder in folders if folder is not None and folder.is_dir())


def registered_applications() -> tuple[Path, ...]:
    """Executables registered under App Paths in both hives and registry views."""
    if os.name != "nt":
        return ()
    applications = {path for path in _app_path_values() if _is_executable_path(path)}
    return tuple(sorted(applications))


def _app_path_values() -> Iterator[Path]:
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(hive, APP_PATHS_KEY, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with key:
                yield from _default_values(key)


def _default_values(key) -> Iterator[Path]:
    """The default value of every subkey, as an expanded, unquoted path."""
    import winreg

    for index in range(winreg.QueryInfoKey(key)[0]):
        try:
            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as application:
                value = winreg.QueryValueEx(application, "")[0]
        except OSError:
            continue
        if isinstance(value, str):
            yield Path(os.path.expandvars(value).strip('"'))


def _is_executable_path(path: Path) -> bool:
    return path.is_absolute() and path.suffix.lower() == ".exe"


def open_path(path: Path) -> None:
    _require_existing(path)
    os.startfile(str(path), cwd=str(path.parent))


def show_in_folder(path: Path) -> None:
    _require_existing(path)
    explorer = Path(os.environ["WINDIR"]) / "explorer.exe"
    # Explorer parses its own command line; this exact quoting handles commas and spaces.
    subprocess.Popen(f'"{explorer}" /select,"{path}"')


def _require_existing(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"This item no longer exists: {path}")
