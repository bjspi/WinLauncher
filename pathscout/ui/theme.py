"""Light launcher theme and PathScout's application icon."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


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
QLineEdit#query { background: transparent; border: 0; padding: 4px 0; font-size: 18pt; color: #1f2329; selection-background-color: #bcd5f5; selection-color: #1f2329; }
QLineEdit#query[explorer="true"] { font-size: 12pt; padding: 2px 0; }
QToolButton#menuButton { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 3px; }
QToolButton#menuButton:hover { background: #e9edf2; }
QToolButton#menuButton:focus { border-color: #2f7de1; }
QToolButton#menuButton::menu-indicator { image: none; width: 0; }
QToolButton#scope { background: #e6eef9; color: #2a5da8; border: 1px solid transparent; border-radius: 11px; padding: 3px 10px; font-size: 9pt; }
QToolButton#scope:hover { background: #d8e6f7; }
QToolButton#scope:focus { border-color: #2f7de1; }
QListWidget#results { background: transparent; border: 0; outline: none; padding: 0; }
QLabel#message { color: #5f6670; padding: 14px 22px; }
QLabel#detail { color: #5f6670; font-size: 9pt; padding: 6px 22px 0 22px; }
QLabel#status { color: #9aa0a8; font-size: 8pt; padding: 4px 22px 0 22px; }
QLabel#activity { color: #a15c00; font-size: 8pt; padding: 4px 22px 0 22px; }
QLabel#muted { color: #6b727c; font-size: 9pt; }
QLineEdit, QSpinBox { background: #ffffff; border: 1px solid #cfd5dd; border-radius: 6px; padding: 5px 8px; selection-background-color: #bcd5f5; selection-color: #1f2329; }
QLineEdit:focus, QSpinBox:focus { border-color: #2f7de1; }
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
        icon.addPixmap(_magnifier(size))
    return icon


def _magnifier(size: int) -> QPixmap:
    """PathScout's icon: a magnifier on a rounded square, drawn on a 64-unit grid."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)
    painter.setBrush(QColor("#2f7de1"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(1, 1, 62, 62, 15, 15)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#ffffff"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawEllipse(15, 13, 27, 27)
    painter.drawLine(38, 37, 50, 50)
    painter.end()
    return pixmap
