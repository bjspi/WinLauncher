"""Plugin sources on the Plugins settings page: what they offer, and which ones to ask.

Remote plugins are installed, updated and uninstalled at once, on the source
worker. Local plugins are activated: an activation is an edit of the settings,
saved with the others, and loads the plugin from where it lies at the next start.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.catalog.model import path_key
from winlauncher.platform.git import checkout_root
from winlauncher.plugins.config import source_key
from winlauncher.plugins.packages import install_command
from winlauncher.plugins.sources import InstallState, Offer, Source, SourceKind, source_kind
from winlauncher.releases import installation
from winlauncher.settings import Settings
from winlauncher.ui.messages import ERROR, PRIMARY, SECONDARY, SUCCESS, ask, message, show_message
from winlauncher.ui.plugin_sources import (
    CHECK_INTERVAL_HOURS,
    SourceAction,
    SourceOutcome,
    SourceWatcher,
)
from winlauncher.ui.settings import _split_lines
from winlauncher.ui.widgets import Card, EntryList, line_icon, pick_path

SOURCE_EDITOR_HEIGHT = 72
RESTART_NOW = "Restart now"
INSTALL_TEXTS = {
    InstallState.AVAILABLE: "Not installed",
    InstallState.INSTALLED: "Installed",
    InstallState.UPDATE: "Update available",
    InstallState.TAKEN: "A plugin folder of this name exists",
}
NEXT_START = "from the next start"


@dataclass(frozen=True, slots=True)
class _Row:
    """One row of the list: an offer and the source offering it."""

    source: Source
    offer: Offer

    @property
    def key(self) -> tuple[str, str]:
        return source_key(self.source.location), self.offer.folder.name


class SourceSection(QObject):
    """The available plugins with their actions, and the list of sources."""

    def __init__(
        self,
        settings: Settings,
        watcher: SourceWatcher,
        loaded: Callable[[], set[str]],
        plugin_changed: Callable[[Path], None],
        restart: Callable[[], None] | None,
        parent: QObject,
    ):
        """`loaded` gives the path keys of the folders whose plugins are loaded now;
        `plugin_changed` is told the folder of a plugin that was installed or activated;
        `restart` saves the settings and restarts WinLauncher."""
        super().__init__(parent)  # Ends the watcher connections with the dialog.
        self.watcher, self.loaded = watcher, loaded
        self.plugin_changed, self.restart = plugin_changed, restart
        self.active = list(settings.active_plugins)  # Edited with the other settings.
        self.rows: list[_Row] = []
        self.sections: list[QWidget] = [self._offers_card()]
        self.sources_card = self._sources_card(settings)
        watcher.changed.connect(self.show_sources)
        watcher.finished.connect(self.show_outcome)
        self.show_sources()

    def _offers_card(self) -> Card:
        self.list = EntryList("Available plugins")
        self.list.currentRowChanged.connect(self.show_selected)
        self.details = QLabel()
        self.details.setObjectName("rowDescription")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.main_button = QPushButton("Install")
        self.main_button.clicked.connect(self.run_main_action)
        self.uninstall_button = QPushButton("Uninstall")
        self.uninstall_button.clicked.connect(self.uninstall_selected)
        card = Card()
        card.add_block(
            "Available plugins",
            "Plugins of your sources below. Install one from a repository, or activate one "
            "from a folder on this computer; it turns on at the next start.",
            self.list,
        )
        actions = _row(self.main_button, self.uninstall_button)
        card.add_block("", "", _stacked(self.details, actions))
        return card

    def _sources_card(self, settings: Settings) -> Card:
        self.locations = QPlainTextEdit("\n".join(settings.plugin_sources))
        self.locations.setAccessibleName("Plugin sources")
        self.locations.setFixedHeight(SOURCE_EDITOR_HEIGHT)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self.add_folder)
        self.check_button = QPushButton("Check now")
        self.check_button.clicked.connect(self.watcher.check)
        self.status = QLabel()
        self.status.setObjectName("rowDescription")
        self.status.setWordWrap(True)
        card = Card()
        card.add_block(
            "Plugin sources",
            "One per line. A git repository (https or SSH address) offers plugins to install; "
            "private ones use your saved git credentials. A folder offers its plugins to "
            "activate where they lie: a plugin folder, a folder of them, or one with a "
            f"catalog.toml. Checked every {CHECK_INTERVAL_HOURS} hours; save to check an "
            "edited list.",
            _stacked(self.locations, _row(add_folder, self.check_button), self.status),
        )
        return card

    def source_locations(self) -> tuple[str, ...]:
        return _split_lines(self.locations.toPlainText())

    def active_plugins(self) -> tuple[str, ...]:
        """The activated folders that still lie in a listed local source."""
        roots = [
            path_key(location)
            for location in self.source_locations()
            if source_kind(location) is SourceKind.LOCAL
        ]
        return tuple(folder for folder in self.active if _inside_any(path_key(folder), roots))

    def add_folder(self) -> None:
        if folder := pick_path(self.list.window(), "Choose a plugin source folder"):
            self.locations.appendPlainText(str(folder))

    # Showing ---------------------------------------------------------------------

    def show_sources(self) -> None:
        """Rebuild the list from the watcher, keeping the selected plugin selected."""
        selected = self._selected()
        self.rows = [
            _Row(source, offer) for source in self.watcher.sources for offer in source.offers
        ]
        self.list.blockSignals(True)
        self.list.clear_entries()
        for row in self.rows:
            self.list.add_entry(_offer_icon(row.offer), self._row_text(row))
        keys = [row.key for row in self.rows]
        index = keys.index(selected.key) if selected is not None and selected.key in keys else 0
        self.list.setCurrentRow(index if self.rows else -1)
        self.list.blockSignals(False)
        self.status.setText(self._status_text())
        self.check_button.setEnabled(not self.watcher.checking)
        self.show_selected()

    def show_selected(self, _index: int = -1) -> None:
        row = self._selected()
        self.uninstall_button.setVisible(row is not None and row.offer.kind is SourceKind.REMOTE)
        if row is None:
            self.details.setText("")
            self.main_button.setEnabled(False)
            return
        offer, idle = row.offer, not self.watcher.working
        self.details.setText(_details(offer))
        if offer.kind is SourceKind.LOCAL:
            self.main_button.setText("Deactivate" if self._is_active(offer) else "Activate")
            self.main_button.setEnabled(idle)
            return
        update = offer.install is InstallState.UPDATE
        self.main_button.setText(f"Update to {offer.version}" if update else "Install")
        self.main_button.setEnabled(
            idle and offer.install in (InstallState.AVAILABLE, InstallState.UPDATE)
        )
        self.uninstall_button.setEnabled(
            idle and offer.install in (InstallState.INSTALLED, InstallState.UPDATE)
        )

    def _row_text(self, row: _Row) -> str:
        offer = row.offer
        version = f"  {offer.version}" if offer.version else ""
        return f"{offer.name}{version}\n{row.source.title}  ·  {self._state_text(offer)}"

    def _state_text(self, offer: Offer) -> str:
        """The offer's state, and what changes at the next start."""
        if offer.kind is SourceKind.LOCAL:
            active, loaded = self._is_active(offer), self._is_loaded(offer.folder)
            if active:
                return "Active" if loaded else f"Active {NEXT_START}"
            return f"Off {NEXT_START}" if loaded else "Not active"
        text = INSTALL_TEXTS[offer.install]
        if offer.install is InstallState.UPDATE and offer.installed_version:
            text = f"Update from {offer.installed_version}"
        installed = offer.install in (InstallState.INSTALLED, InstallState.UPDATE)
        if installed and not self._is_loaded(self.watcher.installed_folder(offer)):
            text = f"{text}, loads {NEXT_START}"
        return text

    def _status_text(self) -> str:
        if self.watcher.checking:
            parts = ["Checking the sources…"]
        elif self.watcher.checked_at is not None:
            checked = datetime.fromtimestamp(self.watcher.checked_at).strftime("%H:%M")
            parts = [f"Checked at {checked}."]
        else:
            parts = ["Not checked yet."]
        if not self.watcher.locations():
            parts.append("No plugin source is listed.")
        for source in self.watcher.sources:
            if source.error:
                parts.append(f"{source.title}: {source.error}")
            parts.extend(f"{source.title}: {problem}" for problem in source.problems)
        return " ".join(parts)

    def _selected(self) -> _Row | None:
        index = self.list.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def _is_active(self, offer: Offer) -> bool:
        return path_key(offer.folder) in {path_key(folder) for folder in self.active}

    def _is_loaded(self, folder: Path) -> bool:
        return path_key(folder) in self.loaded()

    # Acting ----------------------------------------------------------------------

    def run_main_action(self) -> None:
        row = self._selected()
        if row is None:
            return
        offer = row.offer
        if offer.kind is SourceKind.REMOTE:
            self._install(row)
        elif self._is_active(offer):
            self._deactivate(offer)
        else:
            self._activate(row)

    def _install(self, row: _Row) -> None:
        offer = row.offer
        verb = "Update" if offer.install is InstallState.UPDATE else "Install"
        if self._confirm(f"{verb} {offer.name}?", row, verb):
            self.watcher.install(offer)

    def _activate(self, row: _Row) -> None:
        if self._confirm(f"Activate {row.offer.name}?", row, "Activate"):
            self.watcher.activate(row.offer)

    def _deactivate(self, offer: Offer) -> None:
        self.active = [
            folder for folder in self.active if path_key(folder) != path_key(offer.folder)
        ]
        self.show_sources()

    def uninstall_selected(self) -> None:
        row = self._selected()
        if row is None:
            return
        name = row.offer.name
        if ask(
            self.list.window(),
            f"Uninstall {name}?",
            f"The folder of {name} is deleted; its settings and data are kept, so installing "
            "it again brings them back.",
            "Uninstall",
        ):
            self.watcher.uninstall(row.offer)

    def _confirm(self, title: str, row: _Row, action: str) -> bool:
        offer = row.offer
        text = (
            f"{offer.name} {offer.version} from {row.source.title} runs as Python code with "
            "your permissions. Use only plugins you trust."
        )
        if offer.plugin.manifest.requires:
            text += " Its packages are installed first: " + ", ".join(
                offer.plugin.manifest.requires
            )
        return ask(self.list.window(), title, text, action)

    def show_outcome(self, outcome: SourceOutcome) -> None:
        if outcome.failed:
            message(self.list.window(), "That did not work", outcome.error, ERROR)
            return
        if outcome.action is SourceAction.ACTIVATE and not self._is_active(outcome.offer):
            self.active.append(str(outcome.offer.folder))
        if outcome.action is not SourceAction.UNINSTALL:
            self.plugin_changed(outcome.loads_from)
        self.show_sources()
        self._offer_restart(_outcome_text(outcome))

    def _offer_restart(self, text: str) -> None:
        window = self.list.window()
        if self.restart is None:
            message(window, "Plugins changed", text, SUCCESS)
            return
        answer = show_message(
            window,
            "Plugins changed",
            f"{text} Restart now? Your settings are saved first.",
            [("Later", SECONDARY), (RESTART_NOW, PRIMARY)],
            SUCCESS,
        )
        if answer == RESTART_NOW:
            self.restart()


