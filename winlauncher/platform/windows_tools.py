"""Windows' own tools and settings, as shortcuts the catalog can index.

Control Panel tasks, the pages of the Settings app, management consoles (`.msc`)
and system programs such as Task Manager have no Start-menu shortcut under the
name Windows shows for them. Before each full scan WinLauncher writes them as
shortcuts into `windows_tools_folder()`, one subfolder per kind, named in the
Windows display language:

- Control Panel: every Control Panel item and, for every setting, the tasks of
  the "All Tasks" folder ("God Mode").
- Settings: the pages of the Settings app's own search index and, for every
  setting, each of its other entries, opening its page.
- Tools: management consoles and system programs, named with their file name,
  so `services.msc` and `taskmgr` find them as well as *Dienste* or *Task-Manager*.

Every name comes from Windows, so nothing here is translated. Optionally the
English names Windows has for them are added below `English`, because guides on
the web use those; names equal to the display language's are left out there. A
kind Windows cannot list keeps its shortcuts until the next scan.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from winlauncher.platform.shortcuts import (
    ShortcutWriter,
    shortcut_name,
    sync_shortcuts,
    write_item_shortcut,
    write_target_shortcut,
)

FOLDER = Path("WinLauncher") / "Windows settings"  # Below %LOCALAPPDATA%.
CONTROL_PANEL = "Control Panel"
SETTINGS = "Settings"
TOOLS = "Tools"
ENGLISH_FOLDER = "English"
ENGLISH = "en-US"
CONTROL_PANEL_ITEMS = "shell:::{21EC2020-3AEA-1069-A2DD-08002B30309D}"
ALL_TASKS = "shell:::{ED7BA470-8E54-465E-825C-99712043E01C}"  # "God Mode".
# System programs without a Start-menu shortcut of their own name, below %WINDIR%.
SYSTEM_PROGRAMS = (
    "System32/taskmgr.exe",
    "System32/msconfig.exe",
    "regedit.exe",
    "System32/msinfo32.exe",
    "System32/resmon.exe",
    "System32/dxdiag.exe",
    "System32/cleanmgr.exe",
    "System32/charmap.exe",
    "System32/rstrui.exe",
    "System32/mdsched.exe",
    "System32/SystemPropertiesAdvanced.exe",
    "System32/mstsc.exe",
    "System32/osk.exe",
    "System32/cmd.exe",
)
SETTINGS_APP = "ImmersiveControlPanel"  # Below %WINDIR%.
SETTINGS_INDEX = "Settings/AllSystemSettings_*.xml"
SETTINGS_ICON = "SystemSettings.exe"
# SystemSettings.dll lists each page id right before a `ms-settings:` link to it.
_UTF16_TEXT = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
_PAGE_ID = re.compile(r"SettingsPage\w+|\{[0-9A-Fa-f-]{36}\}")
SETTINGS_URI = "ms-settings:"
# Longer search-index texts are sentences, not names anyone types.
MAX_SETTING_TITLE = 90
MUI_LANGUAGE_NAME = 0x8
logger = logging.getLogger(__name__)

Resolver = Callable[[str], str | None]
Shortcuts = list[tuple[str, ShortcutWriter]]


def windows_tools_folder() -> Path | None:
    """Where the shortcuts live; None where Windows has no local application data."""
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / FOLDER if base else None


def sync_windows_tools(folder: Path, *, every_setting: bool = True, english: bool = False) -> None:
    """Leave one shortcut per tool and setting Windows lists, below `folder`.

    Without `every_setting`, only Settings pages and Control Panel items, no
    single settings or tasks. With `english`, also one shortcut per English name
    that differs, below `ENGLISH_FOLDER`.
    """
    if os.name != "nt":
        return
    import pythoncom

    pythoncom.CoInitialize()
    try:
        localized = _sync_kinds(folder, display_languages(), every_setting)
        if english:
            with _thread_languages(ENGLISH):
                _sync_kinds(folder / ENGLISH_FOLDER, [ENGLISH], every_setting, known=localized)
        else:
            shutil.rmtree(folder / ENGLISH_FOLDER, ignore_errors=True)
    finally:
        pythoncom.CoUninitialize()


def display_languages() -> list[str]:
    """The user's display languages, such as `de-DE`, most preferred first."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    count, size = wintypes.ULONG(), wintypes.ULONG()
    get = ctypes.windll.kernel32.GetUserPreferredUILanguages
    if not get(MUI_LANGUAGE_NAME, ctypes.byref(count), None, ctypes.byref(size)):
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    if not get(MUI_LANGUAGE_NAME, ctypes.byref(count), buffer, ctypes.byref(size)):
        return []
    return [language for language in buffer[: size.value].split("\0") if language]


