"""Choose which folder to exclude: a result's folder or one of its parents.

Custom rule… instead turns the chosen folder into an exclusion pattern to edit first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from winlauncher.catalog.model import is_within

PATTERN_DIALOG_WIDTH = 640
PATTERN_HELP = (
    "Edit the rule before it is added to the excluded patterns.\n"
    "A name such as node_modules matches everywhere. In a path, * stands for one folder\n"
    "and ** for any number of folders: **\\patches\\src excludes every src folder\n"
    "directly inside a patches folder, wherever that is."
)


@dataclass(frozen=True, slots=True)
class FolderChoice:
    folder: Path
    entries: int  # Catalog entries inside the folder, shown to guide the choice.


def exclusion_candidates(folder: Path, roots: list[Path]) -> list[Path]:
    """`folder` and its parents, nearest first.

    Inside a configured root, the list ends at that root. Elsewhere it ends below
    the drive, because excluding a whole drive is never a sensible single click.
    """
    owners = [root for root in roots if is_within(folder, root)]
    stop = max(owners, key=lambda root: len(str(root)), default=None)
    candidates = []
    current = folder
    while current.parent != current:
        candidates.append(current)
        if stop is not None and current == stop:
            break
        current = current.parent
    return candidates


class ExcludeFolderDialog(QDialog):
    def __init__(self, choices: list[FolderChoice], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exclude a folder")
        self.resize(560, 360)
        self.pattern: str | None = None  # Set when a custom rule was accepted instead.
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Choose the folder to exclude. It and everything below it will no "
                "longer be indexed or searched.\nCustom rule… lets you edit it into a "
                "pattern first. Undo this in Settings → Exclusions."
            )
        )
        self.folders = QListWidget()
        self.folders.setObjectName("itemList")
        self.folders.setAccessibleName("Folders that can be excluded")
        for choice in choices:
            item = QListWidgetItem(f"{choice.folder}\n{choice.entries:,} catalog entries")
            item.setData(Qt.ItemDataRole.UserRole, choice.folder)
            self.folders.addItem(item)
        self.folders.setCurrentRow(0)
        self.folders.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self.folders, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        exclude = buttons.addButton("Exclude", QDialogButtonBox.ButtonRole.AcceptRole)
        exclude.setDefault(True)
        custom = buttons.addButton("Custom rule…", QDialogButtonBox.ButtonRole.ActionRole)
        custom.clicked.connect(self.edit_pattern)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen_folder(self) -> Path | None:
        item = self.folders.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def edit_pattern(self) -> None:
        """Edit the chosen folder into an exclusion pattern; accepting it closes the picker."""
        folder = self.chosen_folder()
        editor = QInputDialog(self)
        editor.setWindowTitle("Custom exclusion rule")
        editor.setLabelText(PATTERN_HELP)
        editor.setTextValue(str(folder) if folder else "")
        editor.resize(PATTERN_DIALOG_WIDTH, editor.sizeHint().height())
        pattern = editor.textValue().strip() if editor.exec() else ""
        editor.deleteLater()
        if pattern:
            self.pattern = pattern
            self.accept()
