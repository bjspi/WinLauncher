"""System tray icon and its context menu; it only emits user intents."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from pathscout.ui.menus import Menu
from pathscout.ui.theme import Palette, app_icon
from pathscout.ui.widgets import line_icon

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
    opening = Signal()

    def __init__(self, parent: QObject, *, autostart_available: bool):
        super().__init__(parent)
        self.icon = QSystemTrayIcon(app_icon(), self)
        self.icon.setToolTip(f"PathScout — double Ctrl or {SEARCH_SHORTCUT}")
        self.menu = Menu()
        self.menu.setObjectName("tray")
        self.status = QLabel("Ready to search")
        self.status.setObjectName("trayStatus")
        self.menu.addAction(self._header())
        self.menu.addSeparator()
        self._add_action("Search", self.search_requested, "search", SEARCH_SHORTCUT)
        self._add_action("Refresh catalog", self.refresh_requested, "refresh")
        self._add_action("Index status…", self.status_requested, "activity")
        self.menu.addSeparator()
        self._add_action("Settings…", self.settings_requested, "gear")
        # Checkable: its check mark takes the icon's place.
        self.autostart = self._add_action("Start with Windows", self.autostart_toggled)
        self.autostart.setCheckable(True)
        self.autostart.setEnabled(autostart_available)
        self._add_action("Clear search history", self.clear_history_requested, "trash")
        self.menu.addSeparator()
        self.update = self._add_action("Check for updates…", self.update_requested, "download")
        self._add_action("About PathScout", self.about_requested, "info")
        self.menu.addSeparator()
        self._add_action("Restart PathScout", self.restart_requested, "restart")
        self._add_action("Quit PathScout", self.quit_requested, "quit")
        self.menu.aboutToShow.connect(self.opening)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

    def _header(self) -> QWidgetAction:
        """PathScout's logo, name and current state; not a clickable entry."""
        header = QWidget()
        header.setObjectName("trayHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 8, 16, 6)
        layout.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(HEADER_ICON_SIZE, HEADER_ICON_SIZE))
        texts = QVBoxLayout()
        texts.setSpacing(0)
        name = QLabel("PathScout")
        name.setObjectName("trayTitle")
        texts.addWidget(name)
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
            action.setIcon(line_icon(icon, Palette.MENU_ICON))
        action.triggered.connect(signal)  # Qt forwards `checked` only where accepted.
        return action

    def set_catalog_count(self, count: int) -> None:
        self.status.setText(f"{count:,} items indexed" if count else "Ready to search")

    def set_update_available(self, available: bool) -> None:
        self.update.setText("Install update…" if available else "Check for updates…")
        self.update.setIcon(
            line_icon("download", Palette.ACCENT if available else Palette.MENU_ICON)
        )

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A double-click also reports Trigger first; reacting to both would open twice.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.search_requested.emit()
