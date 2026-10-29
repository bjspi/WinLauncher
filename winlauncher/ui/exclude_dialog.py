"""Choose what to exclude: a result itself, or its folder or one of their parents.

Custom rule… instead turns the chosen path into an exclusion pattern to edit first.
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
from winlauncher.ui.dialogs import run_modal

PATTERN_DIALOG_WIDTH = 640
PATTERN_HELP = (
    "Edit the rule before it is added to the excluded patterns.\n"
    "A name such as node_modules matches everywhere. In a path, * stands for one folder\n"
    "and ** for any number of folders: **\\patches\\src excludes every src folder\n"
    "directly inside a patches folder, wherever that is."
)


@dataclass(frozen=True, slots=True)
class FolderChoice:
    folder: Path  # A folder, or the result itself.
    entries: int  # Catalog entries it covers, shown to guide the choice.


def exclusion_candidates(
    path: Path, roots: list[Path], generated: tuple[Path, ...] = ()
) -> list[Path]:
    """`path` itself and its parents, nearest first.

    Inside a configured root, the list ends at that root. Elsewhere it ends below
    the drive, because excluding a whole drive is never a sensible single click.
    In a `generated` folder, such as WinLauncher's Windows settings shortcuts, only
    `path` itself: those folders are switched off in Settings instead.
    """
    if any(is_within(path, folder) for folder in generated):
        return [path]
    owners = [root for root in roots if is_within(path, root)]
    stop = max(owners, key=lambda root: len(str(root)), default=None)
    candidates = []
    current = path
    while current.parent != current:
        candidates.append(current)
        if stop is not None and current == stop:
            break
        current = current.parent
    return candidates


class ExcludeFolderDialog(QDialog):
    def __init__(self, choices: list[FolderChoice], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exclude from search")
        self.resize(560, 360)
        self.pattern: str | None = None  # Set when a custom rule was accepted instead.
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Choose what to exclude: this entry, or a folder with everything below "
                "it.\nCustom rule… lets you edit it into a pattern first. Undo this in "
                "Settings → Exclusions."
            )
        )
        self.folders = QListWidget()
        self.folders.setObjectName("itemList")
        self.folders.setAccessibleName("Entries and folders that can be excluded")
        for choice in choices:
            entries = f"{choice.entries:,} catalog entr{'y' if choice.entries == 1 else 'ies'}"
            item = QListWidgetItem(f"{choice.folder}\n{entries}")
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
        pattern = editor.textValue().strip() if run_modal(editor) else ""
        editor.deleteLater()
        if pattern:
            self.pattern = pattern
            self.accept()
