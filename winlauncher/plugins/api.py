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
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol

from winlauncher.connectivity import InternetState
from winlauncher.connectivity import InternetStatus as InternetStatus

PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
OptionValue = bool | int | str


class PluginError(Exception):
    """A failure the plugin explains to the user, such as an expired sign-in."""


class SetupRequired(PluginError):
    """The plugin cannot list anything until the user sets it up in Settings."""


class ChipTone(StrEnum):
    """What a chip says; the launcher picks the color that suits the active theme."""

    NEUTRAL = "neutral"  # Gray: a type, a label, a state with no judgement such as "To Do".
    INFO = "info"  # Blue: work going on, such as "In Progress".
    SUCCESS = "success"  # Green: done, approved, online.
    WARNING = "warning"  # Orange: waiting, due soon, needs attention.
    DANGER = "danger"  # Red: blocked, failed, overdue.


@dataclass(frozen=True, slots=True)
class Chip:
    """A short colored label in an item's row, such as a ticket's status."""

    text: str
    tone: ChipTone = ChipTone.NEUTRAL

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("A chip needs text.")
        try:
            object.__setattr__(self, "tone", ChipTone(self.tone))
        except ValueError:
            tones = ", ".join(tone.value for tone in ChipTone)
            raise ValueError(f"A chip's tone must be one of {tones}.") from None


MAX_CHIPS = 3  # Shown per row; further chips are kept but not drawn.


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
    routes: tuple[str, ...] = ()  # Ids of the plugin's routes that list this item, if any.
    # Anything else `open` needs, as JSON values; kept in the item cache, never matched.
    data: Mapping[str, object] = field(default_factory=dict, compare=False)
    # Where the item lives, outermost first, such as ("Contoso", "Finance", "Q3"). Shown
    # instead of `subtitle`; a narrow row drops whole middle parts, never the first.
    breadcrumbs: tuple[str, ...] = ()
    # Short labels in the row before the location, such as a ticket's status. Shown,
    # never matched: put words users type into `keywords`.
    chips: tuple[Chip, ...] = ()

    def __post_init__(self) -> None:
        """Reject an item the launcher could not show, so a refresh fails, not a search."""
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Plugin items need a key.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Plugin items need a name.")
        for field_name in ("target", "subtitle", "icon", "title"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(f"A plugin item's {field_name} must be text.")
        keywords, routes = tuple(self.keywords), tuple(self.routes)
        breadcrumbs, chips = tuple(self.breadcrumbs), tuple(self.chips)
        if not all(isinstance(part, str) and part.strip() for part in breadcrumbs):
            raise ValueError("A plugin item's breadcrumbs must be non-empty text.")
        if not all(isinstance(chip, Chip) for chip in chips):
            raise ValueError("A plugin item's chips must be Chip values.")
        if not all(isinstance(keyword, str) for keyword in keywords):
            raise ValueError("A plugin item's keywords must be text.")
        if not all(isinstance(route, str) for route in routes):
            raise ValueError("A plugin item's routes must be route ids.")
        if not isinstance(self.data, Mapping) or not all(isinstance(key, str) for key in self.data):
            raise ValueError("A plugin item's data must map names to JSON values.")
        object.__setattr__(self, "keywords", keywords)
        object.__setattr__(self, "routes", routes)
        object.__setattr__(self, "breadcrumbs", breadcrumbs)
        object.__setattr__(self, "chips", chips)


@dataclass(frozen=True, slots=True)
class PluginOption:
    """A setting of one plugin; its type is the type of `default`."""

    key: str
    label: str
    default: OptionValue
    description: str = ""
    bounds: tuple[int, int] | None = None  # For whole numbers.
    multiline: bool = False  # For text options: an editor that preserves line breaks.

    def value(self, stored: object) -> OptionValue:
        """`stored` if it is valid for this option, else the default."""
        if type(stored) is not type(self.default):
            return self.default
        if self.bounds is not None and not self.bounds[0] <= stored <= self.bounds[1]:
            return self.default
        return stored


@dataclass(frozen=True, slots=True)
class PluginRoute:
    """Keywords that search only part of a plugin's items.

    A ticket plugin may list every ticket under `jira` and offer the route
    `PluginRoute("bugs", "Bugs only", ("bug",))`: `bug login` then searches only
    the items whose `routes` name `bugs`. The user may change its keywords.
    """

    id: str  # Stable: ties the user's keywords to the route. a-z, 0-9, _ and -.
    label: str  # Shown in Settings, such as "Bugs only".
    keywords: tuple[str, ...] = ()  # Suggested; the user's own replace them.
    description: str = ""


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
    # Read the current shared hint, including during a long refresh or retry loop.
    internet: Callable[[], InternetState] = InternetState


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
    # Typed first, a keyword puts this plugin's items on top: "tg anna". All of them
    # work alike, so a plugin may suggest synonyms; the user may change them.
    keywords: ClassVar[tuple[str, ...]] = ()
    keyword: ClassVar[str] = ""  # A single keyword, as older plugins declare it.
    routes: ClassVar[tuple[PluginRoute, ...]] = ()  # Keywords for parts of the items.
    refresh_minutes: ClassVar[int] = 15
    icon: ClassVar[str] = ""  # Image file for items without an icon of their own.
    round_icons: ClassVar[bool] = False  # Avatars: crop icons to circles, initials without one.
    # Regular expressions for typed terms that name one of this plugin's items, such
    # as (r"HCP-\w*",) for ticket keys. A term a pattern describes entirely is
    # matched in one piece instead of loosely; terms with a digit in them already
    # are. Case is ignored, and the other terms of the same query stay loose.
    literal_terms: ClassVar[tuple[str, ...]] = ()
    options: ClassVar[tuple[PluginOption, ...]] = ()

    @classmethod
    def suggested_keywords(cls) -> tuple[str, ...]:
        """The keywords the plugin suggests, whichever way it declared them."""
        return cls.keywords or ((cls.keyword,) if cls.keyword else ())

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
