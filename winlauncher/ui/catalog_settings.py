"""Plugin catalogs on the Plugins settings page: what they offer, and which ones to ask."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.platform.git import checkout_root
from winlauncher.plugins.catalog import Catalog, CatalogPlugin, InstallState
from winlauncher.plugins.config import catalog_key
from winlauncher.releases import installation, requirements_command
from winlauncher.settings import Settings
from winlauncher.ui.messages import ERROR, SUCCESS, ask, message
from winlauncher.ui.plugin_catalogs import CHECK_INTERVAL_HOURS, CatalogOutcome, CatalogWatcher
from winlauncher.ui.settings import _split_lines
from winlauncher.ui.widgets import Card, EntryList, line_icon, section_title

CATALOG_EDITOR_HEIGHT = 72
STATE_TEXTS = {
    InstallState.AVAILABLE: "Not installed",
    InstallState.INSTALLED: "Installed",
    InstallState.UPDATE: "Update available",
    InstallState.TAKEN: "A plugin folder of this name exists",
}


@dataclass(frozen=True, slots=True)
class _Offer:
    """One row of the list: a plugin and the catalog offering it."""

    catalog: Catalog
    plugin: CatalogPlugin

    @property
    def key(self) -> tuple[str, str]:
        return catalog_key(self.catalog.url), self.plugin.folder


class CatalogSection(QObject):
    """The available plugins with Install, Update and Remove, and the catalog list."""

    def __init__(self, settings: Settings, watcher: CatalogWatcher, parent: QObject):
        super().__init__(parent)  # Ends the watcher connections with the dialog.
        self.watcher = watcher
        self.offers: list[_Offer] = []
        self.sections: list[QWidget] = [
            section_title("Install plugins"),
            self._available_card(),
        ]
        self.catalogs_card = self._catalogs_card(settings)
        watcher.changed.connect(self.show_catalogs)
        watcher.finished.connect(self.show_outcome)
        self.show_catalogs()

    def _available_card(self) -> Card:
        self.list = EntryList("Available plugins")
        self.list.currentRowChanged.connect(self.show_selected)
        self.details = QLabel()
        self.details.setObjectName("rowDescription")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.install_button = QPushButton("Install")
        self.install_button.clicked.connect(self.install_selected)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected)
        self.status = QLabel()
        self.status.setObjectName("rowDescription")
        self.status.setWordWrap(True)
        self.check_button = QPushButton("Check now")
        self.check_button.clicked.connect(self.watcher.check)
        card = Card()
        card.add_block(
            "Available plugins",
            "Offered by your plugin catalogs. A plugin is installed into the plugin folder "
            "above and stays off until you turn it on.",
            self.list,
        )
        card.add_block(
            "", "", _stacked(self.details, _buttons(self.install_button, self.remove_button))
        )
        card.add_block("", "", _beside(self.status, self.check_button))
        return card

    def _catalogs_card(self, settings: Settings) -> Card:
        self.urls = QPlainTextEdit("\n".join(settings.plugin_catalogs))
        self.urls.setAccessibleName("Plugin catalogs")
        self.urls.setFixedHeight(CATALOG_EDITOR_HEIGHT)
        card = Card()
        card.add_block(
            "Plugin catalogs",
            "Git repositories with a catalog.toml, one per line: an https or SSH address, or "
            "a folder. Private ones work once git can reach them with your saved credentials. "
            f"Checked every {CHECK_INTERVAL_HOURS} hours; save to check an edited list.",
            self.urls,
        )
        return card

    def catalog_urls(self) -> tuple[str, ...]:
        return _split_lines(self.urls.toPlainText())

    # Showing ---------------------------------------------------------------------

    def show_catalogs(self) -> None:
        """Rebuild the list from the watcher, keeping the selected plugin selected."""
        selected = self._selected()
        self.offers = [
            _Offer(catalog, plugin)
            for catalog in self.watcher.catalogs
            for plugin in catalog.plugins
        ]
        self.list.blockSignals(True)
        self.list.clear_entries()
        for offer in self.offers:
            self.list.add_entry(line_icon("plug"), _row_text(offer))
        keys = [offer.key for offer in self.offers]
        row = keys.index(selected.key) if selected is not None and selected.key in keys else 0
        self.list.setCurrentRow(row if self.offers else -1)
        self.list.blockSignals(False)
        self.status.setText(self._status_text())
        self.check_button.setEnabled(not self.watcher.checking)
        self.show_selected()

    def show_selected(self, _row: int = -1) -> None:
        offer = self._selected()
        if offer is None:
            self.details.setText("")
            self.install_button.setEnabled(False)
            self.remove_button.setEnabled(False)
            return
        plugin, idle = offer.plugin, not self.watcher.working
        self.details.setText(_details(plugin))
        self.install_button.setText(
            f"Update to {plugin.version}" if plugin.state is InstallState.UPDATE else "Install"
        )
        installable = plugin.state in (InstallState.AVAILABLE, InstallState.UPDATE)
        removable = plugin.state in (InstallState.INSTALLED, InstallState.UPDATE)
        self.install_button.setEnabled(idle and installable)
        self.remove_button.setEnabled(idle and removable)

    def _status_text(self) -> str:
        if self.watcher.checking:
            parts = ["Checking the catalogs…"]
        elif self.watcher.checked_at is not None:
            checked = datetime.fromtimestamp(self.watcher.checked_at).strftime("%H:%M")
            parts = [f"Checked at {checked}."]
        else:
            parts = ["Not checked yet."]
        if not self.watcher.urls():
            parts.append("No catalog is configured.")
        for catalog in self.watcher.catalogs:
            if catalog.error:
                parts.append(f"{catalog.title}: {catalog.error}")
            parts.extend(f"{catalog.title}: {problem}" for problem in catalog.problems)
        return " ".join(parts)

    def _selected(self) -> _Offer | None:
        row = self.list.currentRow()
        return self.offers[row] if 0 <= row < len(self.offers) else None

    # Acting ----------------------------------------------------------------------

    def install_selected(self) -> None:
        offer = self._selected()
        if offer is None:
            return
        plugin = offer.plugin
        if ask(
            self.list.window(),
            f"Install {plugin.name}?",
            f"{plugin.name} {plugin.version} from {offer.catalog.title} runs as Python code "
            "with your permissions. Install only plugins you trust.",
            "Update" if plugin.state is InstallState.UPDATE else "Install",
        ):
            self.watcher.install(offer.catalog, plugin)

    def remove_selected(self) -> None:
        offer = self._selected()
        if offer is None:
            return
        name = offer.plugin.name
        if ask(
            self.list.window(),
            f"Remove {name}?",
            f"The folder of {name} is deleted; its settings and data are kept, so installing "
            "it again brings them back.",
            "Remove",
        ):
            self.watcher.remove(offer.catalog, offer.plugin)

    def show_outcome(self, outcome: CatalogOutcome) -> None:
        title = "That did not work" if outcome.failed else "Plugins changed"
        message(self.list.window(), title, outcome.text, ERROR if outcome.failed else SUCCESS)


def _row_text(offer: _Offer) -> str:
    plugin = offer.plugin
    version = f"  {plugin.version}" if plugin.version else ""
    state = STATE_TEXTS[plugin.state]
    if plugin.state is InstallState.UPDATE and plugin.installed_version:
        state = f"Update from {plugin.installed_version}"
    return f"{plugin.name}{version}\n{offer.catalog.title}  ·  {state}"


def _details(plugin: CatalogPlugin) -> str:
    """The description, what else it needs, and why it cannot be installed if so."""
    parts = [plugin.manifest.description] if plugin.manifest.description else []
    if plugin.manifest.requires:
        command = requirements_command(installation(checkout_root()), plugin.manifest.requires)
        parts.append(f"Needs Python packages beside WinLauncher; install them with:\n{command}")
    if plugin.state is InstallState.TAKEN:
        parts.append(
            f"Your plugin folder already holds a {plugin.folder} folder that did not come from "
            "this catalog, so it is left alone."
        )
    return "\n\n".join(parts)


def _buttons(*buttons: QPushButton) -> QWidget:
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


def _beside(text: QLabel, button: QPushButton) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    layout.addWidget(text, 1)
    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
    return row
