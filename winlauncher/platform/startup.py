"""Manage WinLauncher's per-user Windows Startup shortcut.

The shortcut file itself is the source of truth. WinLauncher only ever changes a
shortcut it recognizes as its own and refuses to touch an unrelated one.

Uses Windows' IShellLink directly, without loading Windows Script Host.
COM objects are created and released inside `_with_shortcut`, before the
apartment is uninitialized; exceptions never carry COM references out of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TypeVar

from winlauncher.platform.shortcuts import load_shortcut, write_target_shortcut

SHORTCUT_NAME = "WinLauncher.lnk"
SHORTCUT_DESCRIPTION = "WinLauncher startup shortcut"
MODULE_ARGUMENTS = ("-m", "winlauncher", "--background")
# The directory containing the `winlauncher` package: a source checkout's root or
# site-packages. `python -m winlauncher` works from there in both layouts.
PACKAGE_PARENT = Path(__file__).resolve().parents[2]

_Result = TypeVar("_Result")


class ShortcutState(Enum):
    MISSING = "missing"
    OWNED = "owned"
    UNRELATED = "unrelated"


class StartupShortcut:
    def __init__(self, data_dir: Path, *, startup_folder: Path | None = None):
        self.data_dir = data_dir.resolve()
        self.startup_folder = startup_folder

    @property
    def path(self) -> Path:
        return (self.startup_folder or _user_startup_folder()) / SHORTCUT_NAME

    def is_enabled(self) -> bool:
        if os.name != "nt":
            return False
        return self._state() == ShortcutState.OWNED

    def set_enabled(self, enabled: bool) -> None:
        """Raise OSError with a user-facing message if the change is impossible."""
        if os.name != "nt":
            raise OSError("Autostart is only available on Windows.")
        state = self._state()
        if state == ShortcutState.UNRELATED:
            raise OSError(f"Refusing to change an unrelated shortcut: {self.path}")
        if enabled:
            self._write_shortcut()
        elif state == ShortcutState.OWNED:
            self.path.unlink()

    def launch_arguments(self) -> str:
        arguments = [*MODULE_ARGUMENTS, "--data-dir", str(self.data_dir)]
        return subprocess.list2cmdline(arguments)

    def _state(self) -> ShortcutState:
        path = self.path
        if not path.exists():
            return ShortcutState.MISSING
        description, arguments = _with_shortcut(lambda: _read_shortcut(path))
        owned = description == SHORTCUT_DESCRIPTION and arguments.startswith(
            subprocess.list2cmdline(MODULE_ARGUMENTS[:2])
        )
        return ShortcutState.OWNED if owned else ShortcutState.UNRELATED

    def _write_shortcut(self) -> None:
        """Save into a private temporary folder, then atomically replace the shortcut."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="winlauncher-", dir=path.parent) as folder:
            temporary = Path(folder) / path.name
            _with_shortcut(lambda: self._save_shortcut(temporary))
            os.replace(temporary, path)

    def _save_shortcut(self, target: Path) -> None:
        write_target_shortcut(
            target,
            str(_windowless_python()),
            self.launch_arguments(),
            description=SHORTCUT_DESCRIPTION,
            working_directory=str(PACKAGE_PARENT),
        )


def _windowless_python() -> Path:
    """pythonw.exe of the running environment, so autostart opens no console."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    return windowless if windowless.is_file() else executable


def _user_startup_folder() -> Path:
    from win32comext.shell import shell, shellcon

    return Path(shell.SHGetKnownFolderPath(shellcon.FOLDERID_Startup))


def _read_shortcut(path: Path) -> tuple[str, str]:
    shortcut = load_shortcut(path)
    return shortcut.GetDescription(), shortcut.GetArguments()


def _with_shortcut(action: Callable[[], _Result]) -> _Result:
    import pythoncom

    pythoncom.CoInitialize()
    try:
        return _call_shortcut(action)
    finally:
        pythoncom.CoUninitialize()


def _call_shortcut(action: Callable[[], _Result]) -> _Result:
    """Run `action`; its frames and COM objects are gone when this function returns."""
    import pywintypes

    try:
        return action()
    except pywintypes.com_error as exc:
        message = f"Windows could not access the Startup shortcut: {exc.strerror}"
    # Raised outside the except block, so no traceback keeps COM objects alive.
    raise OSError(message)
