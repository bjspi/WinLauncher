"""System tray icon and its context menu; it only emits user intents."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QSystemTrayIcon

from pathscout.ui.menus import Menu
from pathscout.ui.theme import app_icon


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
        self.icon.setToolTip("PathScout — double Ctrl or Ctrl+Alt+Space")
        self.menu = Menu()
        self._add_action("Search", self.search_requested)
        self._add_action("Refresh catalog", self.refresh_requested)
        self._add_action("Settings…", self.settings_requested)
        self.autostart = self._add_action("Start with Windows", self.autostart_toggled)
        self.autostart.setCheckable(True)
        self.autostart.setEnabled(autostart_available)
        self._add_action("Index status…", self.status_requested)
        self._add_action("Clear search history", self.clear_history_requested)
        self.menu.addSeparator()
        self.update = self._add_action("Check for updates…", self.update_requested)
        self._add_action("About PathScout", self.about_requested)
        self.menu.addSeparator()
        self._add_action("Restart PathScout", self.restart_requested)
        self._add_action("Quit PathScout", self.quit_requested)
        self.menu.aboutToShow.connect(self.opening)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

    def _add_action(self, text: str, signal) -> QAction:
        action = self.menu.addAction(text)
        action.triggered.connect(signal)  # Qt forwards `checked` only where accepted.
        return action

    def set_update_available(self, available: bool) -> None:
        self.update.setText("Install update…" if available else "Check for updates…")

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A double-click also reports Trigger first; reacting to both would open twice.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.search_requested.emit()
