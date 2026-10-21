"""System tray icon and its context menu; it only emits user intents."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

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
    opening = Signal()

    def __init__(self, parent: QObject, *, autostart_available: bool):
        super().__init__(parent)
        self.icon = QSystemTrayIcon(app_icon(), self)
        self.icon.setToolTip(f"WinLauncher — double Ctrl or {SEARCH_SHORTCUT}")
        self.menu = Menu()
        self.menu.setObjectName("tray")
        self.icon_names: dict[QAction, str] = {}  # Tinted again when the theme changes.
        self.update_available = False
        self.status = QLabel("Ready to search")
        self.status.setObjectName("trayStatus")
        self.menu.addAction(self._header())
        self.menu.addSeparator()
        self._add_action("Search", self.search_requested, "search", SEARCH_SHORTCUT)
        self._add_action("Refresh catalog", self.refresh_requested, "refresh")
        self._add_action("Index status…", self.status_requested, "activity")
        self.menu.addSeparator()
        self._add_action("Settings…", self.settings_requested, "gear")
        # Its check mark is drawn as an icon, so it lines up with the other icons.
        self.autostart = self._add_action("Start with Windows", self.autostart_toggled)
        self.autostart.setCheckable(True)
        self.autostart.setEnabled(autostart_available)
        self.autostart.toggled.connect(self._show_check)
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
        """WinLauncher's logo, name and current state; not a clickable entry."""
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
            self.icon_names[action] = icon
        action.triggered.connect(signal)  # Qt forwards `checked` only where accepted.
        return action

    def _use_theme_icons(self) -> None:
        for action, icon in self.icon_names.items():
            action.setIcon(line_icon(icon, colors().muted))
        self._show_check(self.autostart.isChecked())
        self.set_update_available(self.update_available)

    def _show_check(self, checked: bool) -> None:
        # A null icon keeps the column free, so the text stays aligned.
        self.autostart.setIcon(line_icon("check", colors().accent) if checked else QIcon())

    def set_catalog_count(self, count: int) -> None:
        self.status.setText(f"{count:,} items indexed" if count else "Ready to search")

    def set_update_available(self, available: bool) -> None:
        self.update_available = available
        self.update.setText("Install update…" if available else "Check for updates…")
        self.update.setIcon(line_icon("download", colors().accent if available else colors().muted))

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A double-click also reports Trigger first; reacting to both would open twice.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.search_requested.emit()
