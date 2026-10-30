"""Launcher presentation: one compact search bar that grows with its results.

Widgets emit intents and never access the catalog.
"""

from __future__ import annotations

import itertools
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from html import escape
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from winlauncher.calculator import Calculation, calculate
from winlauncher.catalog.model import ItemKind
from winlauncher.commands import MenuCommand
from winlauncher.platform.gesture import ViewKind
from winlauncher.plugins.api import Chip, ChipTone
from winlauncher.plugins.search import PluginHit
from winlauncher.search.query import SearchQuery
from winlauncher.search.query_parser import without_type_filter
from winlauncher.search.ranking import SearchHit
from winlauncher.ui.commands import command_icon
from winlauncher.ui.dialogs import blocking_modal, bring_to_front
from winlauncher.ui.file_transfer import file_mime_data, url_mime_data
from winlauncher.ui.highlight import matched_positions
from winlauncher.ui.icons import ResultIcons
from winlauncher.ui.menus import Menu
from winlauncher.ui.plugins import PluginIcons
from winlauncher.ui.query_syntax import QuerySyntax
from winlauncher.ui.result_rows import LauncherHit, ResultRow, result_row, shortened_breadcrumbs
from winlauncher.ui.theme import app_icon, colors, theme
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.widgets import line_icon
from winlauncher.websites import WebsiteTarget

COMPACT_WIDTH = 640
EXPANDED_WIDTH = 900
EXPLORER_WIDTH = 396
SETTINGS_ICON_SIZE = 20  # With its padding as tall as the logo button beside it.
PIN_TOOLTIP = "Keep open after opening results (Ctrl+P)"
EXPANDED_ROWS = 14
SHADOW_MARGIN = 14
SHADOW_OFFSET = 4
SURFACE_RADIUS = 10  # Matches QFrame#surface in the theme.
EXPLORER_BOTTOM_GAP = 56  # Keep Explorer's status bar visible below the bar.
EXPLORER_RIGHT_GAP = 12  # With the shadow margin, keeps the scrollbar beside the bar visible.


@dataclass(frozen=True, slots=True)
class RowLayout:
    """How result rows are drawn: two-line launcher rows or one-line Explorer rows."""

    height: int
    icon: int
    name_points: int
    two_lines: bool
    visible_rows: int


LAUNCHER_ROWS = RowLayout(height=52, icon=32, name_points=11, two_lines=True, visible_rows=9)
EXPLORER_ROWS = RowLayout(height=25, icon=16, name_points=8, two_lines=False, visible_rows=6)
NUMBERED_ROWS = 9  # Ctrl+1 … Ctrl+9 open these rows.
SEPARATOR_INSET = 12  # Horizontal inset of the line before subfolder matches.
SEPARATOR_GAP = 9  # Extra height above that row; the line runs through its middle.
MISSING_OPACITY = 0.45  # Icon and text of a result whose file no longer exists.
LOCATION_POINTS = 8  # Font size of the muted location and metadata text.
HINT_SPACING = 8  # Between the shortcut hint and the kind filter buttons.
CHIP_POINTS = 7.5  # A plugin item's chips, such as a ticket's status.
CHIP_PADDING = 7  # Left and right of a chip's text.
CHIP_SPACING = 4  # Between chips.
CHIP_GAP = 8  # Between the last chip and the location.
CHIP_TEXT_WIDTH = 140  # Longer chip texts are elided.
CHIP_ALPHA = 40  # Of 255: a light tint of the chip's color behind its text.
# Symbols the bar draws; Segoe UI lacks some, and Windows searches every installed
# font for a fallback the first time one is measured, which takes 100-300 ms.
BAR_SYMBOLS = "↵ ↑↓ ✕ ★ · … –"
BAR_FONT_POINTS = (8, 9, 10, 11, 18)  # Status and hints, rows and the query field.
FILTERS = (
    ("All", None),
    ("Apps", ItemKind.APP),
    ("Files", ItemKind.FILE),
    ("Folders", ItemKind.FOLDER),
)
NO_MATCHES = "No matches. Try a shorter name, another spelling or fewer filters."
logger = logging.getLogger(__name__)


class ElidedLabel(QLabel):
    """One right-aligned line that shortens its text with … instead of widening the window.

    It reports no width of its own, so it takes only the space the layout has left.
    """

    def __init__(self):
        super().__init__()
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override.
        self._full_text = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override.
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = self.fontMetrics()
        super().setText(
            metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, self.width())
        )


class ResultList(QListWidget):
    """The result list; dragging a row hands its file or URL to the drop target.

    Drags copy or link but never move, so dropping a result on the desktop or a
    folder of the same drive cannot relocate the user's file by accident.
    """

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)

    def mimeData(self, items) -> QMimeData:  # noqa: N802 - Qt override.
        return transfer_data(items[0].data(Qt.ItemDataRole.UserRole))

    def startDrag(self, _supported) -> None:  # noqa: N802 - Qt override.
        super().startDrag(Qt.DropAction.CopyAction | Qt.DropAction.LinkAction)


def transfer_data(row: ResultRow, *, cut: bool = False) -> QMimeData:
    """What copying or dragging `row` hands over: the file itself, or a website's URL."""
    if (hit := row.file_hit) is not None:
        return file_mime_data([hit.item.path], cut=cut)
    return url_mime_data(row.target)


