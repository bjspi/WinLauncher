"""Messages and questions in WinLauncher's look instead of stock message boxes.

A stock box shows a big system icon above a block of text and Yes/No buttons. These
dialogs reuse InfoDialog: a tinted icon badge, a heading with the text below it,
optional details folded behind a toggle, and buttons that name their action.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QPlainTextEdit, QPushButton, QWidget

from winlauncher.ui.dialogs import run_modal
from winlauncher.ui.theme import colors
from winlauncher.ui.widgets import INFO_DIALOG_WIDTH, INFO_ICON_SIZE, InfoDialog, line_icon

DETAILS_WIDTH = 640  # Shown details widen the dialog: diffs and git output need room.
DETAILS_HEIGHT = 280  # The details scroll instead of growing past the screen.
BADGE_ICON_SIZE = 22
BADGE_RADIUS = 10
BADGE_ALPHA = 36  # Of 255: a light tint of the kind's color behind its icon.

PRIMARY = "primary"  # The filled main action; Enter runs it.
SECONDARY = "secondary"
DANGER = "danger"  # Red outline; Enter then takes the safe way out instead.


@dataclass(frozen=True, slots=True)
class Kind:
    icon: str
    color: Callable[[], str]  # Read when shown, so it follows the active theme.


INFO = Kind("info", lambda: colors().accent)
QUESTION = Kind("help", lambda: colors().accent)
UPDATE = Kind("download", lambda: colors().accent)
SUCCESS = Kind("check", lambda: colors().success)
WARNING = Kind("alert", lambda: colors().warning)
ERROR = Kind("alert", lambda: colors().invalid)


def badge(kind: Kind) -> QIcon:
    """The kind's line icon on a rounded square tinted in its color."""
    color = kind.color()
    pixmap = QPixmap(INFO_ICON_SIZE * 2, INFO_ICON_SIZE * 2)  # Sharp on high-DPI screens.
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    tint = QColor(color)
    tint.setAlpha(BADGE_ALPHA)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(tint)
    painter.drawRoundedRect(
        QRectF(0, 0, INFO_ICON_SIZE, INFO_ICON_SIZE), BADGE_RADIUS, BADGE_RADIUS
    )
    offset = (INFO_ICON_SIZE - BADGE_ICON_SIZE) / 2
    icon = line_icon(kind.icon, color).pixmap(BADGE_ICON_SIZE * 2, BADGE_ICON_SIZE * 2)
    painter.drawPixmap(
        QRectF(offset, offset, BADGE_ICON_SIZE, BADGE_ICON_SIZE), icon, QRectF(icon.rect())
    )
    painter.end()
    return QIcon(pixmap)


class MessageDialog(InfoDialog):
    """A heading and text with buttons; `clicked` names the button that closed it.

    `actions` are helper buttons at the left of the bar that keep the dialog open.
    """

    def __init__(
        self,
        title: str,
        text: str,
        buttons: Sequence[tuple[str, str]],
        kind: Kind = INFO,
        parent: QWidget | None = None,
        *,
        details: str | Callable[[], str] | None = None,
        details_labels: tuple[str, str] = ("Details", "Hide details"),
        window_title: str = "WinLauncher",
        actions: Sequence[tuple[str, Callable[[], None]]] = (),
    ):
        super().__init__(title, text, badge(kind), parent)
        self.setWindowTitle(window_title)
        self.clicked: str | None = None
        self.details: QPlainTextEdit | None = None
        self.details_button: QPushButton | None = None
        if details is not None:
            self._load_details = details if callable(details) else lambda: details
            self._labels = details_labels
            self.details = QPlainTextEdit()
            self.details.setObjectName("code")
            self.details.setReadOnly(True)
            self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.details.setAccessibleName(details_labels[0])
            self.details.setFixedHeight(DETAILS_HEIGHT)
            self.details.hide()
            self.add_widget(self.details)
            # Its text names what a click does: show the details, or hide them again.
            self.details_button = self.add_helper(details_labels[0], self.toggle_details, link=True)
        for text_, slot in actions:
            self.add_helper(text_, slot)
        self.buttons: list[QPushButton] = []
        for text_, style in buttons:
            button = self.add_button(text_, lambda _checked=False, t=text_: self._choose(t))
            if style in (PRIMARY, DANGER):
                button.setObjectName(style)
            self.buttons.append(button)
        # Enter runs the main action; a destructive question without one cancels instead.
        styles = [style for _text, style in buttons]
        default_style = PRIMARY if PRIMARY in styles else SECONDARY
        default = next(
            (b for b, style in zip(self.buttons, styles, strict=True) if style == default_style),
            self.buttons[-1] if self.buttons else None,
        )
        if default is not None:
            default.setDefault(True)
            default.setFocus()

    def toggle_details(self) -> None:
        if self.details is None or self.details_button is None:
            return
        if not self.details.toPlainText():
            self.details.setPlainText(self._load_details())
        shown = not self.details.isVisible()
        self.details.setVisible(shown)
        self.details_button.setText(self._labels[shown])
        self.set_width(DETAILS_WIDTH if shown else INFO_DIALOG_WIDTH)

    def _choose(self, text: str) -> None:
        self.clicked = text
        self.accept()


def show_message(
    parent: QWidget | None,
    title: str,
    text: str,
    buttons: Sequence[tuple[str, str]],
    kind: Kind = INFO,
    **options,
) -> str | None:
    """Show a message dialog; the clicked button's text, or None when it was closed."""
    dialog = MessageDialog(title, text, buttons, kind, parent, **options)
    run_modal(dialog)
    clicked = dialog.clicked
    dialog.deleteLater()
    return clicked


def message(
    parent: QWidget | None, title: str, text: str, kind: Kind = INFO, button: str = "OK"
) -> None:
    """A message with one acknowledging button."""
    show_message(parent, title, text, [(button, PRIMARY)], kind)


def ask(
    parent: QWidget | None,
    title: str,
    text: str,
    confirm: str,
    cancel: str = "Cancel",
    kind: Kind = QUESTION,
) -> bool:
    """A question; warnings and errors get a red confirm button and Enter cancels."""
    style = DANGER if kind in (WARNING, ERROR) else PRIMARY
    return (
        show_message(parent, title, text, [(cancel, SECONDARY), (confirm, style)], kind) == confirm
    )
