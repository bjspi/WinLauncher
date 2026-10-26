"""The Plugins settings page: one card per plugin with its options, status and commands."""

from __future__ import annotations

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
    QPushButton,
    QWidget,
)

from winlauncher.plugins.api import Plugin, PluginCommand, PluginOption
from winlauncher.plugins.config import REFRESH_RANGE_MINUTES, PluginConfig
from winlauncher.plugins.registry import PluginSource
from winlauncher.settings import BOOST_RANGE, Settings
from winlauncher.ui.catalog_settings import CatalogSection
from winlauncher.ui.messages import ERROR, INFO, message
from winlauncher.ui.plugin_catalogs import CatalogWatcher
from winlauncher.ui.plugins import PluginScheduler
from winlauncher.ui.settings import NumberField, PathList, _note, _sized, _with_unit
from winlauncher.ui.widgets import Card, EntryList, ToggleSwitch, line_icon, section_title

KEYWORD_FIELD_WIDTH = 88
TEXT_OPTION_WIDTH = 220


class PluginCard(Card):
    """The settings of one plugin; `config` reads them back."""

    def __init__(
        self,
        plugin: Plugin,
        config: PluginConfig,
        scheduler: PluginScheduler,
        edited_settings: Callable[[], Settings],
    ):
        super().__init__(plugin.label)
        self.plugin, self.scheduler = plugin, scheduler
        self.edited_settings = edited_settings  # Raises ValueError for invalid edits.
        self.original = config
        self.enabled = ToggleSwitch(f"Use the {plugin.label} plugin")
        self.enabled.setChecked(config.enabled)
        self.add_row(plugin.label, plugin.description, self.enabled)
        source = scheduler.host.source(plugin.id)
        version = QLabel(f"Version {source.version}" if source.version else "No version")
        version.setObjectName("unit")
        self.add_row("Installed from", _source_location(source), version)
        self.keyword = QLineEdit(config.keyword)
        self.keyword.setFixedWidth(KEYWORD_FIELD_WIDTH)
        self.keyword.setAccessibleName(f"{plugin.label} keyword")
        self.add_row(
            "Keyword",
            f"Typed first it puts {plugin.label} on top, such as “{config.keyword or 'kw'} anna”; "
            f"your files are still searched. “@{config.keyword or 'kw'} anna” answers from "
            f"{plugin.label} alone, and the keyword by itself lists the most recent items.",
            self.keyword,
        )
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
        self.show_status()

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
            keyword=self.keyword.text().strip(),
            in_search=self.in_search.isChecked(),
            refresh_minutes=self.refresh_minutes.value(),
            boost=self.boost.value(),
            options=options,
        )

    def summary(self) -> str:
        """One line for the plugin list: on or off, keyword and item count."""
        saved = self.scheduler.host.config(self.plugin.id, self.scheduler.settings().plugins)
        if not saved.enabled:
            return "Off"
        state = self.scheduler.host.state(self.plugin.id)
        parts = ["On"]
        if saved.keyword:
            parts.append(f"keyword {saved.keyword}")
        if state.needs_setup:
            parts.append("needs setup")
        elif state.refreshed is not None:
            parts.append(f"{len(state.items):,} items")
        return "  ·  ".join(parts)

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
        elif state.error:
            parts.append(state.error)
        elif state.refreshed is None:
            parts.append("Not listed yet. Turn it on and save, then use Refresh now.")
        self.status.setText(" ".join(parts))
        saved = self.scheduler.host.config(self.plugin.id, self.scheduler.settings().plugins)
        self.refresh_button.setEnabled(not refreshing and saved.enabled)

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


