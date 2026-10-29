"""Light and dark launcher themes and WinLauncher's application icon.

`Theme` applies the colors the user chose, or those of the Windows color scheme,
to the stylesheet and Qt's palette, and tells painters to refresh. Painting code
reads the active colors through `colors()` each time it paints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)

from winlauncher.settings import Appearance


@dataclass(frozen=True, slots=True)
class Colors:
    # Text, from the strongest to the faintest.
    title: str
    text: str
    secondary: str
    muted: str  # Messages, tabs and line icons.
    subtle: str  # Descriptions and units.
    faint: str  # Result paths.
    fainter: str
    status: str
    hint: str  # Shortcut hints and disabled text.
    on_accent: str
    # The accent and its states.
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_strong: str  # Selected navigation, focus rings.
    # Backgrounds.
    page: str  # Settings and info dialogs.
    dialog: str
    surface: str  # The launcher.
    field: str  # Inputs, lists, buttons and cards.
    menu: str
    pane: str
    sidebar: str
    # Borders and lines.
    surface_border: str
    field_border: str
    button_border: str
    checkbox_border: str
    menu_border: str
    sidebar_border: str
    card_border: str
    divider: str
    row_divider: str
    results_divider: str  # Between an Explorer folder's direct entries and subfolder matches.
    # States.
    selected: str
    hover: str
    text_selection: str
    tool_hover: str
    button_hover: str
    button_pressed: str
    menu_selected: str
    nav_hover: str
    nav_selected_border: str
    chosen: str  # A checked filter or a selected list row.
    chosen_border: str
    scope: str
    scope_hover: str
    scope_text: str
    scrollbar: str
    scrollbar_hover: str
    switch_off: str
    # Messages.
    warning: str
    success: str
    # Query syntax in the search field: filters, content searches, exclusions, errors.
    filter: str
    filter_background: str
    content: str
    content_background: str
    exclusion: str
    exclusion_background: str
    invalid: str
    # SVGs in `assets`: the check mark in menus and the arrow of drop-down lists.
    menu_check: str
    combo_arrow: str


LIGHT = Colors(
    title="#15191e",
    text="#1f2329",
    secondary="#3a4049",
    muted="#5f6670",
    subtle="#6b727c",
    faint="#7d848d",
    fainter="#8a919b",
    status="#9aa0a8",
    hint="#a3a9b1",
    on_accent="#ffffff",
    accent="#2f7de1",
    accent_hover="#2a72cf",
    accent_pressed="#2565b8",
    accent_strong="#1f5fb8",
    page="#f3f5f8",
    dialog="#f6f7f9",
    surface="#f7f8fa",
    field="#ffffff",
    menu="#f9f9fb",
    pane="#fbfbfc",
    sidebar="#eaedf2",
    surface_border="#d5dae1",
    field_border="#cfd5dd",
    button_border="#d5dbe3",
    checkbox_border="#9aa3ae",
    menu_border="#e0e3e8",
    sidebar_border="#dfe3e9",
    card_border="#e3e7ed",
    divider="#e3e6ea",
    row_divider="#eef1f4",
    results_divider="#7aa7e8",
    selected="#e2e7ee",
    hover="#edf0f4",
    text_selection="#bcd5f5",
    tool_hover="#e9edf2",
    button_hover="#f1f4f8",
    button_pressed="#e6ebf1",
    menu_selected="#e8ebf0",
    nav_hover="#e0e5ec",
    nav_selected_border="#dbe1e8",
    chosen="#dbe8f8",
    chosen_border="#9bbce8",
    scope="#e6eef9",
    scope_hover="#d8e6f7",
    scope_text="#2a5da8",
    scrollbar="#c6ccd4",
    scrollbar_hover="#aab2bc",
    switch_off="#c5ccd6",
    warning="#a15c00",
    success="#1a7f4b",
    filter="#1f5fb8",
    filter_background="#e3edfb",
    content="#6a3fc1",
    content_background="#eee8fb",
    exclusion="#a15c00",
    exclusion_background="#fbf0e1",
    invalid="#c62828",
    menu_check="check-dark.svg",
    combo_arrow="chevron-dark.svg",
)

DARK = Colors(
    title="#f4f5f7",
    text="#e6e8eb",
    secondary="#c9ced5",
    muted="#aab1ba",
    subtle="#9ea5ae",
    faint="#8f969f",
    fainter="#868d97",
    status="#7c838d",
    hint="#737a84",
    on_accent="#ffffff",
    accent="#4a90e8",
    accent_hover="#5a9bec",
    accent_pressed="#3d82dc",
    accent_strong="#8bbcff",
    page="#1c1f24",
    dialog="#1f2227",
    surface="#24272d",
    field="#2a2e35",
    menu="#2a2d33",
    pane="#24272d",
    sidebar="#181a1e",
    surface_border="#3a3f47",
    field_border="#474d56",
    button_border="#474d56",
    checkbox_border="#6c7480",
    menu_border="#3e434b",
    sidebar_border="#2b2f35",
    card_border="#31363d",
    divider="#353a42",
    row_divider="#2e3238",
    results_divider="#4f82c6",
    selected="#353c47",
    hover="#2d3239",
    text_selection="#2f4f7a",
    tool_hover="#353a42",
    button_hover="#343941",
    button_pressed="#3c424b",
    menu_selected="#3a3f47",
    nav_hover="#262a30",
    nav_selected_border="#3d434c",
    chosen="#27415f",
    chosen_border="#3f6ca6",
    scope="#243650",
    scope_hover="#2c4262",
    scope_text="#9cc3ff",
    scrollbar="#4a515b",
    scrollbar_hover="#5f6772",
    switch_off="#4f5661",
    warning="#e3a24f",
    success="#4cc38a",
    filter="#8bbcff",
    filter_background="#22344d",
    content="#b9a0f7",
    content_background="#30284a",
    exclusion="#e8a85a",
    exclusion_background="#3a2c19",
    invalid="#ff6b6b",
    menu_check="check.svg",
    combo_arrow="chevron-light.svg",
)

_active = LIGHT


def colors() -> Colors:
    """The colors of the active theme; read them when painting, not before."""
    return _active


# Stylesheets replace the native indicator, so the check marks are WinLauncher's own SVGs.
ASSETS = Path(__file__).with_name("assets")
CHECK_MARK = (ASSETS / "check.svg").as_posix()

_STYLESHEET = Template("""
QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: $text; }
QDialog { background: $dialog; }
QFrame#surface { background: $surface; border: 1px solid $surface_border; border-radius: 10px; }
QFrame#divider { background: $divider; border: 0; margin: 0 16px; }
QFrame#calculation { background: transparent; border: 0; }
QFrame#calculation[selected="true"] { background: $selected; }
QLabel#calculationResult { font-size: 15pt; color: $text; }
QLabel#calculationHint { font-size: 8pt; color: $hint; }
QLineEdit#query { background: transparent; border: 0; padding: 4px 0; font-size: 18pt; color: $text; selection-background-color: $text_selection; selection-color: $text; }
QLineEdit#query[explorer="true"] { font-size: 9pt; padding: 1px 0; }
QToolButton#menuButton { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 3px; }
QToolButton#menuButton:hover { background: $tool_hover; }
QToolButton#menuButton:focus { border-color: $accent; }
QToolButton#menuButton::menu-indicator { image: none; width: 0; }
QToolButton#settingsButton { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 7px; }
QToolButton#settingsButton:hover { background: $tool_hover; }
QToolButton#settingsButton:focus { border-color: $accent; }
QToolButton#pinButton { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 7px; }
QToolButton#pinButton:hover { background: $tool_hover; }
QToolButton#pinButton:checked { background: $scope; }
QToolButton#pinButton:focus { border-color: $accent; }
QToolButton#foldButton { background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 3px; }
QToolButton#foldButton:hover { background: $tool_hover; }
QToolButton#foldButton:focus { border-color: $accent; }
QToolButton#scope { background: $scope; color: $scope_text; border: 1px solid transparent; border-radius: 11px; padding: 3px 10px; font-size: 9pt; }
QToolButton#scope:hover { background: $scope_hover; }
QToolButton#scope:focus { border-color: $accent; }
QToolButton#scope[explorer="true"] { padding: 2px 6px; font-size: 8pt; }
QToolButton#kindFilter { background: transparent; border: 1px solid transparent; border-radius: 5px; padding: 4px 7px; }
QToolButton#kindFilter:hover { background: $tool_hover; }
QToolButton#kindFilter:checked { background: $chosen; border-color: $chosen_border; }
QToolButton#kindFilter:focus { border-color: $accent; }
QToolButton#kindFilter[explorer="true"] { padding: 2px 4px; }
QListWidget#results { background: transparent; border: 0; outline: none; padding: 0; }
QLabel#message { color: $muted; padding: 14px 22px; }
QLabel#message[explorer="true"] { font-size: 8pt; padding: 8px 10px; }
QLabel#detail { color: $muted; font-size: 9pt; padding: 6px 22px 0 22px; }
QLabel#status { color: $status; font-size: 8pt; padding: 0; }
QLabel#hint { color: $hint; font-size: 8pt; padding: 0; }
QLabel#activity { color: $warning; font-size: 8pt; padding: 4px 22px 0 22px; }
QLabel#muted { color: $subtle; font-size: 9pt; }
QLabel#sectionTitle { font-size: 12pt; font-weight: 600; padding: 4px 0; }
QLineEdit { background: $field; border: 1px solid $field_border; border-radius: 6px; padding: 5px 8px; selection-background-color: $text_selection; selection-color: $text; }
QLineEdit:focus { border-color: $accent; }
QPlainTextEdit, QListWidget { background: $field; border: 1px solid $field_border; border-radius: 6px; padding: 4px; selection-background-color: $chosen; selection-color: $text; }
QPlainTextEdit:focus, QListWidget:focus { border-color: $accent; }
QPushButton { background: $field; border: 1px solid $button_border; border-radius: 8px; padding: 6px 16px; min-width: 64px; }
QPushButton:hover { background: $button_hover; }
QPushButton:pressed { background: $button_pressed; }
QPushButton:focus { border-color: $accent; }
QPushButton:default { background: $accent; border-color: $accent_hover; color: $on_accent; font-weight: 600; }
QPushButton:default:hover { background: $accent_hover; }
QPushButton:default:pressed { background: $accent_pressed; }
QPushButton:default:focus { border-color: $accent_strong; }
QPushButton:disabled { color: $hint; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid $checkbox_border; border-radius: 4px; background: $field; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; image: url($check_mark); }
QCheckBox::indicator:focus { border: 2px solid $accent; }
QCheckBox:disabled { color: $hint; }
QComboBox { background: $field; border: 1px solid $button_border; border-radius: 8px; padding: 5px 10px; }
QComboBox:hover { background: $button_hover; }
QComboBox:focus { border-color: $accent; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox::down-arrow { image: url($combo_arrow); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: $menu; border: 1px solid $menu_border; selection-background-color: $menu_selected; selection-color: $text; outline: none; }
QTabWidget::pane { border: 1px solid $surface_border; border-radius: 6px; background: $pane; padding: 8px; }
QTabBar::tab { background: transparent; padding: 8px 14px; color: $muted; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: $text; border-bottom-color: $accent; }
QTabBar::tab:focus { color: $accent; }
QMenu { background: $menu; border: 1px solid $menu_border; padding: 4px; font-size: 9pt; }
QMenu::item { padding: 7px 28px 7px 34px; border-radius: 4px; }
QMenu::item:selected { background: $menu_selected; color: $text; }
QMenu::item:disabled { color: $hint; }
QMenu::indicator { width: 16px; height: 16px; left: 10px; }
QMenu::indicator:checked { image: url($menu_check); }
QMenu::separator { height: 1px; background: $divider; margin: 4px 10px; }
QMenu::icon { padding-left: 8px; }
QMenu#withIcons::item { padding-left: 6px; }  /* Qt adds the icon column itself. */
QDialog#settings { background: $page; }
QFrame#sidebar { background: $sidebar; border: 0; border-right: 1px solid $sidebar_border; }
QLabel#brandName { font-size: 12pt; font-weight: 600; color: $title; }
QLabel#brandVersion { color: $fainter; font-size: 8pt; }
QLabel#sidebarHint { color: $fainter; font-size: 8pt; padding: 0 8px; }
QListWidget#nav { background: transparent; border: 0; outline: none; padding: 0; }
QListWidget#nav::item { padding: 8px 10px; margin: 1px 0; border-radius: 8px; color: $secondary; border: 1px solid transparent; }
QListWidget#nav::item:hover { background: $nav_hover; }
QListWidget#nav::item:selected { background: $field; color: $accent_strong; border-color: $nav_selected_border; }
QScrollArea#pageScroll { background: $page; border: 0; }
QWidget#page { background: $page; }
QLabel#pageTitle { font-size: 17pt; font-weight: 600; color: $title; }
QLabel#pageSubtitle { color: $subtle; font-size: 9pt; padding-bottom: 6px; }
QLabel#cardTitle { color: $subtle; font-size: 8pt; font-weight: 600; padding: 10px 4px 0 4px; }
QFrame#card { background: $field; border: 1px solid $card_border; border-radius: 12px; }
QFrame#rowDivider { background: $row_divider; border: 0; }
QLabel#unit { color: $subtle; font-size: 9pt; min-width: 18px; }
QListWidget#itemList { outline: none; }
QListWidget#itemList::item { padding: 4px 6px; border: 0; border-radius: 6px; color: $text; }
QListWidget#itemList::item:hover { background: $button_hover; }
QListWidget#itemList::item:selected { background: $chosen; color: $text; }
QLabel#rowTitle { font-weight: 600; color: $text; }
QLabel#rowDescription { color: $subtle; font-size: 9pt; }
QFrame#footer { background: $field; border: 0; border-top: 1px solid $card_border; }
QLabel#savedMessage { color: $success; font-weight: 600; }
QDialog#settings QPushButton { padding: 7px 16px; border-radius: 8px; }
QDialog#settings QLineEdit { border-radius: 8px; padding: 6px 9px; }
QDialog#settings QPlainTextEdit, QDialog#settings QListWidget#itemList { border-radius: 8px; }
QPlainTextEdit#code { font-family: 'Cascadia Mono', 'Consolas'; font-size: 9pt; }
QMenu#tray { padding: 6px; font-size: 9pt; }
QMenu#tray::item { padding: 7px 20px 7px 10px; border-radius: 6px; }
QMenu#tray::icon { padding-left: 10px; }
QMenu#tray::indicator { width: 0; height: 0; }  /* Checks are icons, see TrayMenu. */
QWidget#trayHeader { background: transparent; }
QLabel#trayTitle { font-size: 10pt; font-weight: 600; color: $title; }
QLabel#trayStatus { font-size: 8pt; color: $faint; }
QLabel#trayVersion { font-size: 8pt; color: $fainter; }
QDialog#infoDialog { background: $field; }
QWidget#infoBody { background: $field; }
QWidget#infoFooter { background: $page; border-top: 1px solid $card_border; }
QLabel#infoTitle { font-size: 15px; font-weight: 600; color: $title; }
QLabel#infoText { font-size: 13px; color: $subtle; }
QFrame#factCard { background: $hover; border: 1px solid $card_border; border-radius: 10px; }
QLabel#factLabel { font-size: 12px; color: $subtle; }
QLabel#factValue { font-size: 12px; color: $text; }
QDialog#infoDialog QPlainTextEdit, QDialog#infoDialog QLineEdit#code {
    background: $hover; border: 1px solid $card_border; border-radius: 8px; padding: 6px 8px;
}
QDialog#infoDialog QPushButton {
    background: $field; color: $text; border: 1px solid $button_border; border-radius: 7px;
    padding: 7px 18px; min-width: 64px; font-size: 13px;
}
QDialog#infoDialog QPushButton:hover { border-color: $accent; color: $accent; }
QDialog#infoDialog QPushButton:default { font-weight: normal; }  /* Only #primary is bold. */
QDialog#infoDialog QPushButton:pressed { background: $button_pressed; }
QDialog#infoDialog QPushButton#primary { background: $accent; color: $on_accent; border-color: $accent; font-weight: 600; }
QDialog#infoDialog QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; color: $on_accent; }
QDialog#infoDialog QPushButton#primary:pressed { background: $accent_pressed; }
QDialog#infoDialog QPushButton#danger { color: $invalid; border-color: $invalid; }
QDialog#infoDialog QPushButton#danger:hover { background: $invalid; color: $on_accent; }
QDialog#infoDialog QPushButton#linkButton { background: transparent; border: none; color: $accent; padding: 7px 4px; min-width: 0; }
QDialog#infoDialog QPushButton#linkButton:hover { color: $accent_hover; text-decoration: underline; }
QLineEdit#code { font-family: 'Cascadia Mono', 'Consolas'; font-size: 9pt; }
QLabel#kbd { background: $field; border: 1px solid $button_border; border-bottom-width: 2px; border-radius: 5px; padding: 1px 7px; font-size: 8pt; color: $secondary; }
QToolTip { background: $field; color: $text; border: 1px solid $field_border; padding: 5px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbar; border-radius: 3px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $scrollbar_hover; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
""")


def stylesheet(theme_colors: Colors) -> str:
    values = asdict(theme_colors)
    for asset in ("menu_check", "combo_arrow"):
        values[asset] = (ASSETS / values[asset]).as_posix()
    return _STYLESHEET.substitute(values, check_mark=CHECK_MARK)


def _palette(theme_colors: Colors) -> QPalette:
    """Qt's palette for what the stylesheet leaves to the style, such as popups."""
    palette = QPalette()
    roles = QPalette.ColorRole
    for role, color in (
        (roles.Window, theme_colors.page),
        (roles.WindowText, theme_colors.text),
        (roles.Base, theme_colors.field),
        (roles.AlternateBase, theme_colors.surface),
        (roles.Text, theme_colors.text),
        (roles.Button, theme_colors.field),
        (roles.ButtonText, theme_colors.text),
        (roles.Highlight, theme_colors.accent),
        (roles.HighlightedText, theme_colors.on_accent),
        (roles.ToolTipBase, theme_colors.field),
        (roles.ToolTipText, theme_colors.text),
        (roles.PlaceholderText, theme_colors.hint),
        (roles.Link, theme_colors.accent),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, roles.Text, QColor(theme_colors.hint))
    palette.setColor(QPalette.ColorGroup.Disabled, roles.ButtonText, QColor(theme_colors.hint))
    return palette


_SCHEMES = {
    Appearance.SYSTEM: Qt.ColorScheme.Unknown,  # Follows Windows.
    Appearance.LIGHT: Qt.ColorScheme.Light,
    Appearance.DARK: Qt.ColorScheme.Dark,
}


class Theme(QObject):
    """Applies the chosen appearance and follows Windows while that is "system".

    Asking Qt for the color scheme also darkens the title bars Windows draws.
    Anything that keeps colors beyond one paint, such as tinted icons, refreshes
    on `changed`.
    """

    changed = Signal()

    def __init__(self, app: QGuiApplication):
        super().__init__(app)
        self.app = app
        self.appearance = Appearance.SYSTEM
        self.applied: Colors | None = None
        app.styleHints().colorSchemeChanged.connect(self._refresh)

    def use(self, appearance: Appearance) -> None:
        self.appearance = appearance
        self.app.styleHints().setColorScheme(_SCHEMES[appearance])
        self._refresh()

    def _refresh(self) -> None:
        global _active
        if self.appearance == Appearance.SYSTEM:
            dark = self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        else:  # Whether or not the platform honors the requested scheme.
            dark = self.appearance == Appearance.DARK
        theme_colors = DARK if dark else LIGHT
        if theme_colors is self.applied:
            return
        _active = self.applied = theme_colors
        self.app.setPalette(_palette(theme_colors))
        self.app.setStyleSheet(stylesheet(theme_colors))
        self.changed.emit()


_theme: Theme | None = None


def theme() -> Theme:
    """The application's theme, created for the running QApplication on first use."""
    global _theme
    if _theme is None:
        _theme = Theme(QGuiApplication.instance())
    return _theme


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
    """WinLauncher's icon: a neon folder outline with a magnifier, on a 64-unit grid."""
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
