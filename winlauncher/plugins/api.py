"""The plugin interface: what a plugin delivers and what WinLauncher gives it.

A plugin lists items, such as Telegram chats, on a worker thread of its own.
WinLauncher keeps the latest list, searches it together with the catalog, shows
each item with the plugin's label and icon, and asks the plugin to open the
chosen one. Nothing here imports Qt or Win32, so plugins stay testable.

A minimal plugin, saved as `<data directory>/plugins/notes.py`:

    from winlauncher.plugins.api import Plugin, PluginItem

    class Notes(Plugin):
        id = "notes"
        label = "Note"

        def refresh(self, context):
            return [PluginItem("1", "Shopping list", target="C:/Notes/shopping.txt")]
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
OptionValue = bool | int | str


class PluginError(Exception):
    """A failure the plugin explains to the user, such as an expired sign-in."""


class SetupRequired(PluginError):
    """The plugin cannot list anything until the user sets it up in Settings."""


@dataclass(frozen=True, slots=True)
class PluginItem:
    """One searchable entry, such as a chat; the plugin decides what it shows and opens."""

    key: str  # Stable within the plugin: identifies the row across refreshes.
    name: str  # Matched against the typed words; shown after the plugin's label.
    target: str = ""  # What `Plugin.open` launches by default: a URI such as tg://… or a path.
    subtitle: str = ""  # Muted text beside the name.
    keywords: tuple[str, ...] = ()  # Further words that find the item, such as a username.
    icon: str = ""  # An image file, such as a cached avatar; empty uses initials or the plugin's.
    title: str = ""  # Shown instead of "<label>: <name>" when set.
    # Anything else `open` needs, as JSON values; kept in the item cache, never matched.
    data: Mapping[str, object] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        """Reject an item the launcher could not show, so a refresh fails, not a search."""
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Plugin items need a key.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Plugin items need a name.")
        for field_name in ("target", "subtitle", "icon", "title"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(f"A plugin item's {field_name} must be text.")
        keywords = tuple(self.keywords)
        if not all(isinstance(keyword, str) for keyword in keywords):
            raise ValueError("A plugin item's keywords must be text.")
        if not isinstance(self.data, Mapping) or not all(isinstance(key, str) for key in self.data):
            raise ValueError("A plugin item's data must map names to JSON values.")
        object.__setattr__(self, "keywords", keywords)


@dataclass(frozen=True, slots=True)
class PluginOption:
    """A setting of one plugin; its type is the type of `default`."""

    key: str
    label: str
    default: OptionValue
    description: str = ""
    bounds: tuple[int, int] | None = None  # For whole numbers.

    def value(self, stored: object) -> OptionValue:
        """`stored` if it is valid for this option, else the default."""
        if type(stored) is not type(self.default):
            return self.default
        if self.bounds is not None and not self.bounds[0] <= stored <= self.bounds[1]:
            return self.default
        return stored


class Ask(Protocol):
    def __call__(self, title: str, text: str, *, secret: bool = False) -> str | None:
        """Ask the user for one line of text; None when they cancel."""


def no_questions(title: str, text: str, *, secret: bool = False) -> str | None:
    raise SetupRequired(f"{title}: open Settings › Plugins to continue.")


@dataclass(frozen=True, slots=True)
class PluginContext:
    """What one plugin call receives. Calls run on a worker thread, never the GUI's."""

    data_dir: Path  # The plugin's own folder for caches, sessions and avatars; it exists.
    options: Mapping[str, OptionValue]  # Every `Plugin.options` key, validated.
    cancel: threading.Event = field(default_factory=threading.Event)
    # Only commands may ask; a background refresh raises SetupRequired instead.
    ask: Ask = no_questions
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("winlauncher.plugins"))


@dataclass(frozen=True, slots=True)
class PluginCommand:
    """A button on the plugin's settings card, such as Sign in.

    `run` gets a context that may ask questions and returns a message for the user.
    """

    label: str
    run: Callable[[PluginContext], str | None]
    forgets_items: bool = False  # Such as Sign out: the listed items go, too.


class Plugin:
    """Subclass this; WinLauncher creates one instance per plugin and process."""

    id: ClassVar[str] = ""  # Settings key and data folder name: a-z, 0-9, _ and -.
    label: ClassVar[str] = ""  # Shown before each item's name, as in "Telegram: Anna".
    description: ClassVar[str] = ""
    keyword: ClassVar[str] = ""  # Typed first, it searches only this plugin: "tg anna".
    refresh_minutes: ClassVar[int] = 15
    icon: ClassVar[str] = ""  # Image file for items without an icon of their own.
    round_icons: ClassVar[bool] = False  # Avatars: crop icons to circles, initials without one.
    # Regular expressions for typed terms that name one of this plugin's items, such
    # as (r"HCP-\w*",) for ticket keys. A term a pattern describes entirely is
    # matched in one piece instead of loosely; terms with a digit in them already
    # are. Case is ignored, and the other terms of the same query stay loose.
    literal_terms: ClassVar[tuple[str, ...]] = ()
    options: ClassVar[tuple[PluginOption, ...]] = ()

    def refresh(self, context: PluginContext) -> Iterable[PluginItem]:
        """Every item, most relevant (such as most recently used) first.

        Runs on a worker thread at the configured interval; it may take seconds.
        Raise PluginError with a message for the user when it fails.
        """
        raise NotImplementedError

    def open(self, item: PluginItem, context: PluginContext) -> None:
        """Open `item`; by default its target, like a double-click in Explorer would."""
        open_target(item.target)

    def commands(self) -> tuple[PluginCommand, ...]:
        """Buttons for the settings card, such as Sign in and Sign out."""
        return ()


def open_target(target: str) -> None:
    """Hand a URI or path to the program Windows registered for it."""
    if not target:
        raise PluginError("This item has nothing to open.")
    if os.name != "nt":
        raise PluginError("Opening plugin items needs Windows.")
    try:
        os.startfile(target)  # noqa: S606 - the plugin chose the target.
    except OSError as exc:
        raise PluginError(f"Windows could not open {target}: {exc.strerror or exc}") from exc