class PluginPage(QObject):
    """The plugin cards of the Settings dialog; its `sections` go on the page."""

    def __init__(
        self,
        settings: Settings,
        scheduler: PluginScheduler,
        current: Callable[[], Settings],
        data_folder: str,
        parent: QObject,
        catalogs: CatalogWatcher | None = None,
    ):
        super().__init__(parent)  # Ends the status connection with the dialog.
        self.original = settings
        self.current = current  # The dialog's other edits, for commands run before saving.
        host = scheduler.host
        self.cards = [
            PluginCard(
                plugin, host.config(plugin.id, settings.plugins), scheduler, self.edited_settings
            )
            for plugin in host.plugins
        ]
        self.data_folder = Path(data_folder) if data_folder else None
        self.list = EntryList("Installed plugins")
        for card in self.cards:
            self.list.add_entry(_plugin_icon(card.plugin), "")
        self.list.currentRowChanged.connect(self.select)
        overview = Card()
        overview.add_block(
            "Installed plugins",
            "Choose a plugin to set it up. Install more from a catalog below, or copy a "
            "plugin folder into the plugin folder.",
            self.list,
        )
        if self.data_folder is not None:
            folder_button = QPushButton("Open plugin folder")
            folder_button.clicked.connect(self.open_folder)
            overview.add_row("Plugin folder", str(self.data_folder), folder_button)
        self.sections: list[QWidget] = [overview]
        if host.problems:
            self.sections.append(_note("Not loaded: " + " ".join(host.problems)))
        self.sections.extend(self.cards)
        self.catalog_section = (
            CatalogSection(settings, catalogs, self) if catalogs is not None else None
        )
        if self.catalog_section is not None:
            self.sections.extend(self.catalog_section.sections)
        self.sections.append(section_title("Where plugins come from"))
        self.sections.append(self._folders_card(settings))
        if self.catalog_section is not None:
            self.sections.append(self.catalog_section.catalogs_card)
        self.sections.append(
            _note(
                "Plugins are loaded once at start: save, then restart WinLauncher from the "
                "tray to pick up a new folder. Writing your own is documented in docs/plugins.md."
            )
        )
        scheduler.status_changed.connect(self.show_status)
        self.show_status()
        if self.cards:
            self.list.setCurrentRow(0)

    def _folders_card(self, settings: Settings) -> Card:
        """The folders searched beside the data directory's own, such as a cloned repo."""
        self.folders = PathList(settings.plugin_paths, "Plugin folders")
        card = Card()
        card.add_block(
            "Plugin folders",
            "Searched after the folder above, each holding one subfolder per plugin "
            "(plugin.py beside an optional plugin.toml). Point one at a cloned plugin "
            "repository; keep private plugins in a folder you never push.",
            self.folders,
        )
        return card

    def select(self, row: int) -> None:
        """Show the settings of the plugin in `row` only."""
        for index, card in enumerate(self.cards):
            card.setVisible(index == row)

    def open_folder(self) -> None:
        self.data_folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_folder)))

    def show_status(self) -> None:
        for row, card in enumerate(self.cards):
            card.show_status()
            self.list.item(row).setText(f"{card.plugin.label}\n{card.summary()}")

    def configs(self) -> tuple[PluginConfig, ...]:
        """The edited plugins, plus configured plugins that are not installed now."""
        shown = {card.plugin.id for card in self.cards}
        kept = tuple(config for config in self.original.plugins if config.id not in shown)
        return (*(card.config() for card in self.cards), *kept)

    def apply_to(self, settings: Settings) -> Settings:
        """`settings` with the edits of this page: plugins, plugin folders and catalogs."""
        edited = replace(settings, plugins=self.configs(), plugin_paths=self.folders.paths())
        if self.catalog_section is None:
            return edited
        return replace(edited, plugin_catalogs=self.catalog_section.catalog_urls())

    def edited_settings(self) -> Settings:
        return self.apply_to(self.current())


def _source_location(source: PluginSource) -> str:
    """Where the plugin's code sits, for its settings card."""
    if source.folder is not None:
        return str(source.folder)
    if source.path is not None:
        return str(source.path)
    return f"Installed package {source.origin}"


def _plugin_icon(plugin: Plugin) -> QIcon:
    icon = QIcon(plugin.icon) if plugin.icon else QIcon()
    return icon if not icon.isNull() else line_icon("plug")
