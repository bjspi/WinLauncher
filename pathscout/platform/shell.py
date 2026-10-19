"""Windows shell actions and installed Win32 application discovery."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
START_MENU_SUBFOLDER = "Microsoft/Windows/Start Menu/Programs"


def application_roots() -> tuple[Path, ...]:
    """The per-user and all-users Start menu program folders."""
    roots = []
    for variable in ("APPDATA", "PROGRAMDATA"):
        value = os.environ.get(variable)
        if not value:
            continue
        folder = Path(value) / START_MENU_SUBFOLDER
        if folder.is_dir():
            roots.append(folder)
    return tuple(roots)


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
