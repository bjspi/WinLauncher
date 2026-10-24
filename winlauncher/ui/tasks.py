"""Run work on worker pools and deliver results to callbacks on the Qt thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from functools import partial
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal

logger = logging.getLogger(__name__)


class TaskCategory(StrEnum):
    """Each category has its own pool, so a long scan never delays a search or launch."""

    SEARCH = "search"
    PREFETCH = "prefetch"  # Results computed ahead of time, such as the start list.
    INDEX = "index"
    ACTION = "action"
    PLUGIN = "plugin"  # Plugin refreshes and commands, which may wait for the network.


_POOL_SIZES = {
    TaskCategory.SEARCH: 2,
    TaskCategory.PREFETCH: 1,
    TaskCategory.INDEX: 1,
    TaskCategory.ACTION: 1,
    TaskCategory.PLUGIN: 2,
}


class BackgroundTasks(QObject):
    # Carries a zero-argument callable; Qt queues it to this object's (the GUI) thread.
    _deliver = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pools = {
            category: ThreadPoolExecutor(max_workers=size, thread_name_prefix=category)
            for category, size in _POOL_SIZES.items()
        }
        self._closed = False
        # Queued even when the future is already done: callbacks never run inside submit().
        self._deliver.connect(self._run_delivery, Qt.ConnectionType.QueuedConnection)

    def submit(
        self,
        category: TaskCategory,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_failure: Callable[[str], None],
    ) -> Future:
        future = self._pools[category].submit(work)
        future.add_done_callback(partial(self._finish, category, on_success, on_failure))
        return future

    def close(self) -> None:
        self._closed = True
        for pool in self._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)

    def _finish(self, category, on_success, on_failure, future: Future) -> None:
        """Runs on the worker thread; only hands a callback to the GUI thread."""
        if self._closed or future.cancelled():
            return
        error = future.exception()
        if error is None:
            self._deliver.emit(partial(on_success, future.result()))
            return
        logger.error("Background %s task failed", category, exc_info=error)
        self._deliver.emit(partial(on_failure, str(error)))

    def _run_delivery(self, callback: Callable[[], None]) -> None:
        if not self._closed:
            callback()
