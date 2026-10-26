"""The plugin pages of Settings: getting plugins, and one card per plugin to set it up."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from winlauncher.catalog.model import path_key
from winlauncher.plugins.api import Plugin, PluginCommand, PluginOption
from winlauncher.plugins.budget import SLOW_SEARCHES_IN_A_ROW
from winlauncher.plugins.config import REFRESH_RANGE_MINUTES, PluginConfig
from winlauncher.plugins.folders import ManifestError, OfferedPlugin, read_manifest
from winlauncher.plugins.registry import PluginSource
from winlauncher.settings import BOOST_RANGE, PLUGIN_TIME_LIMIT_RANGE_MS, Settings
from winlauncher.ui.messages import ERROR, INFO, message
from winlauncher.ui.plugin_sources import SourceWatcher
from winlauncher.ui.plugins import PluginScheduler
from winlauncher.ui.settings import NumberField, _note, _sized, _split_commas, _with_unit
from winlauncher.ui.source_settings import SourceSection
from winlauncher.ui.widgets import Card, ToggleSwitch, line_icon, section_title

KEYWORD_FIELD_WIDTH = 160  # Room for a few keywords, separated by commas.
TEXT_OPTION_WIDTH = 220
TEXT_OPTION_HEIGHT = 96  # About five lines for a multiline option.
logger = logging.getLogger(__name__)


class UnfoldedCards:
    """The plugins whose card shows its details, remembered across starts.

    How the page looks is no setting: it is written at once, apart from Save and
    Cancel. A file that cannot be read or written only loses what was remembered.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.plugin_ids = self._read()

    def __contains__(self, plugin_id: str) -> bool:
        return plugin_id in self.plugin_ids

    def remember(self, plugin_id: str, unfolded: bool) -> None:
        if unfolded:
            self.plugin_ids.add(plugin_id)
        else:
            self.plugin_ids.discard(plugin_id)
        if self.path is None:
            return
        try:
            self.path.write_text(json.dumps(sorted(self.plugin_ids)), encoding="utf-8")
        except OSError as exc:
            logger.warning("Unfolded plugin cards could not be saved: %s", exc)

    def _read(self) -> set[str]:
        if self.path is None or not self.path.is_file():
            return set()
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Unfolded plugin cards could not be read: %s", exc)
            return set()
        if not isinstance(stored, list):
            return set()
        return {plugin_id for plugin_id in stored if isinstance(plugin_id, str)}


