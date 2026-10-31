"""System tray icon and its context menu; it only emits user intents."""

from __future__ import annotations

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
        self._add_action("Search", self.search_requested, "search", SEARCH_SHORTCUT)
        self._add_action("Refresh catalog", self.refresh_requested, "refresh")
        self._add_action("Index status…", self.status_requested, "activity")
        self.menu.addSeparator()
        self._add_action("Settings…", self.settings_requested, "gear")
        self.autostart = self._add_check("Start with Windows", self.autostart_toggled)
        self.autostart.setEnabled(autostart_available)
        self.centered = self._add_check("Center search bar", self.centered_toggled)
        self._add_action("Clear search history", self.clear_history_requested, "trash")
        self.menu.addSeparator()
        self.update = self._add_action("Check for updates…", self.update_requested, "download")
        self._add_action("About WinLauncher", self.about_requested, "info")
        self.menu.addSeparator()
        self._add_action("Restart WinLauncher", self.restart_requested, "restart")
        self._add_action("Quit WinLauncher", self.quit_requested, "quit")
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

    def _add_action(
        self, text: str, signal, icon: str | None = None, shortcut: str = ""
    ) -> QAction:
        # The text after a tab is shown right-aligned, as a shortcut hint.
        action = self.menu.addAction(f"{text}\t{shortcut}" if shortcut else text)
        if icon is not None:
            self.icon_names[action] = icon
        action.triggered.connect(signal)  # Qt forwards `checked` only where accepted.
        return action

    def _add_check(self, text: str, signal) -> QAction:
        """A checkable entry; its check mark is drawn as an icon, aligned with the others."""
        action = self._add_action(text, signal)
        action.setCheckable(True)
        action.toggled.connect(lambda _checked: self._show_check(action))
        self.check_actions.append(action)
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
        self.update.setText(f"Update to {version}…" if version else "Check for updates…")
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