def _sync_kinds(
    folder: Path,
    languages: Sequence[str],
    every_setting: bool,
    known: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    """Sync each kind's subfolder; returns the file names written per kind.

    Titles whose file name `known` already holds for that kind are left out.
    """
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    kinds: dict[str, Callable[[], Shortcuts]] = {
        CONTROL_PANEL: lambda: _control_panel(every_setting),
        SETTINGS: lambda: _settings_pages(windows / SETTINGS_APP, every_setting),
        TOOLS: lambda: _tools(windows, languages),
    }
    names: dict[str, set[str]] = {}
    for kind, list_shortcuts in kinds.items():
        skipped = (known or {}).get(kind, set())
        try:
            shortcuts = [
                (title, write)
                for title, write in list_shortcuts()
                if shortcut_name(title).casefold() not in skipped
            ]
            sync_shortcuts(folder / kind, shortcuts)
        except Exception:
            logger.warning("Could not update the %s shortcuts", kind, exc_info=True)
            continue
        names[kind] = {shortcut_name(title).casefold() for title, _ in shortcuts}
    return names


@contextmanager
def _thread_languages(language: str) -> Iterator[None]:
    """Windows resources this thread loads come in `language` where it is installed."""
    import ctypes

    set_languages = ctypes.windll.kernel32.SetThreadPreferredUILanguages
    set_languages(MUI_LANGUAGE_NAME, ctypes.create_unicode_buffer(language + "\0"), None)
    try:
        yield
    finally:
        set_languages(0, None, None)  # Back to the user's languages.


# Control Panel -----------------------------------------------------------------


def _control_panel(every_setting: bool) -> Shortcuts:
    from win32comext.shell import shell, shellcon

    shortcuts = []
    for parsing_name in (
        (CONTROL_PANEL_ITEMS, ALL_TASKS) if every_setting else (CONTROL_PANEL_ITEMS,)
    ):
        folder = shell.SHCreateItemFromParsingName(parsing_name, None, shell.IID_IShellItem)
        for item in folder.BindToHandler(None, shell.BHID_EnumItems, shell.IID_IEnumShellItems):
            try:
                title = item.GetDisplayName(shellcon.SIGDN_NORMALDISPLAY)
            except Exception:  # Some items only fail for their name; skip them.
                continue
            shortcuts.append((title, lambda path, item=item: write_item_shortcut(path, item)))
    return shortcuts


# Settings app ------------------------------------------------------------------


def _settings_pages(app: Path, every_setting: bool) -> Shortcuts:
    links = settings_links((app / "SystemSettings.dll").read_bytes())
    icon = (str(app / SETTINGS_ICON), 0)
    explorer = str(app.parent / "explorer.exe")
    documents = [ET.parse(path).getroot() for path in sorted(glob.glob(str(app / SETTINGS_INDEX)))]
    return [
        (title, lambda path, uri=uri: write_target_shortcut(path, explorer, uri, icon))
        for title, uri in settings_entries(
            documents, links, _load_indirect_string, pages_only=not every_setting
        )
    ]


def settings_links(library: bytes) -> dict[str, str]:
    """Settings page id to its first `ms-settings:` link, read from SystemSettings.dll."""
    texts = [match.group().decode("utf-16-le") for match in _UTF16_TEXT.finditer(library)]
    links: dict[str, str] = {}
    for page, link in zip(texts, texts[1:], strict=False):
        if link.startswith(SETTINGS_URI) and _PAGE_ID.fullmatch(page):
            links.setdefault(page, link)
    return links


def settings_entries(
    documents: Iterable[ET.Element],
    links: dict[str, str],
    resolve: Resolver,
    *,
    pages_only: bool = False,
) -> Iterator[tuple[str, str]]:
    """Title and link of each search-index entry whose page has a link.

    Titles are resource references that `resolve` reads in the display language.
    With `pages_only`, only the entries for whole pages, named `AAA_<page id>`.
    """
    for document in documents:
        for entry in document.iter("SearchableContent"):
            page = entry.findtext("SettingIdentity/PageID") or entry.findtext(
                "SettingIdentity/SettingPaths/Path/PageID"
            )
            reference = entry.findtext("SettingInformation/Description")
            if page not in links or not reference:
                continue
            if pages_only and not _is_page_entry(entry.findtext("Filename") or "", page):
                continue
            title = resolve(reference)
            if title and len(title) <= MAX_SETTING_TITLE and "\n" not in title:
                yield title.strip(), links[page]


def _is_page_entry(file_name: str, page: str) -> bool:
    """Windows names a page's own entry `AAA_<page id>`, variants with `-<n>`."""
    return re.fullmatch(rf"AAA_{re.escape(page)}(-\d+)?", file_name) is not None


def _load_indirect_string(reference: str) -> str | None:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    result = ctypes.windll.shlwapi.SHLoadIndirectString(reference, buffer, len(buffer), None)
    return buffer.value if result == 0 else None


# Tools -------------------------------------------------------------------------


def _tools(windows: Path, languages: Sequence[str]) -> Shortcuts:
    system = windows / "System32"
    shortcuts = []
    for console in sorted(system.glob("*.msc")):
        localized = (system / language / console.name for language in languages)
        source = next((path for path in localized if path.is_file()), console)
        title = console_title(source.read_bytes())
        # The shell draws the icon stored in the console, which is the snap-in's own.
        shortcuts.append(
            (
                f"{title or console.stem} ({console.name})",
                lambda path, target=str(console): write_target_shortcut(path, target),
            )
        )
    for relative in SYSTEM_PROGRAMS:
        program = windows / relative
        if program.is_file():
            title = _file_description(program) or program.stem
            shortcuts.append(
                (
                    f"{title} ({program.stem})",
                    lambda path, target=str(program): write_target_shortcut(path, target),
                )
            )
    return shortcuts


def console_title(console: bytes) -> str | None:
    """The title a management console file names: its window title, or else the
    name of the node it opens."""
    try:
        root = ET.fromstring(console)
    except ET.ParseError:
        return None
    strings = {
        string.get("ID"): text.strip()
        for string in root.iter("String")
        if (text := string.text) and text.strip()
    }
    reference = root.find("VisualAttributes/String[@Name='ApplicationTitle']")
    bookmark = root.find(".//BookMark[@Name='RootNode']")
    if reference is None and bookmark is not None:
        node = root.find(f".//Node[@ID='{bookmark.get('NodeID')}']")
        if node is not None and node is root.find("ScopeTree/Nodes/Node"):
            node = node.find("Nodes/Node")  # The console root: name its first snap-in.
        reference = None if node is None else node.find("String[@Name='Name']")
    return None if reference is None else strings.get(reference.get("ID"))


def _file_description(program: Path) -> str | None:
    """The program's description in the display language, as Explorer shows it."""
    import win32api

    try:
        translations = win32api.GetFileVersionInfo(str(program), r"\VarFileInfo\Translation")
        language, codepage = translations[0]
        key = rf"\StringFileInfo\{language:04x}{codepage:04x}\FileDescription"
        description: Any = win32api.GetFileVersionInfo(str(program), key)
    except Exception:
        return None
    return description.strip() if isinstance(description, str) and description.strip() else None
