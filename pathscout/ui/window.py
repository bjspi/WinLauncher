"""Launcher presentation: one compact search bar that grows with its results.

Widgets emit intents and never access the catalog.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QLocale, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pathscout.catalog.model import ItemKind
from pathscout.search.query import SearchQuery
from pathscout.search.ranking import SearchHit
from pathscout.ui.highlight import matched_positions
from pathscout.ui.icons import ResultIcons
from pathscout.ui.menus import Menu
from pathscout.ui.theme import Palette, app_icon

COMPACT_WIDTH = 640
EXPANDED_WIDTH = 900
EXPLORER_WIDTH = 520
EXPANDED_ROWS = 14
SHADOW_MARGIN = 14
EXPLORER_BOTTOM_GAP = 56  # Keep Explorer's status bar visible below the bar.


@dataclass(frozen=True, slots=True)
class RowLayout:
    """How result rows are drawn: two-line launcher rows or one-line Explorer rows."""

    height: int
    icon: int
    name_points: int
    two_lines: bool
    visible_rows: int


LAUNCHER_ROWS = RowLayout(height=52, icon=32, name_points=11, two_lines=True, visible_rows=9)
EXPLORER_ROWS = RowLayout(height=34, icon=20, name_points=10, two_lines=False, visible_rows=8)
NUMBERED_ROWS = 9  # Ctrl+1 … Ctrl+9 open these rows.
FILTERS = (
    ("All", None),
    ("Apps", ItemKind.APP),
    ("Files", ItemKind.FILE),
    ("Folders", ItemKind.FOLDER),
)
NO_MATCHES = "No matches. Try a shorter name, another spelling or fewer filters."
logger = logging.getLogger(__name__)
_METADATA_ROLE = Qt.ItemDataRole.UserRole + 1


class ResultDelegate(QStyledItemDelegate):
    """Paints one result row: icon, highlighted name, folder, shortcut and favorite star."""

    def __init__(self, parent: QListWidget):
        super().__init__(parent)
        self.icons = ResultIcons(self)
        self.icons.changed.connect(parent.viewport().update)
        self.terms: tuple[str, ...] = ()
        self.rows = LAUNCHER_ROWS
        self.scope: Path | None = None

    def sizeHint(self, option, index) -> QSize:
        return QSize(400, self.rows.height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        hit: SearchHit | None = index.data(Qt.ItemDataRole.UserRole)
        if hit is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row, size = option.rect, self.rows.icon
        self._paint_background(painter, row, option.state)
        icon = QRect(row.left() + 16, row.center().y() - size // 2, size, size)
        self.icons.icon_for(hit.item).paint(painter, icon)
        shortcut_width = self._paint_shortcut(painter, row, index.row(), hit.item.pinned)
        left = icon.right() + 14
        text = QRect(left, row.top(), row.right() - left - shortcut_width - 8, row.height())
        baseline = text.top() + 41 if self.rows.two_lines else text.center().y() + 6
        metadata = index.data(_METADATA_ROLE) or ""
        metadata_width = (
            min(text.width(), QFontMetrics(QFont("Segoe UI", 8)).horizontalAdvance(metadata) + 12)
            if metadata
            else 0
        )
        metadata_area = text.adjusted(max(0, text.width() - metadata_width + 12), 0, 0, 0)
        self._paint_folder(painter, metadata_area, metadata, baseline=baseline)
        location_area = text.adjusted(0, 0, -metadata_width, 0)
        if self.rows.two_lines:
            self._paint_name(painter, text, hit.item.name, baseline=text.top() + 23)
            self._paint_folder(painter, location_area, str(hit.item.path.parent), baseline=baseline)
        else:
            name_end = self._paint_name(painter, location_area, hit.item.name, baseline=baseline)
            location = location_area.adjusted(name_end - text.left() + 12, 0, 0, 0)
            self._paint_folder(painter, location, self._location(hit), baseline=baseline)
        painter.restore()

    def _location(self, hit: SearchHit) -> str:
        """In an Explorer search, the subfolder below the searched folder; else the folder."""
        parent = hit.item.path.parent
        if self.scope is None:
            return str(parent)
        if parent == self.scope:
            return ""
        try:
            return str(parent.relative_to(self.scope))
        except ValueError:
            return str(parent)

    @staticmethod
    def _paint_background(painter: QPainter, row: QRect, state: QStyle.StateFlag) -> None:
        color = _row_color(state)
        if color is not None:
            painter.fillRect(row, QColor(color))
        if state & QStyle.StateFlag.State_HasFocus:
            painter.fillRect(QRect(row.left(), row.top(), 3, row.height()), QColor(Palette.ACCENT))

    def _paint_name(self, painter: QPainter, area: QRect, name: str, *, baseline: int) -> int:
        """Draw the name with matched characters highlighted; return where it ends."""
        font = QFont("Segoe UI", self.rows.name_points)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        visible = metrics.elidedText(name, Qt.TextElideMode.ElideRight, area.width())
        highlighted = matched_positions(name, self.terms) if visible == name else set()
        x = area.left()
        for start, end, matched in _runs(visible, highlighted):
            painter.setPen(QColor(Palette.ACCENT if matched else Palette.TEXT))
            segment = visible[start:end]
            painter.drawText(x, baseline, segment)
            x += metrics.horizontalAdvance(segment)
        return x

    @staticmethod
    def _paint_folder(painter: QPainter, area: QRect, folder: str, *, baseline: int) -> None:
        if not folder or area.width() <= 0:
            return
        font = QFont("Segoe UI", 8)
        painter.setFont(font)
        painter.setPen(QColor(Palette.MUTED))
        text = QFontMetrics(font).elidedText(folder, Qt.TextElideMode.ElideMiddle, area.width())
        painter.drawText(area.left(), baseline, text)

    @staticmethod
    def _paint_shortcut(painter: QPainter, row: QRect, number: int, pinned: bool) -> int:
        """Draw `Ctrl+N` and the favorite star at the right; return the width used."""
        shortcut = f"Ctrl+{number + 1}" if number < NUMBERED_ROWS else ""
        label = f"★  {shortcut}".strip() if pinned else shortcut
        if not label:
            return 0
        font = QFont("Segoe UI", 10)
        painter.setFont(font)
        painter.setPen(QColor(Palette.SHORTCUT))
        width = QFontMetrics(font).horizontalAdvance(label) + 24
        area = QRect(row.right() - width - 4, row.top(), width, row.height())
        painter.drawText(area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, label)
        return width


def _row_color(state: QStyle.StateFlag) -> str | None:
    if state & QStyle.StateFlag.State_Selected:
        return Palette.SELECTED
    if state & QStyle.StateFlag.State_MouseOver:
        return Palette.HOVER
    return None


def _runs(text: str, highlighted: set[int]) -> Iterator[tuple[int, int, bool]]:
    """Split `text` into `(start, end, highlighted)` runs of equal highlighting."""
    start = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or (index in highlighted) != (start in highlighted):
            yield start, index, start in highlighted
            start = index


def _accessible_text(hit: SearchHit, metadata: str = "") -> str:
    """What screen readers announce for a row the delegate paints."""
    favorite = ", favorite" if hit.item.pinned else ""
    details = f"\n{metadata}" if metadata else ""
    return f"{hit.item.name}, {hit.item.kind.value}{favorite}{details}\n{hit.item.path}"


def _result_metadata(hit: SearchHit, query: SearchQuery | None) -> str:
    if query is None:
        return ""
    parts = []
    if query.min_size is not None or query.max_size is not None:
        parts.append(QLocale().formattedDataSize(hit.item.size))
    if query.modified_after is not None or query.modified_before is not None:
        parts.append(f"Modified {_format_modified(hit.item.modified)}")
    return " · ".join(parts)


def _format_modified(timestamp: float) -> str:
    if not timestamp:
        return "Unknown"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "Unknown"  # Outside the range Windows can represent.


def _containing_folder(hit: SearchHit) -> Path:
    return hit.item.path if hit.item.kind == ItemKind.FOLDER else hit.item.path.parent


def _soft_shadow(parent: QWidget) -> QGraphicsDropShadowEffect:
    shadow = QGraphicsDropShadowEffect(parent)
    shadow.setBlurRadius(28)
    shadow.setOffset(0, 4)
    shadow.setColor(QColor(0, 0, 0, 70))
    return shadow


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


class LauncherWindow(QWidget):
    query_changed = Signal()
    open_requested = Signal(object)
    reveal_requested = Signal(object)
    favorite_requested = Signal(object)
    exclude_requested = Signal(object)
    settings_requested = Signal()
    rescan_requested = Signal()
    restart_requested = Signal()
    about_requested = Signal()
    hidden = Signal()

    def __init__(self):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        super().__init__(None, flags | Qt.WindowType.WindowStaysOnTopHint)
        self.scope: Path | None = None
        self.kind: ItemKind | None = None
        self.expanded = False
        self.catalog_count = 0
        self.explorer_mode = False  # Compact bar next to the Explorer window.
        self._bottom_anchor: int | None = None
        self._resize_pending = False
        self.setObjectName("launcher")
        self.setWindowTitle("PathScout")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        # The window sizes itself to its content in `_fit_height`.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        surface = QFrame()
        surface.setObjectName("surface")
        surface.setGraphicsEffect(_soft_shadow(surface))
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(0, 0, 0, 6)
        layout.setSpacing(0)
        layout.addLayout(self._build_search_bar())
        self.divider = QFrame()
        self.divider.setObjectName("divider")
        self.divider.setFixedHeight(1)
        layout.addWidget(self.divider)
        self.empty = self._label("message")
        layout.addWidget(self.empty)
        layout.addWidget(self._build_results())
        self.detail = self._label("detail")
        self.status = self._label("status")
        self.activity = self._label("activity")
        for label in (self.detail, self.status, self.activity):
            layout.addWidget(label)
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
        self.query.installEventFilter(self)
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
        self.menu_button.setToolTip("PathScout menu")
        self.menu_button.setAccessibleName("PathScout menu")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setMenu(self._build_menu())
        row.addWidget(self.menu_button)
        return row

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
        menu.addAction("About PathScout").triggered.connect(self.about_requested)
        menu.addSeparator()
        menu.addAction("Restart PathScout").triggered.connect(self.restart_requested)
        menu.addAction("Hide", self.hide)
        return menu

    def _build_results(self) -> QListWidget:
        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.setAccessibleName("Search results")
        self.delegate = ResultDelegate(self.results)
        self.results.setItemDelegate(self.delegate)
        self.results.setMouseTracking(True)
        self.results.setUniformItemSizes(True)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self.context_menu)
        self.results.itemDoubleClicked.connect(self.open_selected)
        self.results.currentItemChanged.connect(self.update_detail)
        self.results.installEventFilter(self)
        return self.results

    @staticmethod
    def _label(name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(name)
        label.setWordWrap(True)
        label.hide()
        return label

    def _install_shortcuts(self) -> None:
        for sequence, action in (
            ("Escape", self.hide),
            ("Ctrl+L", self.focus_query),
            ("Ctrl+Return", self.reveal_selected),
            ("Ctrl+Enter", self.reveal_selected),  # Keypad Enter.
            ("Ctrl+Shift+C", self.copy_path),
            ("Ctrl+D", self.favorite_selected),
            ("Ctrl+,", self.settings_requested),
            ("Shift+F10", self.keyboard_context_menu),
        ):
            QShortcut(QKeySequence(sequence), self).activated.connect(action)
        for row in range(NUMBERED_ROWS):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{row + 1}"), self)
            shortcut.activated.connect(lambda row=row: self.open_row(row))

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
        if (
            event.type() in (QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate)
            and self._native_focus()
        ):
            from pathscout.platform.windows import foreground_details

            logger.info("Launcher %s; foreground=%s", event.type().name, foreground_details())
        return handled

    @staticmethod
    def _native_focus() -> bool:
        return os.name == "nt" and QApplication.platformName() == "windows"

    def _handle_key(self, watched: QObject, event: QKeyEvent) -> bool:
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not modifiers:
            self.open_selected()
            return True
        if watched is self.query and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._move_selection(1 if event.key() == Qt.Key.Key_Down else -1)
            return True
        return False

    def _move_selection(self, step: int) -> None:
        last = self.results.count() - 1
        self.results.setCurrentRow(max(0, min(last, self.results.currentRow() + step)))

    def focus_query(self) -> None:
        self.query.setFocus()
        self.query.selectAll()

    # Result state -------------------------------------------------------------

    def selected_hit(self) -> SearchHit | None:
        item = self.results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def set_catalog_count(self, count: int) -> None:
        self.catalog_count = count

    def set_results(
        self,
        hits: list[SearchHit],
        elapsed_ms: float,
        *,
        preserve_selection: bool = True,
        query: SearchQuery | None = None,
    ) -> None:
        """Show new results; keep the selected path only when the query is unchanged."""
        previous = self.selected_hit() if preserve_selection else None
        self.delegate.terms = query.terms if query is not None else ()
        self.results.clear()
        selected_row = 0
        for row, hit in enumerate(hits):
            metadata = _result_metadata(hit, query)
            item = QListWidgetItem(_accessible_text(hit, metadata))
            item.setData(Qt.ItemDataRole.UserRole, hit)
            item.setData(_METADATA_ROLE, metadata)
            item.setToolTip(f"{hit.item.path}\n{hit.reason}\nRelevance: {hit.score:.1f}")
            self.results.addItem(item)
            if previous is not None and hit.item.path == previous.item.path:
                selected_row = row
        if hits:
            self.results.setCurrentRow(selected_row)
        # An empty query without history stays a bare search bar.
        self.empty.setText(NO_MATCHES)
        self.empty.setVisible(not hits and bool(self.query.text().strip()))
        status = f"{len(hits)} results  ·  {elapsed_ms:.0f} ms  ·  {self.catalog_count:,} indexed"
        self.status.setText(status if hits else "")
        self.status.setVisible(bool(hits))
        self._show_results_area(bool(hits))

    def show_searching(self) -> None:
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
        self.results.clear()
        self.empty.setText(message)
        self.empty.show()
        self.status.hide()
        self._show_results_area(False)

    def _show_results_area(self, has_results: bool) -> None:
        self.results.setVisible(has_results)
        self.detail.setVisible(has_results and self.expanded)
        self._fit_height()

    def _fit_height(self) -> None:
        """Grow or shrink with the content, like a single search bar."""
        layout = self.delegate.rows
        limit = EXPANDED_ROWS if self.expanded and not self.explorer_mode else layout.visible_rows
        rows = min(self.results.count(), limit)
        self.results.setFixedHeight(rows * layout.height + 2 * self.results.frameWidth())
        below_bar = (self.empty, self.results, self.detail, self.status, self.activity)
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

    def set_kind(self, kind: ItemKind | None) -> None:
        self.kind = kind
        for action, (_, action_kind) in zip(self.kind_actions, FILTERS, strict=True):
            action.setChecked(action_kind == kind)
        self.query_changed.emit()
        self.query.setFocus()

    def set_scope(self, folder: Path | None) -> None:
        self._show_scope(folder)
        self.query_changed.emit()
        self.query.setFocus()

    def _show_scope(self, folder: Path | None) -> None:
        self.scope = folder
        if folder is None:
            self.clear_scope.hide()
            return
        self.clear_scope.setText(f"in {folder.name or folder}   ✕")
        self.clear_scope.setToolTip(f"Searching in {folder}. Click to search everywhere.")
        self.clear_scope.show()

    # Window placement -----------------------------------------------------------

    def present(
        self,
        *,
        text: str = "",
        scope: Path | None = None,
        near: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Show with `text` typed below `scope`.

        With `near` (an Explorer window in physical pixels) the compact Explorer
        bar opens at the bottom of that window; otherwise the launcher opens on
        the cursor's screen.
        """
        self._show_scope(scope)
        self.query.blockSignals(True)
        self.query.setText(text)
        self.query.blockSignals(False)
        self._set_explorer_mode(near is not None, scope)
        anchor = _logical_rect(near) if near is not None else None
        if anchor is not None:
            self._place_in(anchor)
        else:
            self._place_on_cursor_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        if self._native_focus():
            from pathscout.platform.windows import activate_window

            active = activate_window(int(self.winId()))
            logger.info(
                "Launcher presented: foreground_granted=%s scoped=%s", active, scope is not None
            )
            QTimer.singleShot(100, self._check_activation)
        self.query.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.query_changed.emit()

    def _check_activation(self) -> None:
        if self.isVisible() and self._native_focus():
            from pathscout.platform.windows import foreground_details, user32

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
        self.delegate.scope = scope if enabled else None
        self.results.doItemsLayout()
        self.query.setProperty("explorer", enabled)
        self.query.style().unpolish(self.query)
        self.query.style().polish(self.query)
        if enabled:
            self.search_bar.setContentsMargins(16, 6, 8, 6)
        else:
            self.search_bar.setContentsMargins(22, 12, 12, 10)
        self.menu_button.setIconSize(QSize(20, 20) if enabled else QSize(28, 28))

    def _place_on_cursor_screen(self) -> None:
        self._bottom_anchor = None
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(self._bar_width(), area.width()), self.height())
        self.move(area.x() + (area.width() - self.width()) // 2, area.y() + area.height() // 5)

    def _place_in(self, explorer: QRect) -> None:
        """Centered near the bottom of the Explorer window, inside its screen."""
        screen = QApplication.screenAt(explorer.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.resize(min(self._bar_width(), area.width()), self.height())
        left = explorer.center().x() - self.width() // 2
        self.move(max(area.left(), min(left, area.right() - self.width())), self.y())
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
        hit = self.selected_hit()
        if not hit:
            self.detail.clear()
            return
        size = QLocale().formattedDataSize(hit.item.size)
        modified = _format_modified(hit.item.modified)
        self.detail.setText(f"{hit.item.path}\n{size}  ·  Modified {modified}  ·  {hit.reason}")

    # Actions ------------------------------------------------------------------

    def open_selected(self, *_clicked) -> None:
        self.open_requested.emit(self.selected_hit())

    def open_row(self, row: int) -> None:
        item = self.results.item(row)
        if item is None:
            return
        self.results.setCurrentRow(row)
        self.open_requested.emit(item.data(Qt.ItemDataRole.UserRole))

    def reveal_selected(self) -> None:
        if hit := self.selected_hit():
            self.reveal_requested.emit(hit)

    def favorite_selected(self) -> None:
        if hit := self.selected_hit():
            self.favorite_requested.emit(hit)

    def copy_path(self, hit: SearchHit | None = None) -> None:
        hit = hit or self.selected_hit()
        if hit is None:
            return
        QApplication.clipboard().setText(str(hit.item.path))
        self.status.setText("Path copied to clipboard")
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
        hit: SearchHit = item.data(Qt.ItemDataRole.UserRole)
        clipboard = QApplication.clipboard()
        menu = Menu(self)
        menu.addAction("Open", lambda: self.open_requested.emit(hit))
        menu.addAction("Show in containing folder", lambda: self.reveal_requested.emit(hit))
        menu.addSeparator()
        menu.addAction("Copy path", lambda: self.copy_path(hit))
        menu.addAction("Copy file name", lambda: clipboard.setText(hit.item.path.name))
        menu.addAction(
            "Copy parent folder path", lambda: clipboard.setText(str(hit.item.path.parent))
        )
        menu.addSeparator()
        favorite_label = "Remove favorite" if hit.item.pinned else "Add to favorites"
        menu.addAction(favorite_label, lambda: self.favorite_requested.emit(hit))
        folder = _containing_folder(hit)
        menu.addAction(
            "Exclude this or a parent folder…", lambda: self.exclude_requested.emit(folder)
        )
        menu.exec(self.results.viewport().mapToGlobal(point))
        menu.deleteLater()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.hidden.emit()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
