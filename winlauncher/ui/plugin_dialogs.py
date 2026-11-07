"""Dialogs a plugin command shows: a checklist, and a form of option fields.

Plugins cannot build windows of their own: their commands run on worker threads
and the plugin API knows no Qt. `context.choose` and `context.form` therefore hand
the host plain `Choice` and `PluginOption` values, and these dialogs show them in
WinLauncher's look. A checklist has a filter, since a plugin's list easily holds
hundreds of rows; a form uses the same editors as the plugin's settings card.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView, QLineEdit, QTreeWidget, QTreeWidgetItem, QWidget

from winlauncher.plugins.api import Choice, OptionValue, PluginOption
from winlauncher.ui.dialogs import run_modal
from winlauncher.ui.option_editors import option_editor
from winlauncher.ui.theme import colors
from winlauncher.ui.widgets import Card, InfoDialog, line_icon

CHECKLIST_WIDTH = 580
FORM_WIDTH = 640  # Room for a label and its description beside a text box.
LIST_HEIGHT = 340  # About eleven rows; more scroll.
FILTER_FROM_ROWS = 10  # Shorter checklists show every row at once and need no filter.
ROW_HEIGHT = 28  # Of a checklist row, for sizing a short list to its rows.
LIST_FRAME = 10  # The list's border and padding, top and bottom together.
LABEL_COLUMN, DETAIL_COLUMN = 0, 1


class ChecklistDialog(InfoDialog):
    """Rows to tick; a long list adds a filter and helpers that tick the visible rows.

    A short one, such as what an Exclude… may leave out, shows all its rows at once.
    """

    def __init__(
        self, title: str, text: str, choices: Sequence[Choice], parent: QWidget | None = None
    ):
        super().__init__(
            title, text, line_icon("help", colors().accent), parent, width=CHECKLIST_WIDTH
        )
        long_list = len(choices) >= FILTER_FROM_ROWS
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter")
        self.filter.setAccessibleName(f"Filter {title}")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        if long_list:
            self.add_widget(self.filter)
        self.rows = _rows(choices)
        self.rows.setAccessibleName(title)
        if not long_list:
            self.rows.setFixedHeight(len(choices) * ROW_HEIGHT + LIST_FRAME)
        self.add_widget(self.rows)
        if long_list:
            self.add_helper("All", lambda: self._tick_visible(True), link=True)
            self.add_helper("None", lambda: self._tick_visible(False), link=True)
        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, primary=True)
        (self.filter if long_list else self.rows).setFocus()

    def ticked(self) -> frozenset[str]:
        """Keys of every ticked row, including the ones the filter hides."""
        return frozenset(
            item.data(LABEL_COLUMN, Qt.ItemDataRole.UserRole)
            for item in self._items()
            if item.checkState(LABEL_COLUMN) == Qt.CheckState.Checked
        )

    def _apply_filter(self, text: str) -> None:
        """Show the rows whose label or detail contains every typed word."""
        terms = text.casefold().split()
        for item in self._items():
            haystack = f"{item.text(LABEL_COLUMN)} {item.text(DETAIL_COLUMN)}".casefold()
            item.setHidden(not all(term in haystack for term in terms))

    def _tick_visible(self, ticked: bool) -> None:
        state = Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(LABEL_COLUMN, state)

    def _items(self) -> list[QTreeWidgetItem]:
        return [self.rows.topLevelItem(row) for row in range(self.rows.topLevelItemCount())]


def _rows(choices: Sequence[Choice]) -> QTreeWidget:
    """A flat two-column list: the label to tick and its muted detail."""
    rows = QTreeWidget()
    rows.setObjectName("checklist")
    rows.setColumnCount(2)
    rows.setHeaderHidden(True)
    rows.setRootIsDecorated(False)
    rows.setUniformRowHeights(True)
    rows.setFixedHeight(LIST_HEIGHT)
    rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    header = rows.header()
    header.setStretchLastSection(False)
    header.setSectionResizeMode(LABEL_COLUMN, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(DETAIL_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
    detail_color = QColor(colors().subtle)
    for choice in choices:
        item = QTreeWidgetItem([choice.label, choice.detail])
        item.setData(LABEL_COLUMN, Qt.ItemDataRole.UserRole, choice.key)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        checked = Qt.CheckState.Checked if choice.checked else Qt.CheckState.Unchecked
        item.setCheckState(LABEL_COLUMN, checked)
        item.setForeground(DETAIL_COLUMN, detail_color)
        item.setToolTip(LABEL_COLUMN, choice.label)
        rows.addTopLevelItem(item)
    return rows


def choose_items(
    parent: QWidget | None, title: str, text: str, choices: Sequence[Choice]
) -> frozenset[str] | None:
    """The keys the user left ticked; None when cancelled."""
    dialog = ChecklistDialog(title, text, choices, parent)
    accepted = run_modal(dialog) == InfoDialog.DialogCode.Accepted
    ticked = dialog.ticked()
    dialog.deleteLater()
    return ticked if accepted else None


class FormDialog(InfoDialog):
    """Option fields on a card, as on the plugin's settings card, above Cancel and Save."""

    def __init__(
        self, title: str, text: str, fields: Sequence[PluginOption], parent: QWidget | None = None
    ):
        super().__init__(title, text, line_icon("help", colors().accent), parent, width=FORM_WIDTH)
        self.fields = tuple(fields)
        self._readers: dict[str, Callable[[], OptionValue]] = {}
        card = Card()
        for field in self.fields:
            editor = option_editor(field, field.default)
            self._readers[field.key] = editor.read
            card.add_row(field.label, field.description, editor.control)
        self.add_widget(card)
        self.add_button("Cancel", self.reject)
        self.add_button("Save", self.accept, primary=True)

    def values(self) -> dict[str, OptionValue]:
        """The edited values, each validated by its field like a stored option."""
        return {field.key: field.value(self._readers[field.key]()) for field in self.fields}


def fill_form(
    parent: QWidget | None, title: str, text: str, fields: Sequence[PluginOption]
) -> dict[str, OptionValue] | None:
    """The values the user saved; None when cancelled."""
    dialog = FormDialog(title, text, fields, parent)
    accepted = run_modal(dialog) == InfoDialog.DialogCode.Accepted
    values = dialog.values()
    dialog.deleteLater()
    return values if accepted else None
