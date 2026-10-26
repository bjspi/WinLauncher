"""Folders of generated shortcuts, which the catalog indexes like Start-menu folders.

Windows lists some things it can open (Store apps, Control Panel tasks, Settings
pages) without a file behind them. WinLauncher writes one shortcut per thing into
a folder it owns, named as Windows shows it, so the catalog finds it by name,
the shortcut opens it and the shell draws its icon. Callers initialize COM.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

SHORTCUT_SUFFIX = ".lnk"
_UNSAFE_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

ShortcutWriter = Callable[[Path], None]


def shortcut_name(title: str) -> str:
    """A file name showing `title`; characters Windows forbids become spaces."""
    name = " ".join(_UNSAFE_FILE_NAME.sub(" ", title).split()).rstrip(". ")
    return (name or "App") + SHORTCUT_SUFFIX


def sync_shortcuts(folder: Path, shortcuts: Iterable[tuple[str, ShortcutWriter]]) -> None:
    """Leave exactly one shortcut per title in `folder`; equal names: the first wins.

    Existing shortcuts are kept as they are, so an unchanged list writes nothing
    and the catalog does not change. Shortcuts no longer listed are deleted.
    """
    wanted: dict[str, tuple[str, ShortcutWriter]] = {}
    for title, write in shortcuts:
        name = shortcut_name(title)
        wanted.setdefault(name.casefold(), (name, write))
    folder.mkdir(parents=True, exist_ok=True)
    for existing in folder.glob("*" + SHORTCUT_SUFFIX):
        if existing.name.casefold() not in wanted:
            existing.unlink(missing_ok=True)
    for name, write in wanted.values():
        if not (folder / name).exists():
            write(folder / name)


def write_item_shortcut(path: Path, item: Any) -> None:
    """A shortcut to a shell item, as Windows creates when dragging it from Start."""
    from win32comext.shell import shell

    link = _new_link()
    link.SetIDList(shell.SHGetIDListFromObject(item))
    _save(link, path)


def write_target_shortcut(
    path: Path,
    target: str,
    arguments: str = "",
    icon: tuple[str, int] | None = None,
    *,
    description: str = "",
    working_directory: str = "",
) -> None:
    """A shortcut that runs `target` with `arguments`, showing `icon` (file, index)."""
    link = _new_link()
    link.SetPath(target)
    if arguments:
        link.SetArguments(arguments)
    if icon is not None:
        link.SetIconLocation(*icon)
    if description:
        link.SetDescription(description)
    if working_directory:
        link.SetWorkingDirectory(working_directory)
    _save(link, path)


def load_shortcut(path: Path) -> Any:
    """Load an IShellLink; the caller must release it before uninitializing COM."""
    import pythoncom

    link = _new_link()
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path))
    return link


def _new_link() -> Any:
    import pythoncom
    from win32comext.shell import shell

    return pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
    )


def _save(link: Any, path: Path) -> None:
    import pythoncom

    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), True)
