"""Manage WinLauncher's per-user Windows Startup shortcut.

The shortcut file itself is the source of truth. WinLauncher only ever changes a
shortcut it recognizes as its own and refuses to touch an unrelated one.

COM objects are created and released inside `_with_wscript_shell`, before the
apartment is uninitialized; exceptions never carry COM references out of it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from winlauncher.platform.launcher import (
    HOST_NAME,
    PACKAGE_PARENT,
    application_command,
    bin_directory,
)

SHORTCUT_NAME = "WinLauncher.lnk"
SHORTCUT_DESCRIPTION = "WinLauncher startup shortcut"
MODULE_ARGUMENTS = ("-m", "winlauncher", "--background")

_Result = TypeVar("_Result")


class ShortcutState(Enum):
    MISSING = "missing"
    OWNED = "owned"
    UNRELATED = "unrelated"


@dataclass(frozen=True)
class _Shortcut:
    target: str
    arguments: str
    description: str
    directory: str
    icon: str

    @property
    def owned(self) -> bool:
        module = subprocess.list2cmdline(MODULE_ARGUMENTS[:2])
        legacy = self.arguments == module or self.arguments.startswith(module + " ")
        host = Path(self.target) == bin_directory() / HOST_NAME
        return self.description == SHORTCUT_DESCRIPTION and (legacy or host)


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
        return subprocess.list2cmdline(self._launch_command()[1:])

    def _launch_command(self) -> list[str]:
        return application_command(["--background", "--data-dir", str(self.data_dir)])

    def migrate(self) -> None:
        """Retarget an owned link without changing its arguments or enabling autostart.

        In particular, a custom data directory saved by an older instance wins
        over the data directory of the instance performing the migration.
        """
        if os.name != "nt" or not self.path.exists():
            return
        command = self._launch_command()
        if Path(command[0]) != bin_directory() / HOST_NAME:
            return
        current = _with_wscript_shell(lambda shell: _read_shortcut(shell, self.path))
        if not current.owned:
            return
        prefix = subprocess.list2cmdline(MODULE_ARGUMENTS[:2])
        arguments = current.arguments
        if arguments == prefix:
            arguments = ""
        elif arguments.startswith(prefix + " "):
            arguments = arguments[len(prefix) + 1 :]
        updated = replace(current, target=command[0], arguments=arguments, icon=command[0] + ",0")
        if updated != current:
            self._write_shortcut(updated)

    def _state(self) -> ShortcutState:
        path = self.path
        if not path.exists():
            return ShortcutState.MISSING
        shortcut = _with_wscript_shell(lambda shell: _read_shortcut(shell, path))
        return ShortcutState.OWNED if shortcut.owned else ShortcutState.UNRELATED

    def _write_shortcut(self, shortcut: _Shortcut | None = None) -> None:
        """Save into a private temporary folder, then atomically replace the shortcut."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="winlauncher-", dir=path.parent) as folder:
            temporary = Path(folder) / path.name
            _with_wscript_shell(lambda shell: self._save_shortcut(shell, temporary, shortcut))
            os.replace(temporary, path)

    def _save_shortcut(self, shell: Any, target: Path, saved: _Shortcut | None) -> None:
        if saved is None:
            command = self._launch_command()
            saved = _Shortcut(
                command[0],
                subprocess.list2cmdline(command[1:]),
                SHORTCUT_DESCRIPTION,
                str(PACKAGE_PARENT),
                command[0] + ",0",
            )
        shortcut = shell.CreateShortcut(str(target))
        shortcut.TargetPath = saved.target
        shortcut.Arguments = saved.arguments
        shortcut.WorkingDirectory = saved.directory
        shortcut.Description = saved.description
        shortcut.IconLocation = saved.icon
        shortcut.Save()


def _user_startup_folder() -> Path:
    from win32comext.shell import shell, shellcon

    return Path(shell.SHGetKnownFolderPath(shellcon.FOLDERID_Startup))


def _read_shortcut(shell: Any, path: Path) -> _Shortcut:
    shortcut = shell.CreateShortcut(str(path))
    return _Shortcut(
        str(shortcut.TargetPath),
        str(shortcut.Arguments),
        str(shortcut.Description),
        str(shortcut.WorkingDirectory),
        str(shortcut.IconLocation),
    )


def _with_wscript_shell(action: Callable[[Any], _Result]) -> _Result:
    import pythoncom

    pythoncom.CoInitialize()
    try:
        return _call_with_shell(action)
    finally:
        pythoncom.CoUninitialize()


def _call_with_shell(action: Callable[[Any], _Result]) -> _Result:
    """Run `action`; its frames and COM objects are gone when this function returns."""
    import pywintypes
    import win32com.client

    try:
        return action(win32com.client.Dispatch("WScript.Shell"))
    except pywintypes.com_error as exc:
        message = f"Windows could not access the Startup shortcut: {exc.strerror}"
    # Raised outside the except block, so no traceback keeps COM objects alive.
    raise OSError(message)
