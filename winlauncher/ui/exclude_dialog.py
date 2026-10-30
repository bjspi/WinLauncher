"""Choose what to exclude: a result itself, or its folder or one of their parents.

Custom rule… instead turns the chosen path into an exclusion pattern to edit first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from winlauncher.catalog.model import is_within
from winlauncher.ui.dialogs import run_modal
from winlauncher.ui.messages import Kind, badge
from winlauncher.ui.theme import colors
from winlauncher.ui.widgets import InfoDialog, line_icon

EXCLUDE = Kind("eye-off", lambda: colors().accent)
PICKER_WIDTH = 580
PATTERN_DIALOG_WIDTH = 640
PATTERN_HELP = (
    "A name such as <b>node_modules</b> matches everywhere. In a path, <b>*</b> stands "
    "for one folder and <b>**</b> for any number of folders: <b>**\\patches\\src</b> "
    "excludes every src folder directly inside a patches folder, wherever that is."
)
ROW_HEIGHT = 52
ROW_SPACING = 2  # Between rows, so the rounded selection never touches its neighbour.
ROW_RADIUS = 8
ROW_PADDING = 12
ROW_ICON_SIZE = 20
ROW_GAP = 12  # Between the icon, the texts and the count.
VISIBLE_ROWS = 6  # More parents scroll.
COUNT_PADDING = 8  # Inside the count's pill.
COUNT_HEIGHT = 20


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


def entries_text(entries: int) -> str:
    return f"{entries:,} entr{'y' if entries == 1 else 'ies'}"


@dataclass(frozen=True, slots=True)
class _Row:
    name: str  # The last part of the path, or the whole path for a drive.
    location: str  # The folder it is in.
    count: str
    folder: bool


class _ChoiceDelegate(QStyledItemDelegate):
    """A choice as a card row: icon, name above its location, and a count pill."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(0, ROW_HEIGHT)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        row: _Row = index.data(Qt.ItemDataRole.UserRole + 1)
        theme = colors()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
        if selected or hovered:
            painter.setPen(QColor(theme.chosen_border) if selected else Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.chosen if selected else theme.hover))
            painter.drawRoundedRect(frame, ROW_RADIUS, ROW_RADIUS)
        rect = option.rect.adjusted(ROW_PADDING, 0, -ROW_PADDING, 0)
        icon_color = theme.accent_strong if selected else theme.muted
        icon = line_icon("folder" if row.folder else "file", icon_color)
        icon_top = rect.top() + (rect.height() - ROW_ICON_SIZE) // 2
        icon.paint(painter, QRect(rect.left(), icon_top, ROW_ICON_SIZE, ROW_ICON_SIZE))
        rect.setLeft(rect.left() + ROW_ICON_SIZE + ROW_GAP)
        # The count pill at the right.
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() - 1)
        count_width = QFontMetrics(small).horizontalAdvance(row.count) + 2 * COUNT_PADDING
        pill = QRectF(
            rect.right() - count_width,
            rect.top() + (rect.height() - COUNT_HEIGHT) / 2,
            count_width,
            COUNT_HEIGHT,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.field if selected else theme.selected))
        painter.drawRoundedRect(pill, COUNT_HEIGHT / 2, COUNT_HEIGHT / 2)
        painter.setFont(small)
        painter.setPen(QColor(theme.secondary if selected else theme.subtle))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, row.count)
        rect.setRight(int(pill.left()) - ROW_GAP)
        # The name above its location.
        bold = QFont(option.font)
        bold.setWeight(QFont.Weight.DemiBold)
        name_height = QFontMetrics(bold).height()
        location_height = QFontMetrics(small).height()
        top = rect.top() + (rect.height() - name_height - location_height - 2) // 2
        painter.setFont(bold)
        painter.setPen(QColor(theme.text))
        name = QFontMetrics(bold).elidedText(row.name, Qt.TextElideMode.ElideMiddle, rect.width())
        painter.drawText(QRect(rect.left(), top, rect.width(), name_height), 0, name)
        painter.setFont(small)
        painter.setPen(QColor(theme.faint))
        location = QFontMetrics(small).elidedText(
            row.location, Qt.TextElideMode.ElideMiddle, rect.width()
        )
        location_rect = QRect(rect.left(), top + name_height + 2, rect.width(), location_height)
        painter.drawText(location_rect, 0, location)
        painter.restore()


class ExcludeFolderDialog(InfoDialog):
    def __init__(self, choices: list[FolderChoice], parent=None):
        super().__init__(
            "Exclude from search",
            "Choose this entry, or a folder with everything below it. "
            "You can undo this in Settings → Exclusions.",
            badge(EXCLUDE),
            parent,
            width=PICKER_WIDTH,
        )
        self.pattern: str | None = None  # Set when a custom rule was accepted instead.
        self.folders = QListWidget()
        self.folders.setObjectName("choiceList")
        self.folders.setAccessibleName("Entries and folders that can be excluded")
        self.folders.setItemDelegate(_ChoiceDelegate(self.folders))
        self.folders.setSpacing(ROW_SPACING // 2)
        self.folders.setMouseTracking(True)  # Hovered rows light up.
        self.folders.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.folders.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for position, choice in enumerate(choices):
            # Every choice after the first is a parent folder of the result.
            folder = position > 0 or choice.folder.is_dir()
            row = _Row(
                name=choice.folder.name or str(choice.folder),
                location=str(choice.folder.parent) if choice.folder.name else "",
                count=entries_text(choice.entries) if folder else "Only this entry",
                folder=folder,
            )
            item = QListWidgetItem(f"{choice.folder}\n{row.count}")
            item.setData(Qt.ItemDataRole.UserRole, choice.folder)
            item.setData(Qt.ItemDataRole.UserRole + 1, row)
            item.setToolTip(str(choice.folder))
            self.folders.addItem(item)
        rows = min(max(1, len(choices)), VISIBLE_ROWS)
        self.folders.setFixedHeight(rows * (ROW_HEIGHT + ROW_SPACING) + 2 * ROW_SPACING)
        self.folders.setCurrentRow(0)
        self.folders.itemDoubleClicked.connect(self.accept)
        self.add_widget(self.folders)
        self.add_helper("Custom rule…", self.edit_pattern, link=True)
        self.add_button("Cancel", self.reject)
        self.add_button("Exclude", self.accept, primary=True)
        self.folders.setFocus()

    def chosen_folder(self) -> Path | None:
        item = self.folders.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def edit_pattern(self) -> None:
        """Edit the chosen folder into an exclusion pattern; accepting it closes the picker."""
        folder = self.chosen_folder()
        editor = PatternDialog(str(folder) if folder else "", self)
        pattern = editor.field.text().strip() if run_modal(editor) else ""
        editor.deleteLater()
        if pattern:
            self.pattern = pattern
            self.accept()


class PatternDialog(InfoDialog):
    """Edit a path into an exclusion pattern before it is added."""

    def __init__(self, text: str, parent=None):
        super().__init__(
            "Custom exclusion rule",
            "Edit the rule before it is added to the excluded patterns.",
            badge(EXCLUDE),
            parent,
            width=PATTERN_DIALOG_WIDTH,
        )
        self.field = QLineEdit(text)
        self.field.setObjectName("code")
        self.field.setAccessibleName("Exclusion rule")
        self.add_widget(self.field)
        help_text = QLabel(PATTERN_HELP)
        help_text.setObjectName("infoText")
        help_text.setWordWrap(True)
        self.add_widget(help_text)
        self.add_button("Cancel", self.reject)
        self.add_button("Add rule", self.accept, primary=True)
        self.field.setFocus()
        self.field.selectAll()
