"""Popup menus that look native on Windows 11."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

#: How long after opening a click on an entry is ignored, in seconds.
CLICK_GUARD_S = 0.4


class Menu(QMenu):
    """A QMenu with Windows 11 rounded corners and shadow.

    The window stays opaque, so text keeps ClearType rendering; DWM rounds it.

    Clicks on the menu right after it opens are ignored: a menu opened from the
    tray opens upwards with its last entry (Quit) under the pointer, and a touchpad
    tap that lands just after the right-click would trigger it by accident.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._keys: dict[str, QAction] = {}
        self._opened_at = 0.0

    def set_key(self, action: QAction, key: str) -> None:
        """Pressing `key` while the menu is open triggers `action`.

        The letter is shown right-aligned beside the entry, where Qt puts the text
        after a tab; `set_text` keeps it there when the entry is renamed.
        """
        self._keys[key.lower()] = action
        action.setData(key.upper())
        self.set_text(action, action.text())

    @staticmethod
    def set_text(action: QAction, text: str) -> None:
        key = action.data()
        action.setText(f"{text}\t{key}" if isinstance(key, str) and key else text)

    def keyPressEvent(self, event) -> None:
        # Spelled out: in PySide6, ~ShiftModifier is a mask without Ctrl.
        held = event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        action = None if held else self._keys.get(event.text().lower())
        if action is not None and action.isEnabled() and action.isVisible():
            self.close()
            action.trigger()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        if not self._too_early(event):
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if not self._too_early(event):
            super().mouseReleaseEvent(event)

    def _too_early(self, event) -> bool:
        """A click inside the menu within the guard time; outside clicks still close it."""
        inside = self.rect().contains(event.position().toPoint())
        return inside and time.monotonic() - self._opened_at < CLICK_GUARD_S

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._opened_at = time.monotonic()
        if os.name == "nt":
            from winlauncher.platform.windows import round_corners

            round_corners(int(self.winId()))
