"""System tray icon and its context menu; it only emits user intents."""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from winlauncher import __version__
from winlauncher.ui.menus import Menu
from winlauncher.ui.theme import app_icon, colors, theme
from winlauncher.ui.widgets import line_icon

HEADER_ICON_SIZE = 30
SEARCH_SHORTCUT = "Ctrl+Alt+Space"
CHECK = None  # The icon of a checkable entry: its check mark, drawn when checked.


class _Entry(NamedTuple):
    text: str
    signal: str  # The TrayMenu signal the entry emits.
    key: str  # Triggers the entry while the menu is open.
    icon: str | None


# None separates groups. The letters follow WhisperTyper's tray menu where the
# entries match: K for Settings (Konfiguration), U, A, N and Q.
ENTRIES = (
    _Entry("Search", "search_requested", "s", "search"),
    _Entry("Refresh catalog", "refresh_requested", "r", "refresh"),
    _Entry("Index status…", "status_requested", "i", "activity"),
    None,
    _Entry("Settings…", "settings_requested", "k", "gear"),
    _Entry("Start with Windows", "autostart_toggled", "w", CHECK),
    _Entry("Center search bar", "centered_toggled", "c", CHECK),
    _Entry("Clear search history", "clear_history_requested", "h", "trash"),
    None,
    _Entry("Check for updates…", "update_requested", "u", "download"),
    _Entry("About WinLauncher", "about_requested", "a", "info"),
    None,
    _Entry("Restart WinLauncher", "restart_requested", "n", "restart"),
    _Entry("Quit WinLauncher", "quit_requested", "q", "quit"),
)


class TrayMenu(QObject):
    search_requested = Signal()
    refresh_requested = Signal()
    settings_requested = Signal()
    status_requested = Signal()
    clear_history_requested = Signal()
    quit_requested = Signal()
    restart_requested = Signal()
    update_requested = Signal()
    about_requested = Signal()
    autostart_toggled = Signal(bool)
    centered_toggled = Signal(bool)
    opening = Signal()

    def __init__(self, parent: QObject, *, autostart_available: bool):
        super().__init__(parent)
        self.icon = QSystemTrayIcon(app_icon(), self)
        self.icon.setToolTip(f"WinLauncher — double Ctrl or {SEARCH_SHORTCUT}")
        self.menu = Menu()
        self.menu.setObjectName("tray")
        self.icon_names: dict[QAction, str] = {}  # Tinted again when the theme changes.
        self.check_actions: list[QAction] = []
        self.update_version: str | None = None  # The release an update installs, once known.
        self.status = QLabel("Ready to search")
        self.status.setObjectName("trayStatus")
        self.menu.addAction(self._header())
        self.menu.addSeparator()
        actions: dict[str, QAction] = {}
        for entry in ENTRIES:
            if entry is None:
                self.menu.addSeparator()
            else:
                actions[entry.signal] = self._add_entry(entry)
        self.autostart = actions["autostart_toggled"]
        self.autostart.setEnabled(autostart_available)
        self.centered = actions["centered_toggled"]
        self.update = actions["update_requested"]
        self._use_theme_icons()
        theme().changed.connect(self._use_theme_icons)
        self.menu.aboutToShow.connect(self.opening)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

    def _header(self) -> QWidgetAction:
        """WinLauncher's logo, name, version and current state; not a clickable entry."""
        header = QWidget()
        header.setObjectName("trayHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 8, 16, 6)
        layout.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(HEADER_ICON_SIZE, HEADER_ICON_SIZE))
        texts = QVBoxLayout()
        texts.setSpacing(0)
        name = QLabel("WinLauncher")
        name.setObjectName("trayTitle")
        version = QLabel(__version__)
        version.setObjectName("trayVersion")
        title = QHBoxLayout()
        title.setSpacing(6)
        title.addWidget(name)
        title.addWidget(version, 0, Qt.AlignmentFlag.AlignBaseline)
        title.addStretch(1)
        texts.addLayout(title)
        texts.addWidget(self.status)
        layout.addWidget(logo)
        layout.addLayout(texts, 1)
        # Enabled, or Qt draws the logo grayed out; with no signal it triggers nothing.
        action = QWidgetAction(self.menu)
        action.setDefaultWidget(header)
        return action

    def _add_entry(self, entry: _Entry) -> QAction:
        action = self.menu.addAction(entry.text)
        self.menu.set_key(action, entry.key)
        # Qt forwards `checked` only to signals that accept it.
        action.triggered.connect(getattr(self, entry.signal))
        if entry.icon is CHECK:
            # Its check mark is drawn as an icon, aligned with the others.
            action.setCheckable(True)
            action.toggled.connect(lambda _checked: self._show_check(action))
            self.check_actions.append(action)
        else:
            self.icon_names[action] = entry.icon
        return action

    def _use_theme_icons(self) -> None:
        for action, icon in self.icon_names.items():
            action.setIcon(line_icon(icon, colors().muted))
        for action in self.check_actions:
            self._show_check(action)
        self.set_update_version(self.update_version)

    @staticmethod
    def _show_check(action: QAction) -> None:
        # A null icon keeps the column free, so the text stays aligned.
        action.setIcon(line_icon("check", colors().accent) if action.isChecked() else QIcon())

    def set_catalog_count(self, count: int) -> None:
        self.status.setText(f"{count:,} items indexed" if count else "Ready to search")

    def set_update_version(self, version: str | None) -> None:
        self.update_version = version
        self.menu.set_text(
            self.update, f"Update to {version}…" if version else "Check for updates…"
        )
        self.update.setIcon(line_icon("download", colors().accent if version else colors().muted))

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A double-click also reports Trigger first; reacting to both would open twice.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.search_requested.emit()
