"""Search the Settings dialog: its pages and every titled row of their cards.

Typing replaces the page list with the matching settings; choosing one opens
its page, scrolls to the row and lights it up for a moment.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem, QWidget

from winlauncher.ui.theme import colors, theme
from winlauncher.ui.widgets import line_icon

NO_MATCH = "No settings match"


@dataclass(frozen=True, eq=False)
class SettingEntry:
    """A page, or a row on it; `widget` is None for the page itself."""

    page_row: int
    page: str
    title: str
    description: str
    widget: QWidget | None = None


def find(query: str, entries: Sequence[SettingEntry]) -> list[SettingEntry]:
    """Entries containing every word, ignoring case; title matches come first."""
    words = query.casefold().split()
    if not words:
        return []

    def has(text: str) -> bool:
        folded = text.casefold()
        return all(word in folded for word in words)

    titled = [entry for entry in entries if has(entry.title)]
    shown = {id(entry) for entry in titled}
    described = [
        entry
        for entry in entries
        if id(entry) not in shown and has(f"{entry.title} {entry.description} {entry.page}")
    ]
    return titled + described


class SettingsSearch(QObject):
    """The search field and its result list; the dialog places both and opens a choice."""

    chosen = Signal(object)  # The SettingEntry to open.
    active_changed = Signal(bool)  # Results replace the page list while text is typed.

    def __init__(self, entries: Callable[[], list[SettingEntry]], parent: QObject | None = None):
        """`entries` is asked at each keystroke, so rows added meanwhile are found too."""
        super().__init__(parent)
        self.entries = entries
        self.active = False
        self.field = QLineEdit()
        self.field.setObjectName("settingsSearch")
        self.field.setPlaceholderText("Search settings")
        self.field.setAccessibleName("Search settings")
        self.field.setClearButtonEnabled(True)
        self.magnifier = self.field.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._use_theme_icon()
        theme().changed.connect(self._use_theme_icon)
        self.field.textChanged.connect(self._search)
        self.field.installEventFilter(self)
        self.results = QListWidget()
        self.results.setObjectName("nav")
        self.results.setAccessibleName("Matching settings")
        self.results.setWordWrap(True)  # Long titles wrap instead of scrolling sideways.
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.hide()
        self.results.itemClicked.connect(self._open)
        self.results.itemActivated.connect(self._open)

    def _use_theme_icon(self) -> None:
        self.magnifier.setIcon(line_icon("search", colors().faint))

    def focus(self) -> None:
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.field.selectAll()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """In the field, Enter opens the selected match, Esc clears typed text first and
        Down moves into the results. Enter never reaches the dialog's Save button.
        """
        if watched is self.field and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._open_current()
                return True
            if event.key() == Qt.Key.Key_Escape and self.field.text():
                self.field.clear()
                return True
            if event.key() == Qt.Key.Key_Down and self.active:
                self.results.setFocus(Qt.FocusReason.OtherFocusReason)
                return True
        return super().eventFilter(watched, event)

    def _search(self, text: str) -> None:
        active = bool(text.strip())
        if active != self.active:
            self.active = active
            self.results.setVisible(active)
            self.active_changed.emit(active)
        self.results.clear()
        if not active:
            return
        for entry in find(text, self.entries()):
            self._add_result(entry)
        if self.results.count():
            self.results.setCurrentRow(0)
        else:
            item = QListWidgetItem(NO_MATCH)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.results.addItem(item)

    def _add_result(self, entry: SettingEntry) -> None:
        """The setting's title with its page on a second line; a page alone for a page."""
        text = entry.page if entry.widget is None else f"{entry.title}\n{entry.page}"
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setToolTip(entry.description)
        self.results.addItem(item)

    def _open_current(self) -> None:
        if (item := self.results.currentItem()) is not None:
            self._open(item)

    def _open(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, SettingEntry):
            self.chosen.emit(entry)
