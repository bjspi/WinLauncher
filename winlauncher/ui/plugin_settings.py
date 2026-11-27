"""The plugin pages of Settings: getting plugins, and one page per plugin to set it up."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from winlauncher.catalog.model import path_key
from winlauncher.plugins.api import OptionValue, Plugin, PluginCommand, PluginOption
from winlauncher.plugins.budget import SLOW_SEARCHES_IN_A_ROW
from winlauncher.plugins.config import REFRESH_RANGE_MINUTES, PluginConfig, PluginCopy, copy_id
from winlauncher.plugins.folders import ManifestError, OfferedPlugin, read_manifest
from winlauncher.plugins.registry import PluginSource
from winlauncher.settings import BOOST_RANGE, PLUGIN_TIME_LIMIT_RANGE_MS, Settings
from winlauncher.ui.messages import (
    ERROR,
    INFO,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    WARNING,
    ask,
    message,
    show_message,
)
from winlauncher.ui.option_editors import option_editor
from winlauncher.ui.plugin_sources import SourceWatcher
from winlauncher.ui.plugins import PluginScheduler
from winlauncher.ui.settings import _note, _split_commas, _with_unit
from winlauncher.ui.source_settings import SourceSection
from winlauncher.ui.widgets import (
    ButtonRow,
    Card,
    NumberField,
    ToggleSwitch,
    breakable_path,
    line_icon,
    section_title,
    sized_number,
)

KEYWORD_FIELD_WIDTH = 160  # Room for a few keywords, separated by commas.
RESTART_NOW = "Restart now"


class PluginCard(Card):
    """The settings of one plugin; `config` reads them back."""

    def __init__(
        self,
        plugin: Plugin,
        config: PluginConfig,
        scheduler: PluginScheduler,
        edited_settings: Callable[[], Settings],
        add_copy: Callable[[], None],
        remove_copy: Callable[[], None],
    ):
        """`add_copy` adds another instance of the plugin; `remove_copy` removes this copy."""
        super().__init__(plugin.label)
        self.plugin, self.scheduler = plugin, scheduler
        self.edited_settings = edited_settings  # Raises ValueError for invalid edits.
        self.original = config
        self.copy = scheduler.host.copy(plugin.id)  # None unless it copies another plugin.
        self.enabled = ToggleSwitch(f"Use the {plugin.label} plugin")
        self.enabled.setChecked(config.enabled)
        source = scheduler.host.source(plugin.id)
        self.add_header(plugin_icon(plugin, source), plugin.label, plugin.description, self.enabled)
        version = QLabel(f"Version {source.version}" if source.version else "No version")
        version.setObjectName("unit")
        self.add_row("Installed from", _source_location(source), version)
        self.name: QLineEdit | None = None
        if self.copy is not None:
            base = scheduler.host.plugin(self.copy.of)
            self.name = QLineEdit(self.copy.label)
            self.name.setFixedWidth(KEYWORD_FIELD_WIDTH)
            self.name.setAccessibleName(f"Name of the copy {plugin.label}")
            self.add_row(
                "Name",
                f"A copy of {base.label if base else self.copy.of} with settings and items of "
                "its own. Its name shows before each item; a new one from the next start.",
                self.name,
            )
        self.pending_copies = QLabel()
        self.pending_copies.setObjectName("rowDescription")
        self.pending_copies.setWordWrap(True)
        self.pending_row = self.add_row("Copies", "", self.pending_copies)
        for other in scheduler.host.shadowed(plugin.id):
            skipped = QLabel(f"Version {other.version}" if other.version else "No version")
            skipped.setObjectName("unit")
            self.add_row(
                "Skipped copy",
                f"{_source_location(other)} is not loaded: the copy above comes first. "
                "Remove one of them to use the other.",
                skipped,
            )
        self.keywords = _keywords_field(config.keywords, f"{plugin.label} keywords")
        example = config.keywords[0] if config.keywords else "kw"
        self.add_row(
            "Keywords",
            f"Typed first, one puts {plugin.label} on top, such as “{example} anna”; your "
            f"files are still searched. “@{example} anna” answers from {plugin.label} alone, "
            "and a keyword by itself lists the most recent items. Separate several with commas.",
            self.keywords,
        )
        self.route_keywords: dict[str, QLineEdit] = {}
        for route in plugin.routes:
            words = config.routes.get(route.id, route.keywords)
            field = _keywords_field(words, f"{route.label} keywords")
            self.route_keywords[route.id] = field
            description = route.description or f"Typed first, it searches only “{route.label}”."
            self.add_row(f"Keywords for {route.label}", description, field)
        self.in_search = ToggleSwitch(f"{plugin.label} items in every search")
        self.in_search.setChecked(config.in_search)
        self.add_row(
            "In every search", "Matching items also appear among files and apps.", self.in_search
        )
        self.refresh_minutes = sized_number(
            NumberField(REFRESH_RANGE_MINUTES, config.refresh_minutes, "minutes")
        )
        self.refresh_minutes.setAccessibleName(f"{plugin.label} refresh interval in minutes")
        self.add_row(
            "Refresh every",
            "Runs in the background; searches use the last list meanwhile.",
            _with_unit(self.refresh_minutes, "min"),
        )
        self.boost = sized_number(NumberField(BOOST_RANGE, config.boost))
        self.boost.setAccessibleName(f"{plugin.label} ranking boost")
        self.add_row("Boost", "Lifts (up to 100) or lowers its items against files.", self.boost)
        self.options: dict[str, Callable[[], object]] = {}
        for option in plugin.options:
            self._add_option(option, option.value(config.options.get(option.key)))
        self.status = QLabel()
        self.status.setObjectName("rowDescription")
        self.status.setWordWrap(True)
        # They wrap, so a plugin with many commands fits a narrow window.
        buttons = ButtonRow()
        self.refresh_button = buttons.add(QPushButton("Refresh now"))
        self.refresh_button.clicked.connect(lambda: scheduler.refresh(plugin.id))
        self.command_buttons: list[QPushButton] = []
        for command in scheduler.host.commands(plugin.id):
            button = QPushButton(command.label)
            button.clicked.connect(partial(self._run, command))
            buttons.add(button)
            self.command_buttons.append(button)
        if self.copy is None:
            self.copy_button = QPushButton("Add copy")
            self.copy_button.setToolTip(
                f"Another {plugin.label} with its own keywords and settings, such as for a "
                "second account or server."
            )
            self.copy_button.clicked.connect(add_copy)
        else:
            self.copy_button = QPushButton("Remove copy")
            self.copy_button.clicked.connect(remove_copy)
        buttons.add(self.copy_button)
        self.add_block("Status", "", buttons)
        self.add_block("", "", self.status)
        self.enabled.toggled.connect(self._show_details)
        self._show_details()
        self.show_status()

    def _show_details(self) -> None:
        """A plugin that is off shows its header only."""
        self.show_details(self.enabled.isChecked())
        if not self.pending_copies.text():
            self.pending_row.setVisible(False)

    def show_pending_copies(self, labels: list[str]) -> None:
        """Name the copies that load from the next start."""
        self.pending_copies.setText(
            f"{', '.join(labels)} {'loads' if len(labels) == 1 else 'load'} from the next start."
            if labels
            else ""
        )
        self._show_details()

    def edited_copy(self) -> PluginCopy | None:
        """This copy with the edited name; None for a plugin that is no copy."""
        if self.copy is None or self.name is None:
            return None
        return replace(self.copy, label=self.name.text().strip())

    def _add_option(self, option: PluginOption, value: OptionValue) -> None:
        editor = option_editor(option, value)
        self.options[option.key] = editor.read
        self.add_row(option.label, option.description, editor.control)

    def config(self) -> PluginConfig:
        options = dict(self.original.options)
        options.update({key: read() for key, read in self.options.items()})
        return PluginConfig(
            self.plugin.id,
            enabled=self.enabled.isChecked(),
            keywords=_split_commas(self.keywords.text()),
            routes={
                route: _split_commas(field.text()) for route, field in self.route_keywords.items()
            },
            in_search=self.in_search.isChecked(),
            refresh_minutes=self.refresh_minutes.value(),
            boost=self.boost.value(),
            options=options,
        )

    def show_status(self) -> None:
        state = self.scheduler.host.state(self.plugin.id)
        refreshing = self.scheduler.is_refreshing(self.plugin.id)
        parts = []
        if state.refreshed is not None:
            when = datetime.fromtimestamp(state.refreshed).strftime("%H:%M")
            day = (
                ""
                if time.time() - state.refreshed < 86400
                else " on " + datetime.fromtimestamp(state.refreshed).strftime("%Y-%m-%d")
            )
            parts.append(f"{len(state.items):,} items, listed at {when}{day}.")
        if refreshing:
            parts.append("Refreshing…")
        elif self.scheduler.paused_offline:
            parts.append("Automatic refreshes paused: no internet. Refresh now still works.")
        elif state.error:
            parts.append(state.error)
        elif state.refreshed is None:
            parts.append("Not listed yet. Turn it on, then use Refresh now.")
        parts.extend(self._search_status())
        self.status.setText(" ".join(parts))
        saved = self.scheduler.host.config(self.plugin.id, self.scheduler.settings().plugins)
        self.refresh_button.setEnabled(not refreshing and saved.enabled)

    def _search_status(self) -> list[str]:
        """How long its last search took, and whether it is too slow for every search."""
        record = self.scheduler.host.search_record(self.plugin.id)
        parts = []
        if record.last_ms is not None:
            parts.append(f"Its last search took {record.last_ms:.0f} ms.")
        if record.left_out:
            parts.append(
                "Too slow for every search, so only its keywords search it until it lists "
                "new items."
            )
        return parts

    def _run(self, command: PluginCommand) -> None:
        try:
            settings = self.edited_settings()
        except ValueError as exc:
            message(self.window(), "Check your settings", str(exc), ERROR)
            return
        for button in self.command_buttons:
            button.setEnabled(False)
        self.scheduler.run_command(
            self.plugin.id, command, settings, self.window(), self._command_done
        )

    def _command_done(self, text: str | None, failed: bool) -> None:
        for button in self.command_buttons:
            button.setEnabled(True)
        if text:
            title = "That did not work" if failed else self.plugin.label
            message(self.window(), title, text, ERROR if failed else INFO)
        self.show_status()


class PluginPages(QObject):
    """The plugin pages of the Settings dialog.

    `install_sections` installs plugins and lists where they come from;
    `settings_sections` holds what all plugins share, and each card of `cards`
    gets a page of its own below it, so setting plugins up stays apart from
    getting them.
    """

    # Copies were added or removed: an edit no widget reports.
    copies_changed = Signal()

    def __init__(
        self,
        settings: Settings,
        scheduler: PluginScheduler,
        current: Callable[[], Settings],
        data_folder: str,
        parent: QObject,
        sources: SourceWatcher | None = None,
        restart: Callable[[], None] | None = None,
    ):
        super().__init__(parent)  # Ends the status connection with the dialog.
        self.original = settings
        self.current = current  # The dialog's other edits, for commands run before saving.
        host = self.host = scheduler.host
        self.restart = restart
        self.copies = list(settings.plugin_copies)  # Edited with the other settings.
        self.removed: set[str] = set()  # Ids of copies removed in this dialog.
        self.cards = [
            PluginCard(
                plugin,
                host.config(plugin.id, settings.plugins),
                scheduler,
                self.edited_settings,
                partial(self.add_copy, plugin),
                partial(self.remove_copy, plugin.id),
            )
            for plugin in host.plugins
        ]
        self._show_pending_copies()
        self.data_folder = Path(data_folder) if data_folder else None
        self.source_section = (
            SourceSection(settings, sources, self.loaded_folders, self.turn_on, restart, self)
            if sources is not None
            else None
        )
        self.install_sections = self._install_sections()
        self.time_limit = sized_number(
            NumberField(PLUGIN_TIME_LIMIT_RANGE_MS, settings.plugin_time_limit_ms, "ms")
        )
        self.time_limit.setAccessibleName("Plugin time limit per search in milliseconds")
        shared = Card()
        self.pause_offline = ToggleSwitch("Pause automatic plugin refreshes without internet")
        self.pause_offline.setChecked(settings.plugins_pause_offline)
        shared.add_row(
            "Pause plugins without internet",
            "Pauses automatic refreshes and resumes when internet returns. Cached items and "
            "manual actions stay available. Leave off for local or VPN-only services.",
            self.pause_offline,
        )
        self.internet_status = QLabel()
        self.internet_status.setObjectName("rowDescription")
        self.internet_status.setWordWrap(True)
        shared.add_row("Internet connection", "Checked every 15 seconds.", self.internet_status)
        shared.add_row(
            "Time limit per search",
            "Plugins search in the background, yet a slow one still makes typing lag. A "
            f"plugin over this limit in {SLOW_SEARCHES_IN_A_ROW} searches in a row is left "
            "out of ordinary searches until it lists new items; its keywords still search "
            "it. 0 turns the limit off.",
            _with_unit(self.time_limit, "ms"),
        )
        self.settings_sections: list[QWidget] = [
            section_title("All plugins"),
            shared,
            _note(
                "Each plugin has a page of its own, listed below Plugin settings at the left."
                if self.cards
                else "No plugin is loaded. Install or activate one under Plugins; it loads at "
                "the next start."
            ),
        ]
        scheduler.status_changed.connect(self.show_status)
        self.show_status()

    def _install_sections(self) -> list[QWidget]:
        sections: list[QWidget] = []
        if self.source_section is not None:
            sections.extend(self.source_section.sections)
        if self.host.problems:
            sections.append(_note("Not loaded: " + " ".join(self.host.problems)))
        if self.source_section is not None:
            sections.append(section_title("Where plugins come from"))
            sections.append(self.source_section.sources_card)
        if self.data_folder is not None:
            # Below the long path, which would otherwise leave the button no room.
            buttons = ButtonRow()
            buttons.add(QPushButton("Open plugin folder")).clicked.connect(self.open_folder)
            folder = Card()
            folder.add_block(
                "Plugin folder",
                f"{breakable_path(self.data_folder)}\nInstalled plugins lie here; a plugin folder you copy in "
                "loads as well.",
                buttons,
            )
            sections.append(folder)
        sections.append(
            _note(
                "Plugins are loaded once at start, so installing, activating or removing one "
                "takes effect after a restart. Writing your own is documented in docs/plugins.md."
            )
        )
        return sections

    def add_copy(self, plugin: Plugin) -> None:
        """Add another instance of `plugin`, which loads from the next start."""
        taken = {copy.id for copy in self.copies} | {loaded.id for loaded in self.host.plugins}
        number = 2
        while copy_id(plugin.id, number) in taken:
            number += 1
        copy = PluginCopy(copy_id(plugin.id, number), plugin.id, f"{plugin.label} {number}")
        self.copies.append(copy)
        self._show_pending_copies()
        self.copies_changed.emit()
        text = (
            f"{copy.label} is added and is on from the next start, with keywords and settings "
            "of its own."
        )
        window = self.cards[0].window()
        if self.restart is None:
            message(window, "Plugin copy added", text, SUCCESS)
            return
        answer = show_message(
            window,
            "Plugin copy added",
            f"{text} Restart now? Your settings are saved first.",
            [("Later", SECONDARY), (RESTART_NOW, PRIMARY)],
            SUCCESS,
        )
        if answer == RESTART_NOW:
            self.restart()

    def remove_copy(self, plugin_id: str) -> None:
        """Remove the copy `plugin_id` with its settings; it stays loaded until a restart."""
        card = next(card for card in self.cards if card.plugin.id == plugin_id)
        if not ask(
            card.window(),
            "Remove plugin copy",
            f"Remove {card.plugin.label} with its settings and items? It stays loaded until "
            "the next start.",
            "Remove",
            kind=WARNING,
        ):
            return
        self.copies = [copy for copy in self.copies if copy.id != plugin_id]
        self.removed.add(plugin_id)
        card.hide()
        self.copies_changed.emit()

    def _show_pending_copies(self) -> None:
        for card in self.cards:
            card.show_pending_copies(
                [
                    copy.label
                    for copy in self.copies
                    if copy.of == card.plugin.id and self.host.plugin(copy.id) is None
                ]
            )

    def edited_copies(self) -> tuple[PluginCopy, ...]:
        """The configured copies, with the names edited on their cards."""
        edited = {
            copy.id: copy
            for card in self.cards
            if card.plugin.id not in self.removed and (copy := card.edited_copy()) is not None
        }
        return tuple(edited.get(copy.id, copy) for copy in self.copies)

    def loaded_folders(self) -> set[str]:
        """Path keys of the folders the loaded plugins came from."""
        folders = (self.host.source(card.plugin.id).folder for card in self.cards)
        return {path_key(folder) for folder in folders if folder is not None}

    def turn_on(self, folder: Path) -> None:
        """Turn on the loaded plugins of `folder`, which was just installed or activated."""
        for card in self.cards:
            loaded_from = self.host.source(card.plugin.id).folder
            if loaded_from is not None and path_key(loaded_from) == path_key(folder):
                card.enabled.setChecked(True)

    def open_folder(self) -> None:
        self.data_folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_folder)))

    def show_status(self) -> None:
        state = self.host.internet
        self.internet_status.setText(f"Internet: {state.status.value}.")
        for card in self.cards:
            card.show_status()

    def configs(self) -> tuple[PluginConfig, ...]:
        """The edited plugins, plus configured plugins that are not installed now.

        A removed copy's settings go with it.
        """
        shown = {card.plugin.id for card in self.cards}
        kept = tuple(config for config in self.original.plugins if config.id not in shown)
        edited = (card.config() for card in self.cards if card.plugin.id not in self.removed)
        return (*edited, *kept)

    def apply_to(self, settings: Settings) -> Settings:
        """`settings` with the edits of these pages: plugins, sources and activations."""
        edited = replace(
            settings,
            plugins=self.configs(),
            plugin_copies=self.edited_copies(),
            plugin_time_limit_ms=self.time_limit.value(),
            plugins_pause_offline=self.pause_offline.isChecked(),
        )
        if self.source_section is None:
            return edited
        return replace(
            edited,
            plugin_sources=self.source_section.source_locations(),
            active_plugins=self.source_section.active_plugins(),
        )

    def card(self, plugin_id: str) -> PluginCard:
        return next(card for card in self.cards if card.plugin.id == plugin_id)

    def edited_settings(self) -> Settings:
        return self.apply_to(self.current())


def _keywords_field(words: tuple[str, ...], accessible_name: str) -> QLineEdit:
    field = QLineEdit(", ".join(words))
    field.setFixedWidth(KEYWORD_FIELD_WIDTH)
    field.setAccessibleName(accessible_name)
    return field


def _source_location(source: PluginSource) -> str:
    """Where the plugin's code sits, for its settings card."""
    if source.folder is not None:
        return breakable_path(source.folder)
    if source.path is not None:
        return breakable_path(source.path)
    return f"Installed package {source.origin}"


def plugin_icon(plugin: Plugin, source: PluginSource) -> QIcon:
    """The manifest's icon, as under Available plugins; else the plugin's own, else a plug."""
    for path in (_manifest_icon(source), plugin.icon):
        if path and not (icon := QIcon(str(path))).isNull():
            return icon
    return line_icon("plug")


def _manifest_icon(source: PluginSource) -> Path | None:
    if source.folder is None:
        return None
    try:
        return OfferedPlugin(source.folder, read_manifest(source.folder)).icon
    except ManifestError:
        return None
