"""Dialogs that offer a new release, report a failed update or hand over the install command.

A checkout updates itself; its dialogs keep git's details hidden until asked for.
WinLauncher cannot replace its own running pipx or pip environment on Windows,
so for those it hands the exact command to the user instead.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit

from winlauncher import __version__
from winlauncher.releases import Release
from winlauncher.ui.theme import colors
from winlauncher.ui.widgets import InfoDialog, line_icon

UPDATE_DIALOG_WIDTH = 620  # Most of the install command stays visible.
DETAILS_HEIGHT = 320  # The details scroll instead of growing past the screen.


class UpdateDialog(InfoDialog):
    """A question or report whose details (changes, git output) load when first shown."""

    def __init__(
        self,
        title: str,
        subtitle: str,
        details: Callable[[], str],
        details_label: str,
        parent=None,
    ):
        super().__init__(
            title,
            subtitle,
            line_icon("download", colors().accent),
            parent,
            width=UPDATE_DIALOG_WIDTH,
        )
        self.setWindowTitle("Update WinLauncher")
        self._load_details = details
        self.details = QPlainTextEdit()
        self.details.setObjectName("code")
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details.setAccessibleName(details_label)
        self.details.setFixedHeight(DETAILS_HEIGHT)
        self.details.hide()
        self.add_widget(self.details)
        self.add_button(details_label, self.toggle_details)

    def toggle_details(self) -> None:
        if not self.details.toPlainText():
            self.details.setPlainText(self._load_details())
        self.details.setVisible(not self.details.isVisible())
        self.adjustSize()


class UpdateCommandDialog(InfoDialog):
    def __init__(self, release: Release, command: str, parent=None):
        super().__init__(
            f"WinLauncher {release.tag} is available",
            f"You are running {__version__}. Quit WinLauncher, run this command in a terminal, "
            "then start it again.",
            line_icon("download", colors().accent),
            parent,
            width=UPDATE_DIALOG_WIDTH,
        )
        self.setWindowTitle("Update WinLauncher")
        self.command = QLineEdit(command)
        self.command.setObjectName("code")
        self.command.setReadOnly(True)
        self.command.setAccessibleName("Update command")
        self.command.setCursorPosition(0)  # Show where the command starts.
        self.add_widget(self.command)
        self.add_button("Close", self.reject)
        self.add_button("Copy command", self.copy_command, primary=True)

    def copy_command(self) -> None:
        QApplication.clipboard().setText(self.command.text())
