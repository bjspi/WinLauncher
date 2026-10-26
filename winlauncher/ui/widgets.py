"""Building blocks of WinLauncher's settings look: line icons, switches, cards and rows.

The search bar has its own painter; these pieces serve dialogs and menus, so they
share one visual language: white cards on a soft gray canvas, one accent color,
and quiet descriptions under every setting.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QByteArray,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QMouseEvent, QPainter, QPixmap, QShowEvent
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.ui.theme import colors, theme

ICONS = Path(__file__).with_name("assets") / "icons"
ICON_TINT_PLACEHOLDER = "#000000"  # The stroke color the SVG files are drawn with.
ICON_SIZES = (16, 18, 20, 24, 32)

SWITCH_SIZE = QSize(38, 22)
SWITCH_KNOB_MARGIN = 3  # Between the knob and the track's edge.
SWITCH_ANIMATION_MS = 140
CARD_PADDING = 16
INFO_ICON_SIZE = 40
INFO_DIALOG_WIDTH = 480
INFO_DIALOG_MARGIN = 24
INFO_HEADER_SPACING = 16  # Between the icon and the texts.
ROW_SPACING = 16  # Between a row's text and its control.
ENTRY_ICON_SIZE = QSize(28, 28)
HEADER_ICON_SIZE = 28  # The icon before a foldable card's title.
FOLD_ICON_SIZE = QSize(16, 16)
ENTRY_ROW_HEIGHT = 44  # Two lines, such as a name and its state.
ENTRY_VISIBLE_ROWS = 5  # More entries scroll.


def pick_path(parent: QWidget, title: str, filters: str | None = None) -> Path | None:
    """A file matching `filters`, or a folder without them; None if cancelled.

    The picker is modal only to `parent`'s window. QFileDialog's static functions
    make it modal to the whole application, which blocks the search bar, so Quick
    Switch could not search for the file while the picker is open.
    """
    dialog = QFileDialog(parent, title)
    if filters is None:
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly)
    else:
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilters(filters.split(";;"))
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    chosen = dialog.selectedFiles() if dialog.exec() else []
    dialog.deleteLater()
    return Path(chosen[0]) if chosen else None


def line_icon(name: str, color: str | None = None, selected_color: str | None = None) -> QIcon:
    """A tinted line icon from `assets/icons`; `selected_color` tints the selected state.

    Without a color it takes the active theme's faint text color, as it is now:
    callers that keep the icon set it again when the theme changes.
    """
    color = color or colors().faint
    return _tinted_icon(name, color, selected_color or color)


@cache
def _tinted_icon(name: str, color: str, selected_color: str) -> QIcon:
    icon = QIcon()
    for mode, tint in ((QIcon.Mode.Normal, color), (QIcon.Mode.Selected, selected_color)):
        svg = (
            (ICONS / f"{name}.svg").read_text(encoding="utf-8").replace(ICON_TINT_PLACEHOLDER, tint)
        )
        renderer = QSvgRenderer(QByteArray(svg.encode()))
        for size in ICON_SIZES:
            pixmap = QPixmap(size * 2, size * 2)  # Sharp on high-DPI screens.
            pixmap.setDevicePixelRatio(2)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


class ToggleSwitch(QCheckBox):
    """A checkbox drawn as an animated on/off switch; it keeps QCheckBox's API.

    The label belongs to the setting row beside it, so the switch draws no text;
    give it an accessible name instead.
    """

    def __init__(self, accessible_name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setAccessibleName(accessible_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(SWITCH_SIZE)
        self._position = 0.0  # 0 is off, 1 is on; animated in between.
        self._animation = QPropertyAnimation(self, b"position", self)
        self._animation.setDuration(SWITCH_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = Property(float, _get_position, _set_position)

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt override.
        super().setChecked(checked)
        if not self.isVisible():
            self._animation.stop()
            self._position = 1.0 if checked else 0.0  # No animation while building a page.

    def _animate(self, checked: bool) -> None:
        if not self.isVisible():
            self._position = 1.0 if checked else 0.0
            return
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def hitButton(self, position) -> bool:  # noqa: N802 - Qt override.
        return self.rect().contains(position)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override.
        return SWITCH_SIZE

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = track.height() / 2
        off, on = QColor(colors().switch_off), QColor(colors().accent)
        mixed = QColor(
            *(
                round(a + (b - a) * self._position)
                for a, b in zip(off.getRgb()[:3], on.getRgb()[:3], strict=True)
            )
        )
        if not self.isEnabled():
            mixed.setAlpha(110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mixed)
        painter.drawRoundedRect(track, radius, radius)
        if self.hasFocus():
            painter.setPen(QColor(colors().accent_strong))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track, radius, radius)
        knob = track.height() - 2 * SWITCH_KNOB_MARGIN
        travel = track.width() - knob - 2 * SWITCH_KNOB_MARGIN
        center = QPointF(
            track.left() + SWITCH_KNOB_MARGIN + knob / 2 + travel * self._position,
            track.center().y(),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors().on_accent))
        painter.drawEllipse(center, knob / 2, knob / 2)


class PageHeader(QWidget):
    """A page's title and a one-line explanation."""

    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        explanation = QLabel(subtitle)
        explanation.setObjectName("pageSubtitle")
        explanation.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(explanation)


