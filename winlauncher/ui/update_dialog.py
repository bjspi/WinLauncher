"""Tell users of an installed package which command installs a new release.

A checkout updates itself (its dialogs are message dialogs, see `messages`).
WinLauncher cannot replace its own running pipx or pip environment on Windows,
so for those it hands the exact command to the user instead.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QLineEdit

from winlauncher import __version__
from winlauncher.releases import Release
from winlauncher.ui.messages import UPDATE, badge
from winlauncher.ui.widgets import InfoDialog

UPDATE_DIALOG_WIDTH = 620  # Most of the install command stays visible.


class UpdateCommandDialog(InfoDialog):
    def __init__(self, release: Release, command: str, parent=None):
        super().__init__(
            f"WinLauncher {release.version} is available",
            f"You are running {__version__}. Quit WinLauncher, run this command in a terminal, "
            "then start it again.",
            badge(UPDATE),
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
