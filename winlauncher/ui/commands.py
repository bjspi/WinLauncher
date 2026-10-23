"""Context-menu command settings, using the same list, form fields and buttons as the other tabs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileIconProvider,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.commands import MenuCommand
from winlauncher.platform.shell import alias_target
from winlauncher.ui.widgets import pick_path

LIST_WIDTH = 200
ROW_SIZE = QSize(LIST_WIDTH, 42)  # Two text lines: title and command line.
LIST_ICON_SIZE = QSize(24, 24)
ICON_FILES = "Icons (*.ico *.png *.svg *.webp);;Programs and libraries (*.exe *.dll);;All files (*)"
IMAGE_SUFFIXES = (".ico", ".png", ".svg", ".webp")
FORM_SPACING = 20
# "Show for" choices: label, (folders, files).
SHOW_FOR = (
    ("Folders", (True, False)),
    ("Files", (False, True)),
    ("Folders and files", (True, True)),
)


class CommandDraft(NamedTuple):
    """A list entry as typed; it becomes a `MenuCommand` when validated on save."""

    title: str = ""
    command: str = ""
    folders: bool = True
    files: bool = False
    icon: str = ""

    def menu_command(self) -> MenuCommand:
        return MenuCommand(
            self.title.strip(), self.command.strip(), self.folders, self.files, self.icon.strip()
        )


def command_icon(command: MenuCommand) -> QIcon:
    """The command's icon: its chosen file, or else its program's; null if neither exists."""
    file = command.icon_file()
    if file is None or not file.exists():
        return QIcon()
    if file.suffix.lower() in IMAGE_SUFFIXES:
        return QIcon(str(file))
    if os.name != "nt":
        return QFileIconProvider().icon(QFileInfo(str(file)))
    return _program_icon(alias_target(file))


def _program_icon(file: Path) -> QIcon:
    """The first icon of a program or library, in its small and large size."""
    import pywintypes
    import win32gui

    try:
        large, small = win32gui.ExtractIconEx(str(file), 0, 1)
    except pywintypes.error:
        return QIcon()
    icon = QIcon()
    for handle in (*small, *large):
        icon.addPixmap(QPixmap.fromImage(QImage.fromHICON(handle)))
        win32gui.DestroyIcon(handle)
    return icon


class CommandList(QWidget):
    def __init__(self, commands: tuple[MenuCommand, ...], parent=None):
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setObjectName("itemList")
        self.list.setAccessibleName("Context menu commands")
        self.list.setMinimumWidth(LIST_WIDTH)
        self.list.setSpacing(1)
        self.list.setIconSize(LIST_ICON_SIZE)
        # Long command lines are cut off; the form shows them in full.
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.title, self.command, self.icon = QLineEdit(), QLineEdit(), QLineEdit()
        self.title.setPlaceholderText("Open in Windows Terminal")
        self.command.setPlaceholderText('wt.exe -d "{folder}"')
        self.icon.setPlaceholderText("Automatic: the program's icon")
        self.icon.setToolTip(
            "An .ico, .png, .svg, .webp, .exe or .dll file; leave empty for the program's icon."
        )
        self.show_for = QComboBox()
        for label, _flags in SHOW_FOR:
            self.show_for.addItem(label)
        for label, field in (
            ("Command title", self.title),
            ("Command line", self.command),
            ("Command icon file", self.icon),
        ):
            field.setAccessibleName(label)
            field.textChanged.connect(self._edited)
        self.show_for.setAccessibleName("Show the command for")
        self.show_for.currentIndexChanged.connect(self._edited)
        self.form = QWidget()
        form = QFormLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.addRow("Title", self.title)
        form.addRow("Command line", self.command)
        form.addRow("Show for", self.show_for)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_icon)
        icon_row = QHBoxLayout()
        icon_row.setContentsMargins(0, 0, 0, 0)
        icon_row.addWidget(self.icon, 1)
        icon_row.addWidget(browse)
        form.addRow("Icon", icon_row)
        hint = QLabel(
            "{folder} is the folder, or a file's folder\n"
            "{path} is the folder or file itself\n"
            "{name} is its name\n"
            "%USERPROFILE% and other variables work, too.\n\n"
            "Programs start in {folder}.\n\n"
            "Claude Code in Windows Terminal:\n"
            'wt.exe -d "{folder}" cmd /k claude'
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(hint)
        add = QPushButton("Add command")
        self.duplicate = QPushButton("Duplicate")
        self.remove = QPushButton("Remove selected")
        add.clicked.connect(self.add_command)
        self.duplicate.clicked.connect(self.duplicate_selected)
        self.remove.clicked.connect(self.remove_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(self.duplicate)
        buttons.addWidget(self.remove)
        buttons.addStretch()
        content = QHBoxLayout()
        content.setSpacing(FORM_SPACING)
        content.addWidget(self.list, 1)
        content.addWidget(self.form, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # The settings page provides title and frame.
        layout.addLayout(content, 1)
        layout.addLayout(buttons)
        self.list.currentRowChanged.connect(self._selected)
        for command in commands:
            self._add_item(
                CommandDraft(
                    command.title, command.command, command.folders, command.files, command.icon
                )
            )
        self.list.setCurrentRow(0 if commands else -1)
        self._selected()

    def _add_item(self, draft: CommandDraft, row: int | None = None) -> None:
        item = QListWidgetItem()
        _store(item, draft)
        item.setSizeHint(ROW_SIZE)
        self.list.insertItem(self.list.count() if row is None else row, item)

    def add_command(self) -> None:
        self._add_item(CommandDraft())
        self.list.setCurrentRow(self.list.count() - 1)
        self.title.setFocus()

    def duplicate_selected(self) -> None:
        """A copy below the selected entry, for a variant such as Codex beside Claude."""
        row = self.list.currentRow()
        if row < 0:
            return
        draft = _draft(self.list.item(row))
        self._add_item(draft._replace(title=f"{draft.title.strip()} (copy)"), row + 1)
        self.list.setCurrentRow(row + 1)
        self.title.setFocus()
        self.title.selectAll()

    def choose_icon(self) -> None:
        if file := pick_path(self, "Choose icon", ICON_FILES):
            self.icon.setText(str(file))

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        title = _draft(self.list.item(row)).title.strip() or "New command"
        answer = QMessageBox.question(
            self, "Remove command", f"Remove the context menu command “{title}”?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.list.takeItem(row)
        self._selected()

    def _selected(self, *_args) -> None:
        item = self.list.currentItem()
        draft = _draft(item) if item else CommandDraft()
        self.form.setEnabled(item is not None)
        self.remove.setEnabled(item is not None)
        self.duplicate.setEnabled(item is not None)
        flags = (draft.folders, draft.files)
        show_for = next((i for i, (_, choice) in enumerate(SHOW_FOR) if choice == flags), 0)
        fields = (self.title, self.command, self.icon, self.show_for)
        for field in fields:
            field.blockSignals(True)
        self.title.setText(draft.title)
        self.command.setText(draft.command)
        self.icon.setText(draft.icon)
        self.show_for.setCurrentIndex(show_for)
        for field in fields:
            field.blockSignals(False)

    def _edited(self) -> None:
        if item := self.list.currentItem():
            folders, files = SHOW_FOR[self.show_for.currentIndex()][1]
            draft = CommandDraft(
                self.title.text(), self.command.text(), folders, files, self.icon.text()
            )
            _store(item, draft)

    def commands(self) -> tuple[MenuCommand, ...]:
        """The validated list; on an invalid entry, select it and raise ValueError."""
        commands = []
        for row in range(self.list.count()):
            try:
                commands.append(_draft(self.list.item(row)).menu_command())
            except ValueError as exc:
                self.list.setCurrentRow(row)
                raise ValueError(f"Command {row + 1}: {exc}") from None
        return tuple(commands)


def _store(item: QListWidgetItem, draft: CommandDraft) -> None:
    item.setData(Qt.ItemDataRole.UserRole, draft)
    title = draft.title.strip() or "New command"
    item.setText(f"{title}\n{draft.command.strip() or 'Enter a command line'}")
    try:
        item.setIcon(command_icon(draft.menu_command()))
    except ValueError:
        item.setIcon(QIcon())  # Not a valid command yet, such as a new entry.


def _draft(item: QListWidgetItem) -> CommandDraft:
    return item.data(Qt.ItemDataRole.UserRole)