class Card(QFrame):
    """A rounded group of settings; rows are separated by hairlines.

    `add_row` puts a control beside its title and description; `add_block` puts
    a larger widget, such as a list or an editor, below them.
    """

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(CARD_PADDING, 6, CARD_PADDING, 6)
        self._layout.setSpacing(0)
        self.title = title

    def add_row(self, title: str, description: str, control: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(ROW_SPACING)
        layout.addLayout(_texts(title, description), 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self._append(row)
        return row

    def add_block(self, title: str, description: str, content: QWidget) -> QWidget:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(8)
        if title or description:
            layout.addLayout(_texts(title, description))
        layout.addWidget(content)
        self._append(block)
        return block

    def add_header(self, icon: QIcon, title: str, description: str, control: QWidget) -> FoldButton:
        """The first row of a foldable card: icon, texts, a fold button, then `control`.

        The rows added after it are the card's details; `show_details` shows or
        hides them. Clicking the texts works like the fold button.
        """
        header = _ClickableRow()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(ROW_SPACING)
        picture = QLabel()
        picture.setPixmap(icon.pixmap(HEADER_ICON_SIZE, HEADER_ICON_SIZE))
        layout.addWidget(picture, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(_texts(title, description), 1)
        fold = FoldButton(f"Details of {title}")
        header.clicked.connect(fold.click)
        layout.addWidget(fold, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self._append(header)
        return fold

    def show_details(self, visible: bool) -> None:
        """Show or hide every row below the header."""
        for index in range(1, self._layout.count()):
            self._layout.itemAt(index).widget().setVisible(visible)

    def _append(self, widget: QWidget) -> None:
        if self._layout.count():
            divider = QFrame()
            divider.setObjectName("rowDivider")
            divider.setFixedHeight(1)
            self._layout.addWidget(divider)
        self._layout.addWidget(widget)


class FoldButton(QToolButton):
    """A chevron that unfolds a card's details while checked."""

    def __init__(self, accessible_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("foldButton")
        self.setAccessibleName(accessible_name)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(FOLD_ICON_SIZE)
        self.toggled.connect(self._use_icon)
        theme().changed.connect(self._use_icon)
        self._use_icon()

    def _use_icon(self) -> None:
        self.setIcon(line_icon("chevron-down" if self.isChecked() else "chevron-right"))


class _ClickableRow(QWidget):
    """A row that reports clicks on its empty parts and texts."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override.
        if event.button() is Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class EntryList(QListWidget):
    """Two-line entries with an icon, such as plugins; as tall as its entries, up to a limit."""

    def __init__(self, accessible_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("itemList")
        self.setAccessibleName(accessible_name)
        self.setIconSize(ENTRY_ICON_SIZE)
        self.setSpacing(1)
        self.fit_height()

    def add_entry(self, icon: QIcon, text: str) -> None:
        item = QListWidgetItem(icon, text)
        item.setSizeHint(QSize(0, ENTRY_ROW_HEIGHT))
        self.addItem(item)
        self.fit_height()

    def clear_entries(self) -> None:
        self.clear()
        self.fit_height()

    def fit_height(self) -> None:
        """Room for every entry up to the limit, so a short list never scrolls.

        The frame comes from the style sheet, which applies only once the list is
        shown; `showEvent` therefore measures again.
        """
        rows = min(max(1, self.count()), ENTRY_VISIBLE_ROWS)
        row = ENTRY_ROW_HEIGHT + 2 * self.spacing()
        self.setFixedHeight(rows * row + 2 * self.frameWidth())

    def showEvent(self, event: QShowEvent) -> None:
        self.fit_height()
        super().showEvent(event)


def section_title(text: str) -> QLabel:
    """A small heading above a card."""
    label = QLabel(text)
    label.setObjectName("cardTitle")
    return label


def _texts(title: str, description: str) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(2)
    if title:
        heading = QLabel(title)
        heading.setObjectName("rowTitle")
        layout.addWidget(heading)
    if description:
        detail = QLabel(description)
        detail.setObjectName("rowDescription")
        detail.setWordWrap(True)
        layout.addWidget(detail)
    return layout


class InfoDialog(QDialog):
    """A small dialog with WinLauncher's look: header, cards of facts, a button bar.

    For status, information and message windows that a stock message box would
    render as a big system icon above a block of text. The body holds a 40 px
    icon, a heading and a text above any cards; the bar below carries the
    buttons, with link-style or plain helper buttons at its left.
    """

    def __init__(
        self,
        title: str,
        subtitle: str,
        icon: QIcon,
        parent: QWidget | None = None,
        *,
        width: int = INFO_DIALOG_WIDTH,
    ):
        super().__init__(parent)
        self.setObjectName("infoDialog")
        self.setWindowTitle(title)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        body = QWidget()
        body.setObjectName("infoBody")
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(INFO_DIALOG_MARGIN, 22, INFO_DIALOG_MARGIN, 20)
        self._layout.setSpacing(16)
        header = QHBoxLayout()
        header.setSpacing(INFO_HEADER_SPACING)
        logo = QLabel()
        logo.setFixedSize(INFO_ICON_SIZE, INFO_ICON_SIZE)
        logo.setPixmap(icon.pixmap(INFO_ICON_SIZE, INFO_ICON_SIZE))
        header.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("infoTitle")
        heading.setWordWrap(True)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("infoText")
        self.subtitle.setWordWrap(True)
        self.subtitle.setOpenExternalLinks(True)
        self.subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        texts.addWidget(heading)
        texts.addWidget(self.subtitle)
        texts.addStretch(1)
        header.addLayout(texts, 1)
        self._layout.addLayout(header)
        # Wrapping labels get an explicit width: nested layouts misjudge their height otherwise.
        self._wrapped = [heading, self.subtitle]
        self._content: list[QWidget] = []
        root.addWidget(body)
        footer = QWidget()
        footer.setObjectName("infoFooter")
        self._buttons = QHBoxLayout(footer)
        self._buttons.setContentsMargins(INFO_DIALOG_MARGIN, 12, INFO_DIALOG_MARGIN, 12)
        self._buttons.setSpacing(8)
        self._buttons.addStretch(1)
        self._left = 0  # Helper buttons before the stretch.
        root.addWidget(footer)
        self.set_width(width)

    def set_width(self, width: int) -> None:
        """Fix the dialog, its texts and its content to `width`; the height follows."""
        self.content_width = width - 2 * INFO_DIALOG_MARGIN
        for label in self._wrapped:
            if label is not None:
                label.setFixedWidth(self.content_width - INFO_ICON_SIZE - INFO_HEADER_SPACING)
        for widget in self._content:
            widget.setFixedWidth(self.content_width)
        self.setFixedWidth(width)
        self.adjustSize()

    def add_facts(self, facts: list[tuple[str, str]]) -> QFrame:
        """A card of label/value rows; values are selectable rich text (links open)."""
        card = QFrame()
        card.setObjectName("factCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 12, 16, 12)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        for row, (label, value) in enumerate(facts):
            name = QLabel(label)
            name.setObjectName("factLabel")
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            text = QLabel(tint_links(value))
            text.setObjectName("factValue")
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setOpenExternalLinks(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            text.setWordWrap(True)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        self.add_widget(card)
        return card

    def add_widget(self, widget: QWidget) -> None:
        widget.setFixedWidth(self.content_width)  # The dialog is as wide as its content.
        self._content.append(widget)
        self._layout.addWidget(widget)

    def add_button(self, text: str, slot, *, primary: bool = False) -> QPushButton:
        """A button at the right of the bar; the primary one is filled and answers Enter."""
        button = self._button(text, slot)
        if primary:
            button.setObjectName("primary")
            button.setDefault(True)
        self._buttons.addWidget(button)
        return button

    def add_helper(self, text: str, slot, *, link: bool = False) -> QPushButton:
        """A button at the left of the bar, such as "Open data folder" or a details toggle."""
        button = self._button(text, slot)
        if link:
            button.setObjectName("linkButton")
        self._buttons.insertWidget(self._left, button)
        self._left += 1
        return button

    @staticmethod
    def _button(text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(slot)
        # Only the primary button is the default, however the focus moves.
        button.setAutoDefault(False)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def finish(self) -> None:
        """Fit the height to the wrapped text."""
        self.adjustSize()

    def exec(self) -> int:
        self.finish()
        return super().exec()


def tint_links(text: str) -> str:
    """Links in the accent color; Qt's default link blue is unreadable on the dark theme."""
    return text.replace("<a ", f'<a style="color: {colors().accent}; text-decoration: none;" ')
