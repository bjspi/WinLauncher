"""Tell users of an installed package which command installs a new release.

PathScout cannot replace its own running pipx or pip environment on Windows,
so it hands the exact command to the user instead.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QLineEdit

from pathscout import __version__
from pathscout.releases import Release
from pathscout.ui.theme import Palette
from pathscout.ui.widgets import InfoDialog, line_icon

UPDATE_DIALOG_WIDTH = 620  # Most of the install command stays visible.


class UpdateCommandDialog(InfoDialog):
    def __init__(self, release: Release, command: str, parent=None):
        super().__init__(
            f"PathScout {release.tag} is available",
            f"You are running {__version__}. Quit PathScout, run this command in a terminal, "
            "then start it again.",
            line_icon("download", Palette.ACCENT),
            parent,
            width=UPDATE_DIALOG_WIDTH,
        )
        self.setWindowTitle("Update PathScout")
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
