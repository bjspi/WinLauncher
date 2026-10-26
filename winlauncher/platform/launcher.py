"""Install the branded Python host and keep application starts separate from pip.

The prebuilt executable is independent of the app version. Its installed copy
holds this installation's paths in Windows resources, so it also works on PATH.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
HOST_NAME = "winlauncher.exe"
HOST_SIGNATURE = "bjspi.WinLauncher.Host.1"
TEMPLATE = Path(__file__).with_name("bin") / "winlauncher-host.exe"
RT_RCDATA = 10
LOAD_LIBRARY_AS_DATAFILE = 0x2
RESOURCE_NOT_FOUND = {1812, 1813, 1814, 1815}
ERROR_BAD_EXE_FORMAT = 193
ENVIRONMENT_BROADCAST_TIMEOUT_MS = 2000
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostRuntime:
    """Paths embedded into a host, plus the fingerprint of its original binary."""

    python: Path
    library: Path
    package: Path
    build: str
    module: str = "winlauncher"

    def resources(self) -> dict[str, str]:
        return {
            "PYTHON_EXECUTABLE": str(self.python),
            "PYTHON_LIBRARY": str(self.library),
            "PYTHON_PACKAGE": str(self.package),
            "HOST_BUILD": self.build,
            "PYTHON_MODULE": self.module,
        }


def bin_directory() -> Path:
    """Use the same user command folder as pipx, uv and the neighboring apps."""
    configured = os.environ.get("XDG_BIN_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".local" / "bin"


def windowless_python() -> Path:
    """The real interpreter stays available for package tools and subprocesses."""
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return windowless if windowless.is_file() else python


def _runtime() -> HostRuntime:
    library_name = sysconfig.get_config_var("LDLIBRARY") or (
        f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    )
    library = Path(sys.base_prefix) / library_name
    if not library.is_file():
        raise OSError(f"Python's shared library is missing: {library}")
    manifest = json.loads(TEMPLATE.with_suffix(".json").read_text(encoding="utf-8"))
    if hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() != manifest["binary"]:
        raise OSError("The WinLauncher host template failed its integrity check.")
    return HostRuntime(windowless_python(), library, PACKAGE_PARENT, manifest["binary"])


def read_host(path: Path) -> HostRuntime | None:
    """Inspect resources without executing the file; a foreign executable returns None."""
    import pywintypes
    import win32api

    if not path.is_file():
        return None
    module = None
    try:
        module = win32api.LoadLibraryEx(str(path), 0, LOAD_LIBRARY_AS_DATAFILE)

        def text(name: str) -> str:
            return win32api.LoadResource(module, RT_RCDATA, name).decode("utf-16-le").rstrip("\0")

        if text("WINLAUNCHER_HOST") != HOST_SIGNATURE:
            return None
        return HostRuntime(
            Path(text("PYTHON_EXECUTABLE")),
            Path(text("PYTHON_LIBRARY")),
            Path(text("PYTHON_PACKAGE")),
            text("HOST_BUILD"),
            text("PYTHON_MODULE"),
        )
    except pywintypes.error as exc:
        if exc.winerror in RESOURCE_NOT_FOUND or exc.winerror == ERROR_BAD_EXE_FORMAT:
            return None
        raise OSError(f"Cannot inspect the WinLauncher host: {exc.strerror}") from exc
    except UnicodeError:
        return None
    finally:
        if module is not None:
            win32api.FreeLibrary(module)


def _belongs_here(runtime: HostRuntime) -> bool:
    return runtime.module == "winlauncher" and runtime.package == PACKAGE_PARENT


def _old_entry_point(path: Path) -> bool:
    """Recognize this interpreter's pip/pipx shim before replacing it."""
    try:
        with zipfile.ZipFile(path) as archive:
            script = archive.read("__main__.py")
        return (
            b"from winlauncher.__main__ import main" in script
            and str(Path(sys.executable).parent).encode("utf-8") in path.read_bytes()
        )
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def write_host(path: Path, runtime: HostRuntime) -> None:
    """Configure a new copy; the caller publishes it only after all resources succeed."""
    import pywintypes
    import win32api

    path.write_bytes(TEMPLATE.read_bytes())
    update = None
    try:
        update = win32api.BeginUpdateResource(str(path), False)
        for name, value in runtime.resources().items():
            win32api.UpdateResource(update, RT_RCDATA, name, (value + "\0").encode("utf-16-le"), 0)
        win32api.EndUpdateResource(update, False)
        update = None
    except pywintypes.error as exc:
        raise OSError(f"Cannot configure the WinLauncher host: {exc.strerror}") from exc
    finally:
        if update is not None:
            win32api.EndUpdateResource(update, True)


