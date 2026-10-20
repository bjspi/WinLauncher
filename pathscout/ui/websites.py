"""Website settings, using the same list, form fields and buttons as the other tabs."""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
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

from pathscout.ui.website_icons import WebsiteIcons
from pathscout.websites import WebsiteShortcut

LIST_ICON_SIZE = QSize(24, 24)
LIST_WIDTH = 160
ROW_SIZE = QSize(LIST_WIDTH, 42)  # Two text lines: title and keyword.
FORM_SPACING = 20
# Favicons follow URL edits once typing pauses, not on every keystroke.
ICON_PREVIEW_DELAY_MS = 500


class WebsiteDraft(NamedTuple):
    """A list entry as typed; it becomes a `WebsiteShortcut` when validated on save."""

    keyword: str = ""
    title: str = ""
    url: str = ""

    def shortcut(self) -> WebsiteShortcut:
        return WebsiteShortcut(self.keyword.strip(), self.title.strip(), self.url.strip())


class WebsiteList(QWidget):
    def __init__(self, websites: tuple[WebsiteShortcut, ...], icons: WebsiteIcons, parent=None):
        super().__init__(parent)
        self.icons = icons
        self.list = QListWidget()
        self.list.setAccessibleName("Website shortcuts")
        self.list.setIconSize(LIST_ICON_SIZE)
        self.list.setMinimumWidth(LIST_WIDTH)
        self.list.setSpacing(1)
        self.keyword, self.title, self.url = QLineEdit(), QLineEdit(), QLineEdit()
        self.keyword.setPlaceholderText("az")
        self.title.setPlaceholderText("Amazon")
        self.url.setPlaceholderText("https://www.amazon.de/s?k=%s")
        self.url.setToolTip("Use %s for the search text, or a fixed URL to open a page.")
        for label, field in (
            ("Website keyword", self.keyword),
            ("Website title", self.title),
            ("Website URL", self.url),
        ):
            field.setAccessibleName(label)
            field.textChanged.connect(self._edited)
        self.form = QWidget()
        form = QFormLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.addRow("Keyword", self.keyword)
        form.addRow("Title", self.title)
        form.addRow("URL", self.url)
        hint = QLabel(
            "Use %s where the search text belongs.\n\n"
            "az headphones → searches Amazon\n"
            "az → opens the website\n\n"
            "YouTube example:\nhttps://www.youtube.com/results?search_query=%s"
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(hint)
        add = QPushButton("Add website")
        self.remove = QPushButton("Remove selected")
        add.clicked.connect(self.add_website)
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
        privacy = QLabel(
            "Favicons load directly from each website. Search text is sent only when you open a result."
        )
        privacy.setWordWrap(True)
        privacy.setObjectName("muted")
        layout.addWidget(privacy)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(ICON_PREVIEW_DELAY_MS)
        self.preview_timer.timeout.connect(self._refresh_icons)
        self.icons.changed.connect(self._refresh_icons)
        self.list.currentRowChanged.connect(self._selected)
        for site in websites:
            self._add_item(WebsiteDraft(site.keyword, site.title, site.url))
        self.list.setCurrentRow(0 if websites else -1)
        self._selected()

    def _add_item(self, draft: WebsiteDraft) -> None:
        item = QListWidgetItem()
        _store(item, draft)
        item.setSizeHint(ROW_SIZE)
        item.setIcon(self.icons.fallback)
        self.list.addItem(item)

    def add_website(self) -> None:
        self._add_item(WebsiteDraft())
        self.list.setCurrentRow(self.list.count() - 1)
        self.keyword.setFocus()

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
        self._selected()

    def _selected(self, *_args) -> None:
        item = self.list.currentItem()
        draft = _draft(item) if item else WebsiteDraft()
        self.form.setEnabled(item is not None)
        self.remove.setEnabled(item is not None)
        for field, value in zip((self.keyword, self.title, self.url), draft, strict=True):
            field.blockSignals(True)
            field.setText(value)
            field.blockSignals(False)

    def _edited(self) -> None:
        if item := self.list.currentItem():
            _store(item, WebsiteDraft(self.keyword.text(), self.title.text(), self.url.text()))
            self.preview_timer.start()

    def _refresh_icons(self) -> None:
        if not self.isVisible():
            return
        for row in range(self.list.count()):
            item = self.list.item(row)
            item.setIcon(self.icons.icon_for(_draft(item).url.strip()))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_icons()

    def websites(self) -> tuple[WebsiteShortcut, ...]:
        """The validated list; on an invalid entry, select it and raise ValueError."""
        sites = []
        for row in range(self.list.count()):
            try:
                sites.append(_draft(self.list.item(row)).shortcut())
            except ValueError as exc:
                self.list.setCurrentRow(row)
                raise ValueError(f"Website {row + 1}: {exc}") from None
        return tuple(sites)


def _store(item: QListWidgetItem, draft: WebsiteDraft) -> None:
    item.setData(Qt.ItemDataRole.UserRole, draft)
    title = draft.title.strip() or "New website"
    item.setText(f"{title}\n{draft.keyword.strip() or 'Choose a keyword'}")


def _draft(item: QListWidgetItem) -> WebsiteDraft:
    return item.data(Qt.ItemDataRole.UserRole)