class ResultDelegate(QStyledItemDelegate):
    """Paints one `ResultRow`: icon, highlighted name, location, shortcut and favorite star."""

    def __init__(self, parent: QListWidget, website_icons: WebsiteIcons, plugin_icons: PluginIcons):
        super().__init__(parent)
        self.icons = ResultIcons(self)
        self.website_icons = website_icons
        self.plugin_icons = plugin_icons
        for icons in (self.icons, website_icons, plugin_icons):
            icons.changed.connect(parent.viewport().update)
        self.terms: tuple[str, ...] = ()
        self.rows = LAUNCHER_ROWS

    def sizeHint(self, option, index) -> QSize:
        return QSize(400, self.row_height(index.data(Qt.ItemDataRole.UserRole)))

    def row_height(self, result: ResultRow | None) -> int:
        separated = result is not None and result.starts_subfolders
        return self.rows.height + (SEPARATOR_GAP if separated else 0)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        result: ResultRow | None = index.data(Qt.ItemDataRole.UserRole)
        if result is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row, size = option.rect, self.rows.icon
        if result.starts_subfolders:
            # The gap stays unhighlighted, so selection never hides the line.
            line = row.top() + SEPARATOR_GAP // 2
            painter.setPen(QColor(colors().results_divider))
            painter.drawLine(
                row.left() + SEPARATOR_INSET, line, row.right() - SEPARATOR_INSET, line
            )
            row = row.adjusted(0, SEPARATOR_GAP, 0, 0)
        self._paint_background(painter, row, option.state)
        if result.missing:
            painter.setOpacity(MISSING_OPACITY)
        icon = QRect(row.left() + 16, row.center().y() - size // 2, size, size)
        self._icon(result.hit).paint(painter, icon)
        shortcut_width = self._paint_shortcut(painter, row, index.row(), result.pinned)
        left = icon.right() + 14
        text = QRect(left, row.top(), row.right() - left - shortcut_width - 8, row.height())
        baseline = text.top() + 41 if self.rows.two_lines else text.center().y() + 6
        metadata = result.metadata
        metadata_width = (
            min(
                text.width(),
                QFontMetrics(QFont("Segoe UI", LOCATION_POINTS)).horizontalAdvance(metadata) + 12,
            )
            if metadata
            else 0
        )
        metadata_area = text.adjusted(max(0, text.width() - metadata_width + 12), 0, 0, 0)
        self._paint_folder(painter, metadata_area, metadata, baseline=baseline)
        location_area = text.adjusted(0, 0, -metadata_width, 0)
        if self.rows.two_lines:
            self._paint_name(painter, text, result.name, baseline=text.top() + 23)
        else:
            name_end = self._paint_name(painter, location_area, result.name, baseline=baseline)
            location_area = location_area.adjusted(name_end - text.left() + 12, 0, 0, 0)
        chips_width = self._paint_chips(painter, location_area, result.chips, baseline=baseline)
        location_area = location_area.adjusted(chips_width, 0, 0, 0)
        self._paint_location(painter, location_area, result, baseline=baseline)
        painter.restore()

    def _icon(self, hit: LauncherHit) -> QIcon:
        if isinstance(hit, WebsiteTarget):
            return self.website_icons.icon_for(hit.url)
        if isinstance(hit, PluginHit):
            return self.plugin_icons.icon_for(hit)
        return self.icons.icon_for(hit.item)

    @staticmethod
    def _paint_background(painter: QPainter, row: QRect, state: QStyle.StateFlag) -> None:
        color = _row_color(state)
        if color is not None:
            painter.fillRect(row, QColor(color))
        if state & QStyle.StateFlag.State_HasFocus:
            painter.fillRect(QRect(row.left(), row.top(), 3, row.height()), QColor(colors().accent))

    def _paint_name(self, painter: QPainter, area: QRect, name: str, *, baseline: int) -> int:
        """Draw the name with matched characters highlighted; return where it ends."""
        font = QFont("Segoe UI", self.rows.name_points)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        visible = metrics.elidedText(name, Qt.TextElideMode.ElideRight, area.width())
        highlighted = matched_positions(name, self.terms) if visible == name else set()
        x = area.left()
        for start, end, matched in _runs(visible, highlighted):
            painter.setPen(QColor(colors().accent if matched else colors().text))
            segment = visible[start:end]
            painter.drawText(x, baseline, segment)
            x += metrics.horizontalAdvance(segment)
        return x

    def _paint_chips(
        self, painter: QPainter, area: QRect, chips: tuple[Chip, ...], *, baseline: int
    ) -> int:
        """Draw the chips that fit at the left of `area`; return the width they take."""
        if not chips or area.width() <= 0:
            return 0
        font = QFont("Segoe UI")
        font.setPointSizeF(CHIP_POINTS if self.rows.two_lines else CHIP_POINTS - 0.5)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        padding = 2 if self.rows.two_lines else 1
        top = baseline - metrics.ascent() - padding
        height = metrics.height() + 2 * padding
        x = area.left()
        for chip in chips:
            text = metrics.elidedText(chip.text, Qt.TextElideMode.ElideRight, CHIP_TEXT_WIDTH)
            width = metrics.horizontalAdvance(text) + 2 * CHIP_PADDING
            if x + width > area.right():
                break
            color = QColor(_chip_color(chip.tone))
            tint = QColor(color)
            tint.setAlpha(CHIP_ALPHA)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(QRectF(x, top, width, height), height / 2, height / 2)
            painter.setPen(color)
            painter.drawText(x + CHIP_PADDING, baseline, text)
            x += width + CHIP_SPACING
        return x - area.left() - CHIP_SPACING + CHIP_GAP if x > area.left() else 0

    def _paint_location(
        self, painter: QPainter, area: QRect, result: ResultRow, *, baseline: int
    ) -> None:
        """Breadcrumbs drop whole middle parts first; any text then elides in the middle."""
        location = result.location
        if result.location_parts and area.width() > 0:
            metrics = QFontMetrics(QFont("Segoe UI", LOCATION_POINTS))
            location = shortened_breadcrumbs(
                result.location_parts,
                lambda text: metrics.horizontalAdvance(text) <= area.width(),
            )
        self._paint_folder(painter, area, location, baseline=baseline)

    @staticmethod
    def _paint_folder(painter: QPainter, area: QRect, folder: str, *, baseline: int) -> None:
        if not folder or area.width() <= 0:
            return
        font = QFont("Segoe UI", LOCATION_POINTS)
        painter.setFont(font)
        painter.setPen(QColor(colors().faint))
        text = QFontMetrics(font).elidedText(folder, Qt.TextElideMode.ElideMiddle, area.width())
        painter.drawText(area.left(), baseline, text)

    def _paint_shortcut(self, painter: QPainter, row: QRect, number: int, pinned: bool) -> int:
        """Draw `Ctrl+N` and the favorite star at the right; return the width used."""
        shortcut = f"Ctrl+{number + 1}" if number < NUMBERED_ROWS else ""
        label = f"★  {shortcut}".strip() if pinned else shortcut
        if not label:
            return 0
        font = QFont("Segoe UI", 10 if self.rows.two_lines else 8)
        painter.setFont(font)
        painter.setPen(QColor(colors().hint))
        width = QFontMetrics(font).horizontalAdvance(label) + 24
        area = QRect(row.right() - width - 4, row.top(), width, row.height())
        painter.drawText(area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, label)
        return width


def _chip_color(tone: ChipTone) -> str:
    theme_colors = colors()
    return {
        ChipTone.NEUTRAL: theme_colors.muted,
        ChipTone.INFO: theme_colors.accent_strong,
        ChipTone.SUCCESS: theme_colors.success,
        ChipTone.WARNING: theme_colors.warning,
        ChipTone.DANGER: theme_colors.invalid,
    }[tone]


def _row_color(state: QStyle.StateFlag) -> str | None:
    if state & QStyle.StateFlag.State_Selected:
        return colors().selected
    if state & QStyle.StateFlag.State_MouseOver:
        return colors().hover
    return None


def _runs(text: str, highlighted: set[int]) -> Iterator[tuple[int, int, bool]]:
    """Split `text` into `(start, end, highlighted)` runs of equal highlighting."""
    start = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or (index in highlighted) != (start in highlighted):
            yield start, index, start in highlighted
            start = index


def _shortcut_hint(
    row: ResultRow | None, *, explorer: bool, calculation: bool, dialog: bool = False
) -> str:
    """The most useful keys for what Enter would act on now."""
    if calculation:
        return "↵ copy result"
    if row is None:
        return ""
    if row.file_hit is None:
        return f"↵ open in {row.opens_in} · Ctrl+C copy {row.target_noun}"
    if dialog:
        if row.file_hit.item.kind == ItemKind.FOLDER:
            return "↵ open in the dialog"
        return "↵ enter as file name"
    if explorer:
        return "↵ open · ↑↓ select in Explorer"
    if row.file_hit.item.kind == ItemKind.FOLDER and not row.missing:
        return "↵ open · Tab search inside · Ctrl+↵ show in folder"
    return "↵ open · Ctrl+↵ show in folder · Ctrl+C copy file · Ctrl+1–9"


def _with_subfolder_separator(rows: list[ResultRow]) -> list[ResultRow]:
    """Mark the first subfolder match that follows direct entries of the folder."""
    for index, (before, row) in enumerate(itertools.pairwise(rows), start=1):
        if before.direct and not row.direct:
            rows[index] = replace(
                row,
                starts_subfolders=True,
                accessible_text=f"In subfolders: {row.accessible_text}",
            )
            break
    return rows


def _menu_commands(commands: tuple[MenuCommand, ...], kind: ItemKind) -> list[MenuCommand]:
    """The commands shown for a result of `kind`; applications have none."""
    if kind == ItemKind.FOLDER:
        return [command for command in commands if command.folders]
    if kind == ItemKind.FILE:
        return [command for command in commands if command.files]
    return []


def _shortcut_modifier(modifiers: Qt.KeyboardModifier, *, ctrl: bool = False) -> str | None:
    """The command shortcut `modifiers` stand for, such as "Alt"; None for others.

    `ctrl` says whether Ctrl must be held, as with Ctrl+Alt+1. Each bit is tested
    on its own: in PySide6, `m & ~ControlModifier` also clears Alt.
    """
    keys = Qt.KeyboardModifier
    if bool(modifiers & keys.ControlModifier) != ctrl or modifiers & keys.MetaModifier:
        return None
    alt, shift = bool(modifiers & keys.AltModifier), bool(modifiers & keys.ShiftModifier)
    return {(True, False): "Alt", (False, True): "Shift", (True, True): "Alt+Shift"}.get(
        (alt, shift)
    )


def _row_number(event: QKeyEvent) -> int | None:
    """The digit 1–9 pressed, read from the virtual key where the platform gives one.

    With Shift or AltGr (Ctrl+Alt) the layout turns digits into other characters,
    such as `!` or `²`, but the virtual key stays VK_1 … VK_9.
    """
    virtual_key = event.nativeVirtualKey()
    if ord("1") <= virtual_key <= ord("9"):
        return virtual_key - ord("0")
    if Qt.Key.Key_1.value <= event.key() <= Qt.Key.Key_9.value:
        return event.key() - Qt.Key.Key_0.value
    return None


def _paint_soft_shadow(painter: QPainter, surface: QRectF) -> None:
    """Stack faint rounded rectangles that darken towards the surface.

    A QGraphicsDropShadowEffect would look the same, but it renders the surface
    through a cache that misses partial repaints and leaves stale glyphs behind
    in the search field while typing.
    """
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 5))
    shadow = surface.translated(0, SHADOW_OFFSET)
    for spread in range(SHADOW_MARGIN - SHADOW_OFFSET, 0, -1):
        radius = SURFACE_RADIUS + spread
        painter.drawRoundedRect(shadow.adjusted(-spread, -spread, spread, spread), radius, radius)