def _outcome_text(outcome: SourceOutcome) -> str:
    name = outcome.offer.name
    match outcome.action:
        case SourceAction.INSTALL:
            return f"{name} {outcome.offer.version} is installed and is on from the next start."
        case SourceAction.UNINSTALL:
            return f"{name} is uninstalled; it stays loaded until the next start."
        case SourceAction.ACTIVATE:
            return f"{name} is activated and is on from the next start."


def _offer_icon(offer: Offer) -> QIcon:
    """The icon the plugin's manifest names, else the generic plug."""
    icon = QIcon(str(offer.plugin.icon)) if offer.plugin.icon is not None else QIcon()
    return icon if not icon.isNull() else line_icon("plug")


def _details(offer: Offer) -> str:
    """The description, the packages it needs, and why it cannot be installed if so."""
    manifest = offer.plugin.manifest
    parts = [manifest.description] if manifest.description else []
    if manifest.requires:
        command = install_command(installation(checkout_root()), manifest.requires)
        parts.append(f"Needs Python packages, installed along with it:\n{command}")
    if offer.kind is SourceKind.LOCAL:
        parts.append(f"Loads from {offer.folder}")
    if offer.install is InstallState.TAKEN:
        parts.append(
            f"Your plugin folder already holds a {offer.folder.name} folder that did not come "
            "from this source, so it is left alone."
        )
    return "\n\n".join(parts)


def _inside_any(folder_key: str, root_keys: list[str]) -> bool:
    return any(
        folder_key == root or folder_key.startswith(root.rstrip("/") + "/") for root in root_keys
    )


def _row(*buttons: QPushButton) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for button in buttons:
        layout.addWidget(button)
    layout.addStretch()
    return row


def _stacked(*widgets: QWidget) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for widget in widgets:
        layout.addWidget(widget)
    return box
