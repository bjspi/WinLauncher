"""Plugins in the GUI: periodic refreshes, questions from plugin workers, item icons.

Refreshes run on the plugin pool, so neither indexing nor searching waits for a
slow plugin. A plugin command, such as Sign in, may ask questions from its worker;
`Questions` shows them on the GUI thread and hands the answer back.
"""

from __future__ import annotations

import logging
import threading
import time
import zlib
from collections.abc import Callable
from functools import partial

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLineEdit, QWidget

from winlauncher.plugins.api import PluginCommand
from winlauncher.plugins.host import PluginHost
from winlauncher.plugins.search import PluginHit
from winlauncher.settings import Settings
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory
from winlauncher.ui.widgets import InfoDialog, line_icon

FIRST_REFRESH_DELAY_MS = 4000  # After startup, once the first scan had its head start.
QUESTION_TIMEOUT_S = 600  # A forgotten question frees its worker after this.
ICON_SIZE = 64
MAX_CACHED_ICONS = 2000
# Initials backgrounds, picked per item like Telegram's own placeholder avatars.
INITIALS_COLORS = ("#e17076", "#eda86c", "#a695e7", "#7bc862", "#6ec9cb", "#65aadd", "#ee7aae")
logger = logging.getLogger(__name__)


class PluginScheduler(QObject):
    """Refreshes each enabled plugin at its interval."""

    items_changed = Signal()  # A refresh or command finished; its items may differ.
    status_changed = Signal()  # A refresh started or finished, for the Settings page.

    def __init__(
        self,
        host: PluginHost,
        tasks: BackgroundTasks,
        settings: Callable[[], Settings],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.host, self.tasks, self.settings = host, tasks, settings
        self.questions = Questions(self)
        self._timers: dict[str, QTimer] = {}
        self._running: dict[str, threading.Event] = {}
        self._closed = False

    def start(self) -> None:
        self.host.load_cached()
        self.apply()

    def apply(self) -> None:
        """Follow the settings: start, stop or reschedule each plugin's refreshes."""
        configs = self.settings().plugins
        for plugin in self.host.plugins:
            config = self.host.config(plugin.id, configs)
            timer = self._timers.get(plugin.id)
            if not config.enabled:
                if timer is not None:
                    timer.stop()
                if (cancel := self._running.pop(plugin.id, None)) is not None:
                    cancel.set()
                continue
            interval = config.refresh_minutes * 60_000
            if timer is None:
                timer = self._timers[plugin.id] = QTimer(self)
                timer.timeout.connect(partial(self.refresh, plugin.id))
            if timer.interval() != interval or not timer.isActive():
                timer.start(interval)
                state = self.host.state(plugin.id)
                stale = state.refreshed is None or time.time() - state.refreshed > interval / 1000
                if stale or state.error:
                    QTimer.singleShot(
                        FIRST_REFRESH_DELAY_MS, self, partial(self.refresh, plugin.id)
                    )

    def refresh_all(self) -> None:
        for plugin in self.host.plugins:
            self.refresh(plugin.id)

    def refresh(self, plugin_id: str) -> None:
        if self._closed or plugin_id in self._running:
            return
        config = self.host.config(plugin_id, self.settings().plugins)
        if not config.enabled:
            return
        cancel = self._running[plugin_id] = threading.Event()
        self.tasks.submit(
            TaskCategory.PLUGIN,
            partial(self.host.refresh, plugin_id, config, cancel),
            partial(self._refreshed, plugin_id, cancel),
            partial(self._refresh_failed, plugin_id, cancel),
        )
        self.status_changed.emit()

    def _refreshed(self, plugin_id: str, cancel: threading.Event, _state) -> None:
        if self._running.get(plugin_id) is cancel:
            del self._running[plugin_id]
        self.items_changed.emit()
        self.status_changed.emit()

    def _refresh_failed(self, plugin_id: str, cancel: threading.Event, message: str) -> None:
        logger.info("Plugin %s refresh failed: %s", plugin_id, message)
        self._refreshed(plugin_id, cancel, None)

    def is_refreshing(self, plugin_id: str) -> bool:
        return plugin_id in self._running

    def run_command(
        self,
        plugin_id: str,
        command: PluginCommand,
        settings: Settings,
        parent: QWidget | None,
        done: Callable[[str | None, bool], None],
    ) -> None:
        """Run `command` on the plugin pool; `done(message, failed)` follows on the GUI thread.

        `settings` may be unsaved edits of the Settings dialog, so a sign-in uses
        the options shown there.
        """
        config = self.host.config(plugin_id, settings.plugins)
        ask = partial(self.questions.ask, parent)
        self.tasks.submit(
            TaskCategory.PLUGIN,
            partial(self.host.run_command, plugin_id, command, config, ask),
            lambda message: (
                done(message, False),
                self.items_changed.emit(),
                self.refresh(plugin_id),
            ),
            lambda message: done(message, True),
        )

    def close(self) -> None:
        self._closed = True
        for timer in self._timers.values():
            timer.stop()
        for cancel in self._running.values():
            cancel.set()
        self.questions.close()


class Questions(QObject):
    """Asks the user on the GUI thread for a plugin worker that waits for the answer."""

    _asked = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pending: list[_Question] = []
        self._closed = False
        self._asked.connect(self._show, Qt.ConnectionType.QueuedConnection)

    def ask(self, parent: QWidget | None, title: str, text: str, *, secret: bool = False):
        """Called on a worker thread; blocks until the user answers or cancels."""
        question = _Question(parent, title, text, secret)
        if self._closed:
            return None
        self._pending.append(question)
        self._asked.emit(question)
        question.answered.wait(QUESTION_TIMEOUT_S)
        return question.answer

    def close(self) -> None:
        self._closed = True
        for question in self._pending:
            question.answered.set()

    def _show(self, question: _Question) -> None:
        try:
            if not self._closed:
                question.answer = ask_text(
                    question.parent, question.title, question.text, secret=question.secret
                )
        finally:
            if question in self._pending:
                self._pending.remove(question)
            question.answered.set()


class _Question:
    def __init__(self, parent: QWidget | None, title: str, text: str, secret: bool):
        self.parent, self.title, self.text, self.secret = parent, title, text, secret
        self.answer: str | None = None
        self.answered = threading.Event()


def ask_text(parent: QWidget | None, title: str, text: str, *, secret: bool = False) -> str | None:
    """One line of text in WinLauncher's look; None when cancelled."""
    from winlauncher.ui.theme import colors

    dialog = InfoDialog(title, text, line_icon("help", colors().accent), parent)
    field = QLineEdit()
    field.setAccessibleName(title)
    if secret:
        field.setEchoMode(QLineEdit.EchoMode.Password)
    dialog.add_widget(field)
    dialog.add_button("Cancel", dialog.reject)
    dialog.add_button("Continue", dialog.accept, primary=True)
    field.setFocus()
    accepted = dialog.exec() == InfoDialog.DialogCode.Accepted
    answer = field.text().strip()
    dialog.deleteLater()
    return answer if accepted and answer else None


class PluginIcons(QObject):
    """Icons of plugin items: their image, round for avatars, else initials or the plugin's."""

    changed = Signal()  # Never emitted: icons load at once. Matches the other icon caches.

    def __init__(self, host: PluginHost | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.host = host
        self._cache: dict[tuple[str, str, str], QIcon] = {}

    def icon_for(self, hit: PluginHit) -> QIcon:
        plugin = self.host.plugin(hit.plugin_id) if self.host is not None else None
        round_icons = plugin is not None and plugin.round_icons
        key = (hit.plugin_id, hit.item.icon, hit.item.name if round_icons else "")
        if (icon := self._cache.get(key)) is not None:
            return icon
        if len(self._cache) >= MAX_CACHED_ICONS:
            self._cache.clear()
        icon = _image_icon(hit.item.icon, round_icons) if hit.item.icon else None
        if icon is None and round_icons:
            icon = _initials_icon(hit.item.name, hit.item.key)
        if icon is None and plugin is not None and plugin.icon:
            icon = _image_icon(plugin.icon, False)
        icon = self._cache[key] = icon or line_icon("search")
        return icon


def _image_icon(path: str, round_icon: bool) -> QIcon | None:
    image = QImage(path)
    if image.isNull():
        return None
    if not round_icon:
        return QIcon(QPixmap.fromImage(image))
    image = image.scaled(
        ICON_SIZE,
        ICON_SIZE,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas, painter = _round_canvas()
    painter.drawImage((ICON_SIZE - image.width()) // 2, (ICON_SIZE - image.height()) // 2, image)
    painter.end()
    return QIcon(canvas)


def _initials_icon(name: str, key: str) -> QIcon:
    words = [word for word in name.split() if word[:1].isalnum()]
    initials = "".join(word[0] for word in words[:2]).upper() or name[:1].upper()
    canvas, painter = _round_canvas()
    painter.fillRect(canvas.rect(), QColor(INITIALS_COLORS[zlib.crc32(key.encode()) % 7]))
    painter.setPen(QColor("white"))
    font = QFont("Segoe UI", 22)
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, initials)
    painter.end()
    return QIcon(canvas)


def _round_canvas() -> tuple[QPixmap, QPainter]:
    canvas = QPixmap(ICON_SIZE, ICON_SIZE)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, ICON_SIZE, ICON_SIZE))
    painter.setClipPath(clip)
    return canvas, painter
