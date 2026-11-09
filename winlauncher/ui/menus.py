"""Popup menus that look native on Windows 11."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu


class Menu(QMenu):
    """A QMenu with Windows 11 rounded corners and shadow.

    The window stays opaque, so text keeps ClearType rendering; DWM rounds it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._keys: dict[str, QAction] = {}

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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if os.name == "nt":
            from winlauncher.platform.windows import round_corners

            round_corners(int(self.winId()))