def _logical_rect(native: tuple[int, int, int, int]) -> QRect | None:
    """Convert a rectangle in physical pixels to Qt's coordinates on its screen.

    Qt keeps each screen's origin and scales positions within it by the
    screen's device pixel ratio.
    """
    left, top, right, bottom = native
    for screen in QGuiApplication.screens():
        origin = screen.geometry().topLeft()
        ratio = screen.devicePixelRatio()
        native_size = screen.geometry().size() * ratio
        if QRect(origin, native_size).contains(left + 1, top + 1):
            x = origin.x() + round((left - origin.x()) / ratio)
            y = origin.y() + round((top - origin.y()) / ratio)
            return QRect(x, y, round((right - left) / ratio), round((bottom - top) / ratio))
    return None


class CalculationRow(QFrame):
    """The calculator's result above the search results; a click copies it."""

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("calculation")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(22, 8, 16, 8)
        row.setSpacing(12)
        self.result = QLabel()
        self.result.setObjectName("calculationResult")
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.hint = QLabel()
        self.hint.setObjectName("calculationHint")
        row.addWidget(self.result, 1)
        row.addWidget(self.hint)
        self.setAccessibleName("Calculation result")
        self.hide()

    def show_result(self, calculation: Calculation, selected: bool) -> None:
        metrics = self.result.fontMetrics()
        text = f"= {calculation.result}"
        self.result.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, 480))
        self.result.setToolTip(f"{calculation.expression} = {calculation.result}")
        self.hint.setText("Enter copies" if selected else "")
        self.setAccessibleDescription(text)
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        event.accept()


