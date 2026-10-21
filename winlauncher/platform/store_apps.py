"""Store apps from Windows' "All apps" list, as shortcuts the catalog can index.

Apps installed from the Microsoft Store, such as Telegram Desktop, have no
Start-menu shortcut file and live in a folder users may not open, so no catalog
source can find them. Before each full scan WinLauncher writes one shortcut per
Store app into `store_app_folder()`, which it indexes like a Start-menu folder:
the shortcut opens the app as the Start menu does, and the shell draws its icon.
Desktop programs already have Start-menu shortcuts and are left out.

The folder lies outside WinLauncher's data directory, which indexing skips.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

FOLDER = Path("WinLauncher") / "Store apps"  # Below %LOCALAPPDATA%.
SHORTCUT_SUFFIX = ".lnk"
# A Store app's AppUserModelID is `<package family name>!<application id>`;
# desktop programs in the list have a path or a plain id instead.
PACKAGED_APP_SEPARATOR = "!"
_UNSAFE_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
logger = logging.getLogger(__name__)


def store_app_folder() -> Path | None:
    """Where the shortcuts live; None where Windows has no local application data."""
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / FOLDER if base else None


def sync_store_apps(folder: Path) -> None:
    """Leave exactly one shortcut per installed Store app in `folder`.

    Existing shortcuts are kept as they are, so an unchanged app list writes
    nothing and the catalog does not change. If Windows cannot list the apps,
    the folder keeps its shortcuts until the next scan.
    """
    if os.name != "nt":
        return
    import pythoncom

    pythoncom.CoInitialize()
    try:
        _sync(folder)
    except Exception:
        logger.warning("Could not update the Store app shortcuts", exc_info=True)
    finally:
        pythoncom.CoUninitialize()


def shortcut_name(app_name: str) -> str:
    """A file name showing `app_name`; characters Windows forbids become spaces."""
    name = " ".join(_UNSAFE_FILE_NAME.sub(" ", app_name).split()).rstrip(". ")
    return (name or "App") + SHORTCUT_SUFFIX


def _sync(folder: Path) -> None:
    """COM objects are released when this function returns, before COM is uninitialized."""
    wanted: dict[str, tuple[str, Any]] = {}
    for app_name, item in _store_apps():
        name = shortcut_name(app_name)
        wanted.setdefault(name.casefold(), (name, item))  # Equal names: the first app wins.
    folder.mkdir(parents=True, exist_ok=True)
    for existing in folder.glob("*" + SHORTCUT_SUFFIX):
        if existing.name.casefold() not in wanted:
            existing.unlink(missing_ok=True)  # The app was uninstalled.
    for name, item in wanted.values():
        if not (folder / name).exists():
            _write_shortcut(folder / name, item)


def _store_apps() -> list[tuple[str, Any]]:
    """Display name and shell item of every Store app in the "All apps" list."""
    from win32comext.shell import shell, shellcon

    folder = shell.SHCreateItemFromParsingName("shell:AppsFolder", None, shell.IID_IShellItem)
    items = folder.BindToHandler(None, shell.BHID_EnumItems, shell.IID_IEnumShellItems)
    return [
        (item.GetDisplayName(shellcon.SIGDN_NORMALDISPLAY), item)
        for item in items
        if PACKAGED_APP_SEPARATOR in item.GetDisplayName(shellcon.SIGDN_PARENTRELATIVEPARSING)
    ]


def _write_shortcut(path: Path, item: Any) -> None:
    """A shortcut to the app's shell item, as Windows creates when dragging it from Start."""
    import pythoncom
    from win32comext.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
    )
    link.SetIDList(shell.SHGetIDListFromObject(item))
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), True)
