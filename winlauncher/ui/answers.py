"""Plugin answers to the typed text, such as `5 km in mi`, in the calculator's row.

Plugins answer on a worker of their own, a moment after typing pauses, so neither
the search nor the next keystroke waits for them. Only the newest text is asked:
while one question runs, further typing waits for it and then asks once more.
Arithmetic answers first and needs no plugin; Explorer's bar answers nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

from PySide6.QtCore import QObject, QTimer

from winlauncher.calculator import Calculation, calculate
from winlauncher.plugins.host import PluginHost
from winlauncher.settings import Settings
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory
from winlauncher.ui.window import LauncherWindow

ASK_DELAY_MS = 60  # After the last keystroke, like the search's own pause.
logger = logging.getLogger(__name__)


class PluginAnswers(QObject):
    def __init__(
        self,
        host: PluginHost,
        window: LauncherWindow,
        tasks: BackgroundTasks,
        settings: Callable[[], Settings],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.host, self.window, self.tasks, self.settings = host, window, tasks, settings
        self._asking = False
        self._ask_again = False
        self._pause = QTimer(self)
        self._pause.setSingleShot(True)
        self._pause.setInterval(ASK_DELAY_MS)
        self._pause.timeout.connect(self.ask)

    def text_changed(self) -> None:
        self._pause.start()

    def ask(self) -> None:
        """Ask the answering plugins about the text in the bar, unless nothing could."""
        text = self.window.query.text()
        if self.window.explorer_mode or not text.strip() or calculate(text) is not None:
            return
        configs = self.settings().plugins
        if not self.host.answering(configs):
            return
        if self._asking:
            self._ask_again = True
            return
        self._asking = True
        self.tasks.submit(
            TaskCategory.ANSWER,
            partial(self.host.answer, text, configs),
            partial(self._answered, text),
            self._failed,
        )

    def _answered(self, text: str, answer: Calculation | None) -> None:
        self._asking = False
        self.window.show_answer(text, answer)
        if self._ask_again:
            self._ask_again = False
            self.ask()

    def _failed(self, message: str) -> None:
        self._asking = False
        logger.warning("Plugin answer failed: %s", message)