class PluginCard(Card):
    """The settings of one plugin; `config` reads them back."""

    def __init__(
        self,
        plugin: Plugin,
        config: PluginConfig,
        scheduler: PluginScheduler,
        edited_settings: Callable[[], Settings],
        unfolded: UnfoldedCards,
    ):
        super().__init__(plugin.label)
        self.plugin, self.scheduler = plugin, scheduler
        self.edited_settings = edited_settings  # Raises ValueError for invalid edits.
        self.original = config
        self.enabled = ToggleSwitch(f"Use the {plugin.label} plugin")
        self.enabled.setChecked(config.enabled)
        source = scheduler.host.source(plugin.id)
        self.fold = self.add_header(
            _plugin_icon(plugin, source), plugin.label, plugin.description, self.enabled
        )
        self.fold.setChecked(plugin.id in unfolded)
        self.fold.toggled.connect(partial(unfolded.remember, plugin.id))
        version = QLabel(f"Version {source.version}" if source.version else "No version")
        version.setObjectName("unit")
        self.add_row("Installed from", _source_location(source), version)
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
        self.refresh_minutes = _sized(
            NumberField(REFRESH_RANGE_MINUTES, config.refresh_minutes, "minutes")
        )
        self.refresh_minutes.setAccessibleName(f"{plugin.label} refresh interval in minutes")
        self.add_row(
            "Refresh every",
            "Runs in the background; searches use the last list meanwhile.",
            _with_unit(self.refresh_minutes, "min"),
        )
        self.boost = _sized(NumberField(BOOST_RANGE, config.boost))
        self.boost.setAccessibleName(f"{plugin.label} ranking boost")
        self.add_row("Boost", "Lifts (up to 100) or lowers its items against files.", self.boost)
        self.options: dict[str, Callable[[], object]] = {}
        for option in plugin.options:
            self._add_option(option, option.value(config.options.get(option.key)))
        self.status = QLabel()
        self.status.setObjectName("rowDescription")
        self.status.setWordWrap(True)
        actions = QWidget()
        buttons = QHBoxLayout(actions)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        self.refresh_button = QPushButton("Refresh now")
        self.refresh_button.clicked.connect(lambda: scheduler.refresh(plugin.id))
        buttons.addWidget(self.refresh_button)
        self.command_buttons: list[QPushButton] = []
        for command in scheduler.host.commands(plugin.id):
            button = QPushButton(command.label)
            button.clicked.connect(partial(self._run, command))
            buttons.addWidget(button)
            self.command_buttons.append(button)
        self.add_row("Status", "", actions)
        self.add_block("", "", self.status)
        self.enabled.toggled.connect(self._show_details)
        self.fold.toggled.connect(self._show_details)
        self._show_details()
        self.show_status()

    def _show_details(self) -> None:
        """Details show while unfolded; a plugin that is off shows its header only."""
        on = self.enabled.isChecked()
        self.fold.setEnabled(on)
        self.show_details(on and self.fold.isChecked())

    def _add_option(self, option: PluginOption, value: object) -> None:
        if isinstance(value, bool):
            control = ToggleSwitch(option.label)
            control.setChecked(value)
            self.options[option.key] = control.isChecked
            self.add_row(option.label, option.description, control)
        elif isinstance(value, int):
            bounds = option.bounds or (-(2**31), 2**31 - 1)
            field = _sized(NumberField(bounds, value))
            field.setAccessibleName(option.label)
            self.options[option.key] = field.value
            self.add_row(option.label, option.description, field)
        elif option.multiline:
            box = QPlainTextEdit(str(value))
            box.setFixedWidth(TEXT_OPTION_WIDTH)
            box.setFixedHeight(TEXT_OPTION_HEIGHT)
            box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            box.setAccessibleName(option.label)
            self.options[option.key] = box.toPlainText
            self.add_row(option.label, option.description, box)
        else:
            line = QLineEdit(str(value))
            line.setFixedWidth(TEXT_OPTION_WIDTH)
            line.setAccessibleName(option.label)
            self.options[option.key] = line.text
            self.add_row(option.label, option.description, line)

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
            parts.append("Not listed yet. Turn it on and save, then use Refresh now.")
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
    """The two plugin pages of the Settings dialog.

    `install_sections` installs plugins and lists where they come from;
    `settings_sections` holds one card per loaded plugin, so setting plugins up
    stays apart from getting them.
    """

    def __init__(
        self,
        settings: Settings,
        scheduler: PluginScheduler,
        current: Callable[[], Settings],
        data_folder: str,
        parent: QObject,
        sources: SourceWatcher | None = None,
        restart: Callable[[], None] | None = None,
        unfolded_file: Path | None = None,
    ):
        """`unfolded_file` remembers which cards show their details; None forgets it."""
        super().__init__(parent)  # Ends the status connection with the dialog.
        self.original = settings
        self.current = current  # The dialog's other edits, for commands run before saving.
        host = self.host = scheduler.host
        unfolded = UnfoldedCards(unfolded_file)
        self.cards = [
            PluginCard(
                plugin,
                host.config(plugin.id, settings.plugins),
                scheduler,
                self.edited_settings,
                unfolded,
            )
            for plugin in host.plugins
        ]
        self.data_folder = Path(data_folder) if data_folder else None
        self.source_section = (
            SourceSection(settings, sources, self.loaded_folders, self.turn_on, restart, self)
            if sources is not None
            else None
        )
        self.install_sections = self._install_sections()
        self.time_limit = _sized(
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
            section_title("Each plugin"),
        ]
        self.settings_sections.extend(
            self.cards
            or [
                _note(
                    "No plugin is loaded. Install or activate one under Plugins; it loads at "
                    "the next start."
                )
            ]
        )
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
            folder_button = QPushButton("Open plugin folder")
            folder_button.clicked.connect(self.open_folder)
            folder = Card()
            folder.add_row(
                "Plugin folder",
                f"{self.data_folder}\nInstalled plugins lie here; a plugin folder you copy in "
                "loads as well.",
                folder_button,
            )
            sections.append(folder)
        sections.append(
            _note(
                "Plugins are loaded once at start, so installing, activating or removing one "
                "takes effect after a restart. Writing your own is documented in docs/plugins.md."
            )
        )
        return sections

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
        """The edited plugins, plus configured plugins that are not installed now."""
        shown = {card.plugin.id for card in self.cards}
        kept = tuple(config for config in self.original.plugins if config.id not in shown)
        return (*(card.config() for card in self.cards), *kept)

    def apply_to(self, settings: Settings) -> Settings:
        """`settings` with the edits of these pages: plugins, sources and activations."""
        edited = replace(
            settings,
            plugins=self.configs(),
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
        return str(source.folder)
    if source.path is not None:
        return str(source.path)
    return f"Installed package {source.origin}"


def _plugin_icon(plugin: Plugin, source: PluginSource) -> QIcon:
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