def _publish(temporary: Path, target: Path) -> None:
    """Windows permits renaming a running host, but not overwriting its image."""
    try:
        os.replace(temporary, target)
    except PermissionError:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
        previous = target.with_name(f"winlauncher-{digest}.old.exe")
        target.replace(previous)
        try:
            temporary.replace(target)
        except OSError:
            previous.replace(target)
            raise


def _clean_previous(folder: Path) -> None:
    for path in folder.glob("winlauncher-*.old.exe"):
        try:
            runtime = read_host(path)
            if runtime is not None and _belongs_here(runtime):
                path.unlink()
        except OSError:
            pass  # A previous process may still be finishing its shutdown.


def install_launcher() -> Path | None:
    """Install or refresh only our host; other apps and installations stay untouched."""
    if os.name != "nt" or sysconfig.get_platform() != "win-amd64":
        return None
    if not TEMPLATE.is_file():
        logger.warning("The WinLauncher host template is missing; using Python")
        return None
    runtime = _runtime()
    target = bin_directory() / HOST_NAME
    existing = read_host(target)
    if existing is not None and not _belongs_here(existing):
        raise FileExistsError(f"Another WinLauncher installation owns {target}")
    if target.exists() and existing is None and not _old_entry_point(target):
        raise FileExistsError(f"Refusing to replace an unrelated command: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing != runtime:
        with tempfile.TemporaryDirectory(
            prefix="winlauncher-host-", dir=target.parent
        ) as directory:
            temporary = Path(directory) / HOST_NAME
            write_host(temporary, runtime)
            _publish(temporary, target)
        logger.info("Installed WinLauncher host: %s", target)
    _clean_previous(target.parent)
    _add_to_user_path(target.parent)
    return target


def _add_to_user_path(folder: Path) -> None:
    """Preserve PATH entries and their registry type; new terminals discover the host."""
    import winreg

    canonical = os.path.normcase(str(folder.resolve()))

    def contains(value: str) -> bool:
        return any(
            os.path.normcase(os.path.normpath(os.path.expandvars(entry))) == canonical
            for entry in value.split(os.pathsep)
            if entry
        )

    changed = False
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        if not contains(value):
            value = str(folder) + (os.pathsep + value if value else "")
            winreg.SetValueEx(key, "Path", 0, kind, value)
            changed = True
    current = os.environ.get("PATH", "")
    if not contains(current):
        os.environ["PATH"] = str(folder) + os.pathsep + current
    if changed:
        import pywintypes
        import win32con
        import win32gui

        try:
            win32gui.SendMessageTimeout(
                win32con.HWND_BROADCAST,
                win32con.WM_SETTINGCHANGE,
                0,
                "Environment",
                win32con.SMTO_ABORTIFHUNG,
                ENVIRONMENT_BROADCAST_TIMEOUT_MS,
            )
        except pywintypes.error:
            logger.warning(
                "PATH was saved, but Windows could not announce the change", exc_info=True
            )


def application_command(arguments: Sequence[str]) -> list[str]:
    """Application launches use the host; Python tooling keeps sys.executable."""
    if os.name == "nt":
        host = bin_directory() / HOST_NAME
        runtime = read_host(host)
        if runtime is not None and _belongs_here(runtime):
            return [str(host), *arguments]
    return [str(windowless_python()), "-m", "winlauncher", *arguments]


def running_in_host(host: Path) -> bool:
    import win32api

    return Path(win32api.GetModuleFileName(0)).resolve() == host.resolve()
