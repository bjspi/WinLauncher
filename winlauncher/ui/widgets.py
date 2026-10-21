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
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.ui.theme import colors

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
FACT_LABEL_WIDTH = 150
# A wrapping label claims the height it would need at its minimum width; keep it sane.
SUBTITLE_MIN_WIDTH = 320
ROW_SPACING = 16  # Between a row's text and its control.


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

    def add_fact(self, label: str, value: QWidget) -> QWidget:
        """A label beside a value that takes the remaining width, such as a path."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(ROW_SPACING)
        name = QLabel(label)
        name.setObjectName("rowTitle")
        name.setFixedWidth(FACT_LABEL_WIDTH)
        layout.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(value, 1)
        self._append(row)
        return row

    def _append(self, widget: QWidget) -> None:
        if self._layout.count():
            divider = QFrame()
            divider.setObjectName("rowDivider")
            divider.setFixedHeight(1)
            self._layout.addWidget(divider)
        self._layout.addWidget(widget)


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
    """A small dialog with WinLauncher's look: header, cards of facts, buttons.

    For status and information windows that a stock message box would render as
    a big system icon above a block of text.
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
        self.content_width = width - 2 * INFO_DIALOG_MARGIN
        self.setObjectName("infoDialog")
        self.setWindowTitle(title)
        self._layout = QVBoxLayout(self)
        # Fixed to the content: wrapped text gets exactly the height it needs.
        self._layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self._layout.setContentsMargins(INFO_DIALOG_MARGIN, 22, INFO_DIALOG_MARGIN, 18)
        self._layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(icon.pixmap(INFO_ICON_SIZE, INFO_ICON_SIZE))
        logo.setAlignment(Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("infoTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setMinimumWidth(SUBTITLE_MIN_WIDTH)
        texts.addWidget(heading)
        texts.addWidget(self.subtitle)
        header.addWidget(logo)
        header.addLayout(texts, 1)
        self._layout.addLayout(header)
        self._buttons = QHBoxLayout()
        self._buttons.addStretch(1)

    def add_facts(self, facts: list[tuple[str, str]]) -> Card:
        """A card of label/value rows; values are selectable rich text (links open)."""
        card = Card()
        for label, value in facts:
            text = QLabel(value)
            text.setObjectName("factValue")
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setOpenExternalLinks(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            text.setWordWrap(True)
            card.add_fact(label, text)
        self.add_widget(card)
        return card

    def add_widget(self, widget: QWidget) -> None:
        widget.setFixedWidth(self.content_width)  # The dialog is as wide as its content.
        self._layout.addWidget(widget)

    def add_button(self, text: str, slot, *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(slot)
        if primary:
            button.setDefault(True)
        self._buttons.addWidget(button)
        return button

    def finish(self) -> None:
        """Add the buttons below everything else and fit the height to the wrapped text."""
        if self._buttons.parent() is None:
            self._layout.addLayout(self._buttons)
        self.adjustSize()

    def exec(self) -> int:
        self.finish()
        return super().exec()
