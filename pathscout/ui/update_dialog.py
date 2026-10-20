"""Tell users of an installed package which command installs a new release.

PathScout cannot replace its own running pipx or pip environment on Windows,
so it hands the exact command to the user instead.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from pathscout import __version__
from pathscout.releases import Release


class UpdateCommandDialog(QDialog):
    def __init__(self, release: Release, command: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update PathScout")
        intro = QLabel(
            f"PathScout {release.tag} is available; you are running {__version__}.\n"
            "Quit PathScout, run this command in a terminal, then start it again:"
        )
        intro.setWordWrap(True)
        self.command = QLineEdit(command)
        self.command.setReadOnly(True)
        self.command.setAccessibleName("Update command")
        self.command.setMinimumWidth(self.command.fontMetrics().horizontalAdvance(command) + 24)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy command", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(self.copy_command)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.command)
        layout.addWidget(buttons)

    def copy_command(self) -> None:
        QApplication.clipboard().setText(self.command.text())
