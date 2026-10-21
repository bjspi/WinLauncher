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
from pathlib import Path
from typing import Any

from winlauncher.platform.shortcuts import (
    SHORTCUT_SUFFIX,
    shortcut_name,
    sync_shortcuts,
    write_item_shortcut,
)

FOLDER = Path("WinLauncher") / "Store apps"  # Below %LOCALAPPDATA%.
# A Store app's AppUserModelID is `<package family name>!<application id>`;
# desktop programs in the list have a path or a plain id instead.
PACKAGED_APP_SEPARATOR = "!"
__all__ = ["SHORTCUT_SUFFIX", "shortcut_name", "store_app_folder", "sync_store_apps"]
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


def _sync(folder: Path) -> None:
    """COM objects are released when this function returns, before COM is uninitialized."""
    sync_shortcuts(
        folder,
        (
            (name, lambda path, item=item: _write_shortcut(path, item))
            for name, item in _store_apps()
        ),
    )


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
    write_item_shortcut(path, item)
