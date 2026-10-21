"""Popup menus that look native on Windows 11."""

from __future__ import annotations

import os

from PySide6.QtWidgets import QMenu


class Menu(QMenu):
    """A QMenu with Windows 11 rounded corners and shadow.

    The window stays opaque, so text keeps ClearType rendering; DWM rounds it.
    """

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if os.name == "nt":
            from winlauncher.platform.windows import round_corners

            round_corners(int(self.winId()))
