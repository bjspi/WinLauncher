"""Windows shell actions and installed application discovery."""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import subprocess
import threading
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING

from winlauncher.catalog import storage
from winlauncher.catalog.model import path_key
from winlauncher.platform.known_folders import folder_locations, known_folder
from winlauncher.platform.store_apps import store_app_folder
from winlauncher.platform.windows_tools import shell_folder_shortcuts, windows_tools_folder

if TYPE_CHECKING:
    from winlauncher.settings import Settings

APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
START_MENU_SUBFOLDER = "Microsoft/Windows/Start Menu/Programs"
# Known folders (FOLDERID_ names) Explorer shows with their own icon, wherever
# they were redirected to.
SPECIAL_FOLDERS = ("Profile", "Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")
# SHOpenWithDialog: run the chosen program, and offer "Always use this app".
OAIF_ALLOW_REGISTRATION, OAIF_REGISTER_EXT, OAIF_EXEC = 0x1, 0x2, 0x4
OPEN_WITH_FLAGS = OAIF_ALLOW_REGISTRATION | OAIF_REGISTER_EXT | OAIF_EXEC
COINIT_APARTMENTTHREADED = 0x2
ERROR_CANCELLED = 1223  # The user declined, such as the consent prompt.
ERROR_CANCELLED_HRESULT = -2147023673  # HRESULT_FROM_WIN32(ERROR_CANCELLED), signed.
RUN_AS_ADMINISTRATOR = "runas"  # The shell verb that asks for elevation.
logger = logging.getLogger(__name__)


@functools.cache
def special_folders() -> frozenset[str]:
    """Path keys of the user's known folders, such as Desktop and Downloads.

    Asked once: Windows resolves redirection (OneDrive, another drive) itself.
    """
    return frozenset(path_key(path) for name in SPECIAL_FOLDERS if (path := known_folder(name)))


def shell_folders() -> dict[str, Path]:
    """What each `shell:` name opens: a folder, or a shortcut for a virtual folder.

    Those shortcuts exist only after a scan with Windows tools enabled wrote them,
    so they are looked up on every call; folder locations are asked once.
    """
    named = folder_locations()
    if (tools := windows_tools_folder()) is not None:
        named.update(shell_folder_shortcuts(tools))
    return named


def application_roots(*, apps: bool = True, windows_tools: bool = False) -> tuple[Path, ...]:
    """Existing application folders.

    `apps`: the per-user and all-users Start menu program folders and the Store
    app shortcuts. `windows_tools`: the Windows tool and setting shortcuts.
    """
    folders: list[Path | None] = []
    if apps:
        folders.extend(
            Path(value) / START_MENU_SUBFOLDER
            for variable in ("APPDATA", "PROGRAMDATA")
            if (value := os.environ.get(variable))
        )
        folders.append(store_app_folder())
    if windows_tools:
        folders.append(windows_tools_folder())
    return tuple(folder for folder in folders if folder is not None and folder.is_dir())


def indexed_application_roots(settings: Settings) -> tuple[Path, ...]:
    """The application roots `settings` include in the catalog."""
    return application_roots(apps=settings.index_apps, windows_tools=settings.index_windows_tools)


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


def run_as_administrator(path: Path) -> bool:
    """Start `path` elevated after Windows' consent prompt; False if the user declined.

    Shortcuts work too: the shell elevates the program or console they open.
    """
    _require_existing(path)
    try:
        os.startfile(str(path), RUN_AS_ADMINISTRATOR, cwd=str(path.parent))
    except OSError as error:
        if getattr(error, "winerror", None) == ERROR_CANCELLED:
            return False
        raise
    return True


class _OpenAsInfo(ctypes.Structure):
    _fields_ = (
        ("pcszFile", wintypes.LPCWSTR),
        ("pcszClass", wintypes.LPCWSTR),
        ("oaifInFlags", ctypes.c_int),
    )


def open_with(path: Path) -> None:
    """Windows' own Open with dialog for `path`, as Explorer shows it.

    The dialog is modal and blocks its thread until the user chose, so it gets a
    thread of its own instead of holding up the launcher's action worker.
    """
    _require_existing(path)
    threading.Thread(target=_open_with_dialog, args=(path,), name="open-with", daemon=True).start()


def _open_with_dialog(path: Path) -> None:
    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHOpenWithDialog.argtypes = (wintypes.HWND, ctypes.POINTER(_OpenAsInfo))
    shell32.SHOpenWithDialog.restype = ctypes.c_long
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        info = _OpenAsInfo(str(path), None, OPEN_WITH_FLAGS)
        result = shell32.SHOpenWithDialog(None, ctypes.byref(info))
        if result < 0 and result != ERROR_CANCELLED_HRESULT:
            logger.warning("Open with failed for %s: HRESULT 0x%08X", path, result & 0xFFFFFFFF)
    finally:
        ole32.CoUninitialize()


def show_in_folder(path: Path) -> None:
    _require_existing(path)
    explorer = Path(os.environ["WINDIR"]) / "explorer.exe"
    # Explorer parses its own command line; this exact quoting handles commas and spaces.
    subprocess.Popen(f'"{explorer}" /select,"{path}"')


def run_command(command_line: str, folder: Path) -> None:
    """Start a command line in `folder`, as typed in the Run dialog.

    A console program gets a console of its own instead of WinLauncher's.
    """
    _require_existing(folder)
    try:
        subprocess.Popen(command_line, cwd=folder, creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise OSError(f"Windows could not run {command_line}: {exc.strerror or exc}") from exc


IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
FSCTL_GET_REPARSE_POINT = 0x000900A8
APPEXECLINK_HEADER = 12  # Reparse tag, data length, reserved, then the link version.


def alias_target(path: Path) -> Path:
    """The program an app execution alias such as `WindowsApps\\wt.exe` starts.

    The alias is an empty file whose reparse data names the packaged program;
    its own icon is a generic one. Any other path is returned unchanged.
    """
    try:
        if getattr(os.lstat(path), "st_reparse_tag", 0) != IO_REPARSE_TAG_APPEXECLINK:
            return path
        data = _reparse_data(path)
    except OSError:
        return path
    # Null-terminated UTF-16 strings: package, app user model ID, target, ...
    strings = data[APPEXECLINK_HEADER:].decode("utf-16-le", "replace").split("\0")
    return Path(strings[2]) if len(strings) > 2 and strings[2] else path


def _reparse_data(path: Path) -> bytes:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    share_all, open_existing = 7, 3
    reparse_point, backup_semantics = 0x00200000, 0x02000000
    handle = kernel32.CreateFileW(
        str(path), 0, share_all, None, open_existing, reparse_point | backup_semantics, None
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(16 * 1024)
        size = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            wintypes.HANDLE(handle),
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buffer,
            len(buffer),
            ctypes.byref(size),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[: size.value]
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _require_existing(path: Path) -> None:
    """Raise unless `path` exists, without waiting long for a dead server."""
    storage.require_drive(path)
    if not path.exists():
        raise FileNotFoundError(f"This item no longer exists: {path}")
