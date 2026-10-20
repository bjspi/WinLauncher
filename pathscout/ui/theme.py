"""Light launcher theme and PathScout's application icon."""

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap


class Palette:
    """Colors the result painter needs; the stylesheet below uses the same values."""

    TEXT = "#1f2329"
    MUTED = "#7d848d"
    SHORTCUT = "#a3a9b1"
    ACCENT = "#2f7de1"
    SELECTED = "#e2e7ee"
    HOVER = "#edf0f4"


# Stylesheets replace the native indicator, so the check mark is PathScout's own SVG.
ASSETS = Path(__file__).with_name("assets")
CHECK_MARK = (ASSETS / "check.svg").as_posix()
CHECK_DARK = (ASSETS / "check-dark.svg").as_posix()

STYLESHEET = """
QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: #1f2329; }
QDialog { background: #f6f7f9; }
QFrame#surface { background: #f7f8fa; border: 1px solid #d5dae1; border-radius: 10px; }
QFrame#divider { background: #e3e6ea; border: 0; margin: 0 16px; }
QFrame#calculation { background: transparent; border: 0; }
QFrame#calculation[selected="true"] { background: #e2e7ee; }
QLabel#calculationResult { font-size: 15pt; color: #1f2329; }
QLabel#calculationHint { font-size: 8pt; color: #a3a9b1; }
QLineEdit#query { background: transparent; border: 0; padding: 4px 0; font-size: 18pt; color: #1f2329; selection-background-color: #bcd5f5; selection-color: #1f2329; }
QLineEdit#query[explorer="true"] { font-size: 10pt; padding: 1px 0; }
QToolButton#menuButton { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 3px; }
QToolButton#menuButton:hover { background: #e9edf2; }
QToolButton#menuButton:focus { border-color: #2f7de1; }
QToolButton#menuButton::menu-indicator { image: none; width: 0; }
QToolButton#scope { background: #e6eef9; color: #2a5da8; border: 1px solid transparent; border-radius: 11px; padding: 3px 10px; font-size: 9pt; }
QToolButton#scope:hover { background: #d8e6f7; }
QToolButton#scope:focus { border-color: #2f7de1; }
QToolButton#scope[explorer="true"] { padding: 2px 6px; font-size: 8pt; }
QToolButton#kindFilter { background: transparent; border: 1px solid transparent; border-radius: 5px; padding: 4px 7px; }
QToolButton#kindFilter:hover { background: #e9edf2; }
QToolButton#kindFilter:checked { background: #dbe8f8; border-color: #9bbce8; }
QToolButton#kindFilter:focus { border-color: #2f7de1; }
QToolButton#kindFilter[explorer="true"] { padding: 2px 4px; }
QListWidget#results { background: transparent; border: 0; outline: none; padding: 0; }
QLabel#message { color: #5f6670; padding: 14px 22px; }
QLabel#message[explorer="true"] { font-size: 9pt; padding: 8px 10px; }
QLabel#detail { color: #5f6670; font-size: 9pt; padding: 6px 22px 0 22px; }
QLabel#status { color: #9aa0a8; font-size: 8pt; padding: 0; }
QLabel#activity { color: #a15c00; font-size: 8pt; padding: 4px 22px 0 22px; }
QLabel#muted { color: #6b727c; font-size: 9pt; }
QLineEdit { background: #ffffff; border: 1px solid #cfd5dd; border-radius: 6px; padding: 5px 8px; selection-background-color: #bcd5f5; selection-color: #1f2329; }
QLineEdit:focus { border-color: #2f7de1; }
QPlainTextEdit, QListWidget { background: #ffffff; border: 1px solid #cfd5dd; border-radius: 6px; padding: 4px; selection-background-color: #dbe8f8; selection-color: #1f2329; }
QPlainTextEdit:focus, QListWidget:focus { border-color: #2f7de1; }
QPushButton { background: #ffffff; border: 1px solid #cfd5dd; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: #eef2f7; }
QPushButton:focus { border-color: #2f7de1; }
QPushButton:default { background: #2f7de1; border-color: #2f7de1; color: #ffffff; }
QPushButton:disabled { color: #a3a9b1; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #9aa3ae; border-radius: 4px; background: #ffffff; }
QCheckBox::indicator:checked { background: #2f7de1; border-color: #2f7de1; image: url(CHECK_MARK); }
QCheckBox::indicator:focus { border: 2px solid #2f7de1; }
QCheckBox:disabled { color: #a3a9b1; }
QTabWidget::pane { border: 1px solid #d5dae1; border-radius: 6px; background: #fbfbfc; padding: 8px; }
QTabBar::tab { background: transparent; padding: 8px 14px; color: #5f6670; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #1f2329; border-bottom-color: #2f7de1; }
QTabBar::tab:focus { color: #2f7de1; }
QMenu { background: #f9f9fb; border: 1px solid #e0e3e8; padding: 4px; font-size: 9pt; }
QMenu::item { padding: 7px 28px 7px 34px; border-radius: 4px; }
QMenu::item:selected { background: #e8ebf0; color: #1f2329; }
QMenu::item:disabled { color: #a3a9b1; }
QMenu::indicator { width: 16px; height: 16px; left: 10px; }
QMenu::indicator:checked { image: url(CHECK_DARK); }
QMenu::separator { height: 1px; background: #e3e6ea; margin: 4px 10px; }
QToolTip { background: #ffffff; color: #1f2329; border: 1px solid #cfd5dd; padding: 5px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #c6ccd4; border-radius: 3px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #aab2bc; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
""".replace("CHECK_MARK", CHECK_MARK).replace("CHECK_DARK", CHECK_DARK)


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_folder_scout(size))
    return icon


def _gradient(start: QPointF, end: QPointF, *stops: tuple[float, str]) -> QLinearGradient:
    gradient = QLinearGradient(start, end)
    for position, color in stops:
        gradient.setColorAt(position, QColor(color))
    return gradient


def _folder_outline() -> QPainterPath:
    """An open folder outline: left side, tab, top edge, right side, part of the bottom."""
    path = QPainterPath(QPointF(19, 47))
    path.lineTo(17, 47)
    path.quadTo(13, 47, 13, 43)
    path.lineTo(13, 21)
    path.quadTo(13, 17, 17, 17)
    path.lineTo(25, 17)
    path.quadTo(27, 17, 28.5, 19)
    path.lineTo(30.5, 22)
    path.lineTo(47, 22)
    path.quadTo(51, 22, 51, 26)
    path.lineTo(51, 41)
    return path


def _folder_scout(size: int) -> QPixmap:
    """PathScout's icon: a neon folder outline with a magnifier, on a 64-unit grid."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)
    background = _gradient(QPointF(0, 0), QPointF(0, 64), (0, "#1b2338"), (1, "#0a0e1a"))
    painter.setPen(QPen(QColor("#2a3552"), 0.8))
    painter.setBrush(background)
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 15, 15)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#17213b"))
    painter.drawRoundedRect(QRectF(17.5, 27, 26, 18), 3, 3)  # Front of the folder.
    neon = _gradient(
        QPointF(51, 17), QPointF(13, 47), (0, "#2fe8f0"), (0.5, "#3b8bf6"), (1, "#8b5cf6")
    )
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(neon, 3.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawPath(_folder_outline())
    lens = _gradient(QPointF(26, 33), QPointF(46, 54), (0, "#38bdf8"), (1, "#7c5cf6"))
    pen = QPen(lens, 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawEllipse(QPointF(34, 41), 8.5, 8.5)
    painter.drawLine(QPointF(40.5, 47.5), QPointF(47.5, 54.5))
    painter.end()
    return pixmap