class LauncherWindow(QWidget):
    query_changed = Signal()
    open_requested = Signal(object)
    reveal_requested = Signal(object)
    favorite_requested = Signal(object)
    # SearchHit the user moved to in the Explorer bar, for Explorer to select.
    explorer_selection = Signal(object)
    # Text typed in a file dialog's bar that Escape returns to the dialog.
    dialog_text_returned = Signal(str)
    # SearchHit and whether to add the extended (Shift) entries: Windows' own menu.
    shell_menu_requested = Signal(object, bool)
    exclude_requested = Signal(object)
    # SearchHit and the MenuCommand to run for it.
    command_requested = Signal(object, object)
    settings_requested = Signal()
    rescan_requested = Signal()
    restart_requested = Signal()
    about_requested = Signal()
    hidden = Signal()
    shown = Signal()

    def __init__(self, website_icons: WebsiteIcons, plugin_icons: PluginIcons | None = None):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        super().__init__(None, flags | Qt.WindowType.WindowStaysOnTopHint)
        self.scope: Path | None = None
        self.kind: ItemKind | None = None
        self.expanded = False
        self.catalog_count = 0
        self.explorer_mode = False  # Compact bar next to the Explorer window.
        self.explorer_view: tuple[int, int] | None = None
        # Whether `explorer_view` is an Explorer tab or an Open/Save dialog (Quick Switch).
        self.view_kind = ViewKind.EXPLORER
        self.website_icons = website_icons
        self.plugin_icons = plugin_icons or PluginIcons(parent=self)
        # Result locations are relative to this folder in an Explorer search.
        self.result_scope: Path | None = None
        self.calculation: Calculation | None = None
        # The calculation, not a result, receives Enter while it is selected.
        self.calculation_selected = False
        self._bottom_anchor: int | None = None
        self._resize_pending = False
        self._refreshing_results = False
        # Started when focus leaves the launcher; see `set_auto_hide` and `_hide_if_unused`.
        self.auto_hide = QTimer(self)
        self.auto_hide.setSingleShot(True)
        self.auto_hide.timeout.connect(self._hide_if_unused)
        self.auto_hide_enabled = False
        self.auto_hide_ms = 0
        # Pinned, results open without closing the bar; see `set_pinned`.
        self.pinned = False
        self.menu_commands: tuple[MenuCommand, ...] = ()
        self._command_icons: dict[MenuCommand, QIcon] = {}  # Loaded when first shown.
        self.foreground_watcher = None
        if self._native_focus():
            from winlauncher.platform.windows import ForegroundWatcher

            self.foreground_watcher = ForegroundWatcher(self._focus_left)
        self.setObjectName("launcher")
        self.setWindowTitle("WinLauncher")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        # The window sizes itself to its content in `_fit_height`.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        surface = self.surface = QFrame()
        surface.setObjectName("surface")
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(0)
        layout.addLayout(self._build_search_bar())
        self.divider = QFrame()
        self.divider.setObjectName("divider")
        self.divider.setFixedHeight(1)
        layout.addWidget(self.divider)
        self.calculation_row = CalculationRow()
        self.calculation_row.clicked.connect(self.copy_calculation)
        layout.addWidget(self.calculation_row)
        self.empty = self._label("message")
        self.empty.linkActivated.connect(self.search_contents)
        self.content_search: str | None = None  # Offered while no name matches.
        layout.addWidget(self.empty)
        layout.addWidget(self._build_results())
        self.detail = self._label("detail")
        self.status = self._label("status")
        self.status.setWordWrap(False)
        self.hint = ElidedLabel()
        self.hint.setObjectName("hint")
        self.hint.setAccessibleName("Keyboard shortcuts")
        self.hint.hide()
        self.activity = self._label("activity")
        layout.addWidget(self.detail)
        layout.addWidget(self._build_footer())
        layout.addWidget(self.activity)
        self._install_shortcuts()
        self._show_scope(None)
        self.resize(COMPACT_WIDTH + 2 * SHADOW_MARGIN, 0)
        self._show_results_area(False)

    # Construction -------------------------------------------------------------

    def _build_search_bar(self) -> QHBoxLayout:
        row = self.search_bar = QHBoxLayout()
        row.setContentsMargins(22, 12, 12, 10)
        row.setSpacing(8)
        self.query = QLineEdit()
        self.query.setObjectName("query")
        self.query.setPlaceholderText("Search apps, files, %TEMP% or ~/")
        self.query.setAccessibleName("Search query")
        self.query.textChanged.connect(self.query_changed)
        self.query.textChanged.connect(self._update_calculation)
        self.query.installEventFilter(self)
        self.query_syntax = QuerySyntax(self.query)
        row.addWidget(self.query, 1)
        self.clear_scope = QToolButton()
        self.clear_scope.setObjectName("scope")
        self.clear_scope.setAccessibleName("Search everywhere")
        self.clear_scope.clicked.connect(lambda: self.set_scope(None))
        row.addWidget(self.clear_scope)
        self.menu_button = QToolButton()
        self.menu_button.setObjectName("menuButton")
        self.menu_button.setIcon(app_icon())
        self.menu_button.setIconSize(QSize(28, 28))
        self.menu_button.setToolTip("WinLauncher menu")
        self.menu_button.setAccessibleName("WinLauncher menu")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setMenu(self._build_menu())
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setIconSize(QSize(SETTINGS_ICON_SIZE, SETTINGS_ICON_SIZE))
        self.settings_button.setToolTip("Settings (Ctrl+,)")
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.clicked.connect(self.settings_requested)
        self.pin_button = QToolButton()
        self.pin_button.setObjectName("pinButton")
        self.pin_button.setCheckable(True)
        self.pin_button.setIconSize(QSize(SETTINGS_ICON_SIZE, SETTINGS_ICON_SIZE))
        self.pin_button.setToolTip(PIN_TOOLTIP)
        self.pin_button.setAccessibleName("Keep open")
        self.pin_button.toggled.connect(self.set_pinned)
        self._use_theme_icons()
        theme().changed.connect(self._theme_changed)
        row.addWidget(self.settings_button)
        row.addWidget(self.pin_button)
        row.addWidget(self.menu_button)  # The logo stays the bar's right edge.
        return row

    def _use_theme_icons(self) -> None:
        self.settings_button.setIcon(line_icon("gear", colors().muted))
        pin_color = colors().scope_text if self.pinned else colors().muted
        self.pin_button.setIcon(line_icon("pin", pin_color))

    def _theme_changed(self) -> None:
        """Results paint with the active colors; icons keep the colors they were made with."""
        self._use_theme_icons()
        self.results.viewport().update()

    def _build_menu(self) -> QMenu:
        menu = Menu(self)
        group = QActionGroup(self)
        self.kind_actions: list[QAction] = []
        for label, kind in FILTERS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(kind is None)
            action.triggered.connect(lambda _checked=False, kind=kind: self.set_kind(kind))
            group.addAction(action)
            self.kind_actions.append(action)
        menu.addSeparator()
        menu.addAction("Refresh catalog").triggered.connect(self.rescan_requested)
        self.expand_action = menu.addAction("Expanded view")
        self.expand_action.setCheckable(True)
        self.expand_action.triggered.connect(self.toggle_expanded)
        menu.addAction("Settings…").triggered.connect(self.settings_requested)
        menu.addAction("About WinLauncher").triggered.connect(self.about_requested)
        menu.addSeparator()
        menu.addAction("Restart WinLauncher").triggered.connect(self.restart_requested)
        menu.addAction("Hide", self.hide)
        return menu

    def _build_results(self) -> QListWidget:
        self.results = ResultList()
        self.results.setObjectName("results")
        self.results.setAccessibleName("Search results")
        self.delegate = ResultDelegate(self.results, self.website_icons, self.plugin_icons)
        self.results.setItemDelegate(self.delegate)
        self.results.setMouseTracking(True)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self.context_menu)
        self.results.itemDoubleClicked.connect(self._double_clicked)
        self.results.itemClicked.connect(self._clicked)
        self.results.currentItemChanged.connect(self.update_detail)
        self.results.currentItemChanged.connect(self._update_hint)
        self.results.currentItemChanged.connect(self._selection_moved)
        self.results.installEventFilter(self)
        return self.results

    def _build_footer(self) -> QFrame:
        self.footer = QFrame()
        row = QHBoxLayout(self.footer)
        row.setContentsMargins(22, 2, 14, 2)
        row.setSpacing(4)
        row.addWidget(self.status)
        row.addWidget(self.hint, 1)  # Takes the space left over, right-aligned.
        row.addSpacing(HINT_SPACING)
        self.kind_buttons: dict[ItemKind, QToolButton] = {}
        for number, (kind, label, icon) in enumerate(
            (
                (ItemKind.FILE, "Files", QStyle.StandardPixmap.SP_FileIcon),
                (ItemKind.FOLDER, "Folders", QStyle.StandardPixmap.SP_DirIcon),
                (ItemKind.APP, "Apps", QStyle.StandardPixmap.SP_ComputerIcon),
            ),
            1,
        ):
            button = QToolButton()
            button.setObjectName("kindFilter")
            button.setIcon(self.style().standardIcon(icon))
            button.setCheckable(True)
            button.setAccessibleName(f"{label} only")
            button.setToolTip(f"{label} only (Alt+{number}); click again for All")
            button.clicked.connect(
                lambda checked, kind=kind: self.set_kind(kind if checked else None)
            )
            row.addWidget(button)
            self.kind_buttons[kind] = button
            QShortcut(QKeySequence(f"Alt+{number}"), self).activated.connect(button.click)
        QShortcut(QKeySequence("Alt+0"), self).activated.connect(lambda: self.set_kind(None))
        return self.footer

    @staticmethod
    def _label(name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(name)
        label.setWordWrap(True)
        label.hide()
        return label

    def _install_shortcuts(self) -> None:
        for sequence, action in (
            ("Escape", self.dismiss),
            ("Ctrl+L", self.focus_query),
            ("Ctrl+Return", self.reveal_selected),
            ("Ctrl+Enter", self.reveal_selected),  # Keypad Enter.
            ("Ctrl+Shift+C", self.copy_target),
            ("Ctrl+D", self.favorite_selected),
            ("Ctrl+P", self.pin_button.toggle),
            ("Ctrl+,", self.settings_requested),
            ("Shift+F10", self.keyboard_context_menu),
        ):
            QShortcut(QKeySequence(sequence), self).activated.connect(action)
        for row in range(NUMBERED_ROWS):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{row + 1}"), self)
            shortcut.activated.connect(lambda row=row: self.open_row(row))

    def paintEvent(self, event) -> None:
        _paint_soft_shadow(QPainter(self), QRectF(self.surface.geometry()))

    # Input --------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        """The frameless bar moves when dragged anywhere outside its controls."""
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and self._handle_key(watched, event):
            return True
        return super().eventFilter(watched, event)

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.WindowDeactivate:
            self._focus_left()
        elif event.type() == QEvent.Type.WindowActivate:
            self.auto_hide.stop()
        if (
            event.type() in (QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate)
            and self._native_focus()
        ):
            from winlauncher.platform.windows import foreground_details

            logger.info("Launcher %s; foreground=%s", event.type().name, foreground_details())
        return handled

    @staticmethod
    def _native_focus() -> bool:
        return os.name == "nt" and QApplication.platformName() == "windows"

    def _handle_key(self, watched: QObject, event: QKeyEvent) -> bool:
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if self._command_key(event):
            return True
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not modifiers:
            if self.calculation_selected:
                self.copy_calculation()
            elif self.content_search is not None and not self.results.count():
                self.search_contents()
            else:
                self.open_selected()
            return True
        # Without selected query text, copying and cutting act on the selected result.
        typed_selection = watched is self.query and self.query.hasSelectedText()
        if not typed_selection and not self.calculation_selected:
            if event.matches(QKeySequence.StandardKey.Copy):
                return self.copy_selected()
            if event.matches(QKeySequence.StandardKey.Cut):
                return self.copy_selected(cut=True)
        if watched is self.query and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._move_selection(1 if event.key() == Qt.Key.Key_Down else -1)
            return True
        return watched is self.query and self._folder_search_key(event)

    def _folder_search_key(self, event: QKeyEvent) -> bool:
        """Tab searches inside the selected folder, Shift+Tab inside the chip's parent
        and Backspace in the empty field everywhere again.

        Only the launcher bar does this; the Explorer bar's folder is the Explorer
        window's. Without a folder to act on, the keys keep their usual meaning.
        """
        if self.explorer_mode:
            return False
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key == Qt.Key.Key_Tab and modifiers == Qt.KeyboardModifier.NoModifier:
            return self._search_inside(self._selected_folder())
        if self.scope is None:
            return False
        if key == Qt.Key.Key_Backtab:
            return self._search_inside(self.scope.parent)
        if key == Qt.Key.Key_Backspace and not self.query.text():
            self.set_scope(None)
            return True
        return False

    def _selected_folder(self) -> Path | None:
        row = self.selected_row()
        hit = row.file_hit if row is not None else None
        if hit is None or row.missing or hit.item.kind != ItemKind.FOLDER:
            return None
        return hit.item.path

    def _search_inside(self, folder: Path | None) -> bool:
        """Make `folder` the scope chip and start over with an empty query."""
        if folder is None:
            return False
        self._replace_query("")
        self.set_scope(folder)
        return True

    def _command_key(self, event: QKeyEvent) -> bool:
        """Modifier+Enter or Ctrl+Modifier+1–9: run the command with that shortcut."""
        if self.calculation_selected:
            return False
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifier = _shortcut_modifier(event.modifiers())
            return modifier is not None and self.run_shortcut(self.selected_row(), modifier)
        number = _row_number(event)
        modifier = _shortcut_modifier(event.modifiers(), ctrl=True)
        if number is None or modifier is None or (item := self.results.item(number - 1)) is None:
            return False
        self.results.setCurrentItem(item)
        return self.run_shortcut(item.data(Qt.ItemDataRole.UserRole), modifier)

    def _clicked(self, item: QListWidgetItem) -> None:
        """Modifier+click runs that command; the Explorer bar opens on a single click."""
        modifier = _shortcut_modifier(QApplication.keyboardModifiers())
        if modifier is not None and self.run_shortcut(
            item.data(Qt.ItemDataRole.UserRole), modifier
        ):
            return
        if self.explorer_mode:
            self.open_selected()

    def _double_clicked(self, _item: QListWidgetItem) -> None:
        modifier = _shortcut_modifier(QApplication.keyboardModifiers())
        if self.explorer_mode or (modifier is not None and self._command_uses(modifier)):
            return  # The first click acted already.
        self.open_selected()

    def _command_uses(self, modifier: str) -> bool:
        return any(command.shortcut == modifier for command in self.menu_commands)

    def run_shortcut(self, row: ResultRow | None, modifier: str) -> bool:
        """Run the command whose shortcut is `modifier` on `row`.

        Returns False when no command uses `modifier`, so the keys act as before.
        """
        if not self._command_uses(modifier):
            return False
        hit = row.file_hit if row is not None else None
        commands = _menu_commands(self.menu_commands, hit.item.kind) if hit else []
        command = next((c for c in commands if c.shortcut == modifier), None)
        if command is None:
            if row is not None:
                self._show_status(f"No command uses {modifier} for {row.name}")
            return True
        self.command_requested.emit(hit, command)
        return True

    def _move_selection(self, step: int) -> None:
        """The calculation, when shown, sits above the first result."""
        if self.calculation is not None:
            if self.calculation_selected and step > 0 and self.results.count():
                self._select_calculation(False)
                self.results.setCurrentRow(0)
                return
            if not self.calculation_selected and step < 0 and self.results.currentRow() <= 0:
                self._select_calculation(True)
                return
            if self.calculation_selected:
                return
        last = self.results.count() - 1
        self.results.setCurrentRow(max(0, min(last, self.results.currentRow() + step)))

    # Calculator ---------------------------------------------------------------

    def _update_calculation(self, *_changed) -> None:
        """Show the result of a typed calculation; the search runs regardless."""
        previous = self.calculation
        self.calculation = None if self.explorer_mode else calculate(self.query.text())
        if self.calculation is None:
            self.calculation_row.hide()
            if self.calculation_selected:
                self._select_calculation(False)
                if self.results.count():
                    self.results.setCurrentRow(0)
        else:
            self._select_calculation(previous is None or self.calculation_selected)
        if not self.calculation_row.isHidden() or previous is not None:
            self.empty.setVisible(self.empty.isVisible() and self.calculation is None)
            self._fit_height()

    def _select_calculation(self, selected: bool) -> None:
        self.calculation_selected = selected and self.calculation is not None
        self._update_hint()
        if self.calculation_selected:
            self.results.setCurrentRow(-1)
        if self.calculation is not None:
            self.calculation_row.show_result(self.calculation, self.calculation_selected)

    def copy_calculation(self) -> None:
        if self.calculation is None:
            return
        QApplication.clipboard().setText(self.calculation.result)
        self._select_calculation(True)
        self.status.setText("Result copied to clipboard")
        self.status.show()
        self._fit_height()

    def focus_query(self) -> None:
        self.query.setFocus()
        self.query.selectAll()

    # Result state -------------------------------------------------------------

    def selected_row(self) -> ResultRow | None:
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_hit(self) -> LauncherHit | None:
        row = self.selected_row()
        return row.hit if row else None

    def set_catalog_count(self, count: int) -> None:
        self.catalog_count = count

    def set_results(
        self,
        hits: list[LauncherHit],
        elapsed_ms: float,
        *,
        preserve_selection: bool = True,
        query: SearchQuery | None = None,
        missing: frozenset[Path] = frozenset(),
        content_search: str | None = None,
    ) -> None:
        """Show new results; keep the selected target only when the query is unchanged.

        `query` is the file search behind the catalog hits; it drives the name
        highlighting and the size and date details. `missing` files are shown
        dimmed. Without hits, `content_search` is offered as the next query.
        """
        previous = self.selected_row() if preserve_selection else None
        self.delegate.terms = query.terms if query is not None else ()
        rows = _with_subfolder_separator(
            [
                result_row(
                    hit,
                    query,
                    self.result_scope,
                    missing=isinstance(hit, SearchHit) and hit.item.path in missing,
                )
                for hit in hits
            ]
        )
        selected_index = 0
        with self._replacing_results():
            self.results.clear()
            for index, row in enumerate(rows):
                item = QListWidgetItem(row.accessible_text)
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setToolTip(row.tooltip)
                self.results.addItem(item)
                if previous is not None and row.target == previous.target:
                    selected_index = index
            if hits and not self.calculation_selected:
                self.results.setCurrentRow(selected_index)
        self._follow_in_explorer(direct_only=True)
        # An empty query without history stays a bare search bar.
        self.content_search = None if hits else content_search
        if self.content_search is None:
            self.empty.setTextFormat(Qt.TextFormat.PlainText)
            self.empty.setText(NO_MATCHES)
        else:
            self.empty.setTextFormat(Qt.TextFormat.RichText)
            self.empty.setText(
                f'No matching names. <a href="contents">Search inside files</a> (Enter):'
                f" {escape(self.content_search)}"
            )
        self.empty.setVisible(
            not hits and bool(self.query.text().strip()) and self.calculation is None
        )
        status = f"{len(hits)} results  ·  {elapsed_ms:.0f} ms"
        if not self.explorer_mode:
            status += f"  ·  {self.catalog_count:,} indexed"
        self.status.setText(status if hits else "")
        self.status.setVisible(bool(hits))
        self._show_results_area(bool(hits))

    def show_searching(self) -> None:
        """Keep shown results until new ones replace them; clearing them per key flickers.

        Enter still opens a result of the typed query: the search session waits for it.
        """
        if self.results.count() == 0:
            self._show_message("Searching…")

    def show_query_error(self, message: str) -> None:
        self._show_message(message)

    def show_search_error(self, message: str) -> None:
        self._show_message(f"The search could not be completed: {message}")

    def set_activity(self, text: str) -> None:
        """Indexing activity and persistent problems, shown only while there are any."""
        self.activity.setText(text)
        self.activity.setVisible(bool(text))
        self._fit_height()

    def _show_message(self, message: str) -> None:
        with self._replacing_results():
            self.results.clear()
        self.content_search = None
        self.empty.setTextFormat(Qt.TextFormat.PlainText)
        self.empty.setText(message)
        self.empty.show()
        self.status.hide()
        self._show_results_area(False)

    def _show_results_area(self, has_results: bool) -> None:
        self.results.setVisible(has_results)
        self._update_hint()  # Reads the list's visibility.
        self.detail.setVisible(has_results and self.expanded and not self.explorer_mode)
        self._fit_height()

    def _fit_height(self) -> None:
        """Grow or shrink with the content, like a single search bar."""
        layout = self.delegate.rows
        limit = EXPANDED_ROWS if self.expanded and not self.explorer_mode else layout.visible_rows
        rows = min(self.results.count(), limit)
        content = sum(
            self.delegate.row_height(self.results.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(rows)
        )
        self.results.setFixedHeight(content + 2 * self.results.frameWidth())
        below_bar = (
            self.calculation_row,
            self.empty,
            self.results,
            self.detail,
            self.footer,
            self.activity,
        )
        self.divider.setVisible(any(not widget.isHidden() for widget in below_bar))
        if not self._resize_pending:
            # Labels are measured correctly only after Qt has polished them.
            self._resize_pending = True
            QTimer.singleShot(0, self._resize_to_content)

    def _resize_to_content(self) -> None:
        self._resize_pending = False
        height = self.layout().totalHeightForWidth(self.width())
        self.resize(self.width(), height if height > 0 else self.sizeHint().height())
        if self._bottom_anchor is not None:
            # The Explorer bar grows upwards from its fixed bottom edge.
            self.move(self.x(), self._bottom_anchor - self.height())

    # Filters and scope ----------------------------------------------------------

    def _replace_query(self, text: str) -> None:
        """Set the text without starting a search; the caller decides when to search."""
        self.query.blockSignals(True)
        self.query.setText(text)
        self.query.blockSignals(False)
        self.query_syntax.refresh()

    def set_kind(self, kind: ItemKind | None) -> None:
        self.kind = kind
        try:
            text = without_type_filter(self.query.text())
        except ValueError:
            text = self.query.text()  # Keep an unfinished quoted query editable.
        self._replace_query(text)
        self.show_kind(kind)
        self.query_changed.emit()
        self.query.setFocus()

    def show_kind(self, kind: ItemKind | None) -> None:
        """Reflect the effective query kind without starting another search."""
        for action, (_, action_kind) in zip(self.kind_actions, FILTERS, strict=True):
            action.setChecked(action_kind == kind)
        for button_kind, button in self.kind_buttons.items():
            button.setChecked(button_kind == kind)

    def set_scope(self, folder: Path | None) -> None:
        if folder is None and self.explorer_mode:
            self.explorer_view = None
            self.view_kind = ViewKind.EXPLORER
            self._set_explorer_mode(False, None)
            self._place_on_cursor_screen()
        self._show_scope(folder)
        if not self.explorer_mode:
            self.result_scope = folder
        self.query_changed.emit()
        self.query.setFocus()

    def _show_scope(self, folder: Path | None) -> None:
        self.scope = folder
        if folder is None:
            self.clear_scope.hide()
            return
        name = self.clear_scope.fontMetrics().elidedText(
            folder.name or str(folder),
            Qt.TextElideMode.ElideMiddle,
            110 if self.explorer_mode else 220,
        )
        self.clear_scope.setText(f"in {name}   ✕")
        keys = "" if self.explorer_mode else "\nShift+Tab: parent folder · Backspace: everywhere"
        self.clear_scope.setToolTip(f"Searching in {folder}. Click to search everywhere.{keys}")
        self.clear_scope.show()

    # Window placement -----------------------------------------------------------

    def present(
        self,
        *,
        text: str = "",
        scope: Path | None = None,
        near: tuple[int, int, int, int] | None = None,
        explorer_view: tuple[int, int] | None = None,
        view_kind: ViewKind = ViewKind.EXPLORER,
    ) -> None:
        """Show with `text` typed below `scope`.

        With `near` (an Explorer window or file dialog in physical pixels) the
        compact Explorer bar opens at the bottom of that window; otherwise the
        launcher opens on the cursor's screen. `view_kind` says what
        `explorer_view` belongs to.
        """
        if (modal := blocking_modal(self)) is not None:
            self.hide()
            bring_to_front(modal)
            return
        self._show_scope(scope)
        self.explorer_view = explorer_view
        self.view_kind = view_kind
        if explorer_view is not None or near is not None:
            self.kind = None  # Explorer completion always searches the whole folder.
            self.show_kind(None)
        self._replace_query(text)
        self._set_explorer_mode(near is not None or explorer_view is not None, scope)
        self._update_calculation()
        anchor = _logical_rect(near) if near is not None else None
        if anchor is not None:
            self._place_in(anchor)
        else:
            self._place_on_cursor_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        if self._native_focus():
            from winlauncher.platform.windows import activate_window

            active = activate_window(int(self.winId()))
            logger.info(
                "Launcher presented: foreground_granted=%s scoped=%s", active, scope is not None
            )
            QTimer.singleShot(100, self._check_activation)
        self.query.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.query_changed.emit()

    def warm_up_text(self) -> None:
        """Load fonts and symbol fallbacks once while idle, not when the bar first opens."""
        for points in BAR_FONT_POINTS:
            QFontMetrics(QFont("Segoe UI", points)).horizontalAdvance(BAR_SYMBOLS)
        for label in (self.hint, self.status):
            label.ensurePolished()  # Their font comes from the style sheet.
            label.fontMetrics().horizontalAdvance(BAR_SYMBOLS)

    def set_auto_hide(self, enabled: bool, seconds: int) -> None:
        """Hide `seconds` after focus left WinLauncher, unless something was typed.

        Independent of this setting, the Explorer bar hides as soon as focus leaves
        WinLauncher, typed or not: it serves only the search started in Explorer.
        """
        self.auto_hide_enabled = enabled
        self.auto_hide_ms = seconds * 1000
        if not enabled:
            self.auto_hide.stop()

    def set_menu_commands(self, commands: tuple[MenuCommand, ...]) -> None:
        """The user's own entries in the context menu of folder and file results."""
        self.menu_commands = commands
        self._command_icons.clear()

    def _command_icon(self, command: MenuCommand) -> QIcon:
        if (icon := self._command_icons.get(command)) is None:
            icon = self._command_icons[command] = command_icon(command)
        return icon

    def close_focus_watch(self) -> None:
        if self.foreground_watcher is not None:
            self.foreground_watcher.close()
            self.foreground_watcher = None

    def _focus_left(self) -> None:
        """Qt or Windows reports that focus moved away; start closing as configured."""
        if not self.isVisible():
            return
        if self.explorer_mode:
            # On the next event loop pass, once the new foreground window is known.
            self.auto_hide.start(0)
        elif self.auto_hide_enabled and not self.pinned:
            self.auto_hide.start(self.auto_hide_ms)

    def _hide_if_unused(self) -> None:
        unused = self.explorer_mode or not self.query.text()
        if unused and not self._winlauncher_in_front():
            self.hide()

    def _winlauncher_in_front(self) -> bool:
        """Whether the launcher or one of WinLauncher's menus or dialogs is in front.

        On Windows the native answer counts: Qt may miss activation changes of a
        window that `activate_window` forced to the front.
        """
        if self._native_focus():
            from winlauncher.platform.windows import foreground_is_own_process

            return foreground_is_own_process()
        return QApplication.activeWindow() is not None

    def _check_activation(self) -> None:
        if self.isVisible() and self._native_focus():
            from winlauncher.platform.windows import foreground_details, user32

            active = user32.GetForegroundWindow() == int(self.winId())
            log = logger.info if active and self.query.hasFocus() else logger.warning
            log(
                "Launcher focus check: foreground=%s query_focus=%s owner=%s",
                active,
                self.query.hasFocus(),
                foreground_details(),
            )

    def _bar_width(self) -> int:
        if self.explorer_mode:
            return EXPLORER_WIDTH + 2 * SHADOW_MARGIN
        return (EXPANDED_WIDTH if self.expanded else COMPACT_WIDTH) + 2 * SHADOW_MARGIN

    def _set_explorer_mode(self, enabled: bool, scope: Path | None) -> None:
        self.explorer_mode = enabled
        self.delegate.rows = EXPLORER_ROWS if enabled else LAUNCHER_ROWS
        self.result_scope = scope if enabled else None
        self.results.doItemsLayout()
        for widget in (
            self.query,
            self.clear_scope,
            self.menu_button,
            self.empty,
            *self.kind_buttons.values(),
        ):
            widget.setProperty("explorer", enabled)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        if enabled:
            self.search_bar.setContentsMargins(10, 4, 6, 4)
            self.footer.layout().setContentsMargins(10, 0, 8, 0)
        else:
            self.search_bar.setContentsMargins(22, 12, 12, 10)
            self.footer.layout().setContentsMargins(22, 2, 14, 2)
        for button in self.kind_buttons.values():
            button.setIconSize(QSize(14, 14) if enabled else QSize(16, 16))
        self.menu_button.setIconSize(QSize(16, 16) if enabled else QSize(28, 28))
        # The Explorer bar stays narrow; its menu still offers the settings.
        self.settings_button.setVisible(not enabled)
        self.pin_button.setVisible(not enabled)
        self._show_scope(scope)

    def _place_on_cursor_screen(self) -> None:
        self._bottom_anchor = None
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(self._bar_width(), area.width()), self.height())
        self.move(area.x() + (area.width() - self.width()) // 2, area.y() + area.height() // 5)

    def _place_in(self, explorer: QRect) -> None:
        """Docked to the bottom right of the Explorer window, inside its screen.

        There it covers the date and type columns rather than the names.
        """
        screen = QApplication.screenAt(explorer.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(self._bar_width(), area.width()), self.height())
        right = min(explorer.right() - EXPLORER_RIGHT_GAP, area.right())
        self.move(max(area.left(), right - self.width() + 1), self.y())  # right() is inclusive.
        bottom = min(explorer.bottom() - EXPLORER_BOTTOM_GAP, area.bottom())
        self._bottom_anchor = max(area.top() + self.height(), bottom)
        self.move(self.x(), self._bottom_anchor - self.height())

    def toggle_expanded(self, *_checked) -> None:
        self.expanded = not self.expanded
        self.expand_action.setChecked(self.expanded)
        area = self.screen().availableGeometry()
        center = self.geometry().center().x()
        self.resize(min(self._bar_width(), area.width()), self.height())
        self.move(max(area.left(), center - self.width() // 2), self.y())
        self.update_detail()
        self._show_results_area(self.results.count() > 0)

    def update_detail(self, *_changed) -> None:
        row = self.selected_row()
        self.detail.setText(row.detail if row else "")

    # Actions ------------------------------------------------------------------

    def search_contents(self, *_link) -> None:
        """Replace the query with the offered content search, which runs at once."""
        if self.content_search is not None:
            self.query.setText(self.content_search)

    def shows_missing(self, hit: LauncherHit) -> bool:
        """Whether `hit` is shown as a file that no longer exists."""
        rows = (
            self.results.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.results.count())
        )
        return any(row.missing and row.hit == hit for row in rows)

    def report_missing(self, name: str) -> None:
        self._show_status(f"{name} no longer exists; WinLauncher refreshes its folder.")

    def open_selected(self, *_clicked) -> None:
        self.open_requested.emit(self.selected_hit())

    def open_row(self, row: int) -> None:
        item = self.results.item(row)
        if item is None:
            return
        self.results.setCurrentRow(row)
        self.open_requested.emit(item.data(Qt.ItemDataRole.UserRole).hit)

    def _update_hint(self, *_items) -> None:
        hint = _shortcut_hint(
            None if self.results.isHidden() else self.selected_row(),
            explorer=self.explorer_mode,
            calculation=self.calculation_selected,
            dialog=self.serves_file_dialog,
        )
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    @contextmanager
    def _replacing_results(self) -> Iterator[None]:
        """While WinLauncher itself fills or clears the list, Explorer does not follow.

        Clearing moves the current row through the remaining rows first.
        """
        self._refreshing_results = True
        try:
            yield
        finally:
            self._refreshing_results = False

    def _selection_moved(self, *_items) -> None:
        if not self._refreshing_results:
            self._follow_in_explorer(direct_only=False)

    def _follow_in_explorer(self, *, direct_only: bool) -> None:
        """In the Explorer bar, Explorer selects the selected result.

        New results move Explorer only to a direct entry of the folder, replacing
        whatever Explorer's own type-ahead picked; arrow keys move it to any result.
        A file dialog does not follow.
        """
        row = self.selected_row()
        if not self.explorer_mode or self.serves_file_dialog:
            return
        if row is None or (direct_only and not row.direct):
            return
        if (hit := row.file_hit) is not None:
            self.explorer_selection.emit(hit)

    def set_pinned(self, pinned: bool) -> None:
        """Pinned, the launcher bar stays open after opening results and when focus
        leaves it, so several results can be opened in a row.

        Escape, the global shortcut and closing it from the tray still hide it. The
        Explorer bar is never pinned: it belongs to the Explorer window.
        """
        self.pinned = pinned
        self.pin_button.setChecked(pinned)
        self._use_theme_icons()
        if pinned:
            self.auto_hide.stop()

    def close_after_action(self) -> None:
        """A result was opened or acted on: hide, unless the launcher bar is pinned."""
        if self.pinned and not self.explorer_mode:
            return
        self.hide()

    def dismiss(self) -> None:
        """Escape: close; a file dialog gets the typed text back in its file-name box.

        Typing in an Open dialog's empty file-name box opens the bar, so leaving it
        must not lose a name or path the user meant to type there.
        """
        text = self.query.text()
        if self.serves_file_dialog and text:
            self.dialog_text_returned.emit(text)
        self.hide()

    @property
    def serves_file_dialog(self) -> bool:
        """Whether the bar was opened by typing in an Open or Save dialog."""
        return self.explorer_view is not None and self.view_kind is ViewKind.FILE_DIALOG

    def reveal_selected(self) -> None:
        if (row := self.selected_row()) and (hit := row.file_hit):
            self.reveal_requested.emit(hit)

    def favorite_selected(self) -> None:
        if (row := self.selected_row()) and (hit := row.file_hit):
            self.favorite_requested.emit(hit)

    def copy_target(self, row: ResultRow | None = None) -> None:
        """Copy the path or URL of `row`, by default of the selected row."""
        row = row or self.selected_row()
        if row is None:
            return
        QApplication.clipboard().setText(row.target)
        self._show_status(f"{row.target_noun} copied to clipboard")

    def copy_selected(self, row: ResultRow | None = None, *, cut: bool = False) -> bool:
        """Put `row`'s file on the clipboard for Explorer; a website copies its URL.

        `row` defaults to the selected row. Returns whether there was something to
        copy; a website cannot be cut.
        """
        row = row or self.selected_row()
        if row is None or (cut and row.file_hit is None):
            return False
        QApplication.clipboard().setMimeData(transfer_data(row, cut=cut))
        if row.file_hit is None:
            self._show_status(f"{row.target_noun} copied to clipboard")
        else:
            action = "Cut" if cut else "Copied"
            self._show_status(f"{action} {row.name}; paste it in Explorer")
        return True

    def _show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.show()
        self._fit_height()

    def keyboard_context_menu(self) -> None:
        if item := self.results.currentItem():
            self.context_menu(self.results.visualItemRect(item).center())

    def context_menu(self, point: QPoint) -> None:
        """Every action is bound to the clicked hit, even if results refresh meanwhile."""
        item = self.results.itemAt(point)
        if item is None:
            return
        self.results.setCurrentItem(item)
        row: ResultRow = item.data(Qt.ItemDataRole.UserRole)
        shift = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        if shift and (hit := row.file_hit) is not None:
            # As in Explorer, Shift asks for Windows' full menu with its extended entries.
            self.shell_menu_requested.emit(hit, True)
            return
        menu = Menu(self)
        if (hit := row.file_hit) is not None:
            self._add_file_actions(menu, row, hit)
        else:
            self._add_website_actions(menu, row)
        menu.exec(self.results.viewport().mapToGlobal(point))
        menu.deleteLater()

    def _add_website_actions(self, menu: QMenu, row: ResultRow) -> None:
        menu.addAction(f"Open in {row.opens_in}", lambda: self.open_requested.emit(row.hit))
        menu.addAction(f"Copy {row.target_noun}", lambda: self.copy_target(row))
        menu.addSeparator()
        menu.addAction("Settings…", self.settings_requested.emit)

    def _add_file_actions(self, menu: QMenu, row: ResultRow, hit: SearchHit) -> None:
        clipboard = QApplication.clipboard()
        menu.addAction("Open", lambda: self.open_requested.emit(hit))
        menu.addAction("Show in containing folder", lambda: self.reveal_requested.emit(hit))
        menu.addSeparator()
        if commands := _menu_commands(self.menu_commands, hit.item.kind):
            menu.setObjectName("withIcons")
            for command in commands:
                keys = f"	{command.shortcut}+Enter" if command.shortcut else ""
                menu.addAction(
                    self._command_icon(command),
                    command.title + keys,
                    partial(self.command_requested.emit, hit, command),
                )
            menu.addSeparator()
        menu.addAction("Copy\tCtrl+C", lambda: self.copy_selected(row))
        menu.addAction("Cut\tCtrl+X", lambda: self.copy_selected(row, cut=True))
        menu.addAction("Copy path\tCtrl+Shift+C", lambda: self.copy_target(row))
        menu.addAction("Copy file name", lambda: clipboard.setText(hit.item.path.name))
        menu.addAction(
            "Copy parent folder path", lambda: clipboard.setText(str(hit.item.path.parent))
        )
        menu.addSeparator()
        favorite_label = "Remove favorite" if hit.item.pinned else "Add to favorites"
        menu.addAction(favorite_label, lambda: self.favorite_requested.emit(hit))
        menu.addAction(
            "Exclude this or a parent folder…",
            lambda: self.exclude_requested.emit(hit.item.path),
        )
        menu.addSeparator()
        menu.addAction(
            "More options…\tShift+Right-click",
            lambda: self.shell_menu_requested.emit(hit, False),
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.shown.emit()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.auto_hide.stop()
        self.hidden.emit()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
