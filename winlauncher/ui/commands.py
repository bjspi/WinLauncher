"""Context-menu command settings, using the same list, form fields and buttons as the other tabs."""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.commands import MenuCommand

LIST_WIDTH = 200
ROW_SIZE = QSize(LIST_WIDTH, 42)  # Two text lines: title and command line.
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

    def menu_command(self) -> MenuCommand:
        return MenuCommand(self.title.strip(), self.command.strip(), self.folders, self.files)


class CommandList(QWidget):
    def __init__(self, commands: tuple[MenuCommand, ...], parent=None):
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setObjectName("itemList")
        self.list.setAccessibleName("Context menu commands")
        self.list.setMinimumWidth(LIST_WIDTH)
        self.list.setSpacing(1)
        # Long command lines are cut off; the form shows them in full.
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.title, self.command = QLineEdit(), QLineEdit()
        self.title.setPlaceholderText("Open in Windows Terminal")
        self.command.setPlaceholderText('wt.exe -d "{folder}"')
        self.show_for = QComboBox()
        for label, _flags in SHOW_FOR:
            self.show_for.addItem(label)
        for label, field in (("Command title", self.title), ("Command line", self.command)):
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
        hint = QLabel(
            "{folder} is the folder, or a file's folder\n"
            "{path} is the folder or file itself\n"
            "{name} is its name\n"
            "%USERPROFILE% and other variables work, too.\n\n"
            "Programs start in {folder}.\n\n"
            'VS Code example:\ncode "{path}"'
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(hint)
        add = QPushButton("Add command")
        self.remove = QPushButton("Remove selected")
        add.clicked.connect(self.add_command)
        self.remove.clicked.connect(self.remove_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
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
                CommandDraft(command.title, command.command, command.folders, command.files)
            )
        self.list.setCurrentRow(0 if commands else -1)
        self._selected()

    def _add_item(self, draft: CommandDraft) -> None:
        item = QListWidgetItem()
        _store(item, draft)
        item.setSizeHint(ROW_SIZE)
        self.list.addItem(item)

    def add_command(self) -> None:
        self._add_item(CommandDraft())
        self.list.setCurrentRow(self.list.count() - 1)
        self.title.setFocus()

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
        self._selected()

    def _selected(self, *_args) -> None:
        item = self.list.currentItem()
        draft = _draft(item) if item else CommandDraft()
        self.form.setEnabled(item is not None)
        self.remove.setEnabled(item is not None)
        flags = (draft.folders, draft.files)
        show_for = next((i for i, (_, choice) in enumerate(SHOW_FOR) if choice == flags), 0)
        for field in (self.title, self.command, self.show_for):
            field.blockSignals(True)
        self.title.setText(draft.title)
        self.command.setText(draft.command)
        self.show_for.setCurrentIndex(show_for)
        for field in (self.title, self.command, self.show_for):
            field.blockSignals(False)

    def _edited(self) -> None:
        if item := self.list.currentItem():
            folders, files = SHOW_FOR[self.show_for.currentIndex()][1]
            _store(item, CommandDraft(self.title.text(), self.command.text(), folders, files))

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


def _draft(item: QListWidgetItem) -> CommandDraft:
    return item.data(Qt.ItemDataRole.UserRole)
