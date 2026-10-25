"""Run work on worker pools and deliver results to callbacks on the Qt thread."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
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
_Job = tuple[Future, Callable[[], Any]]


class WorkerPool:
    """A fixed set of daemon threads running submitted callables, newest last.

    `ThreadPoolExecutor` is deliberately not used: Python joins its workers while
    the interpreter shuts down, so one task that never returns — a plugin waiting
    on the network, a shell extension that hangs — would keep WinLauncher's process
    alive after the window is gone, and the next start could not take over its
    instance lock. These threads are daemons, so quitting never waits for them.
    """

    def __init__(self, workers: int, name: str):
        self._jobs: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._closed = False
        self._threads = [
            threading.Thread(target=self._work, name=f"{name}_{number}", daemon=True)
            for number in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, work: Callable[[], Any]) -> Future:
        """Queue `work`; its future carries the result or the exception it raised."""
        future: Future = Future()
        if self._closed:
            future.cancel()
            return future
        self._jobs.put((future, work))
        return future

    def close(self) -> None:
        """Stop taking work and drop what is still queued; running work is left alone."""
        self._closed = True
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                job[0].cancel()
        for _ in self._threads:
            self._jobs.put(None)

    def _work(self) -> None:
        while (job := self._jobs.get()) is not None:
            future, work = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(work())
            except BaseException as error:  # noqa: BLE001 - belongs to the future, not here.
                future.set_exception(error)


class BackgroundTasks(QObject):
    # Carries a zero-argument callable; Qt queues it to this object's (the GUI) thread.
    _deliver = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pools = {
            category: WorkerPool(size, category) for category, size in _POOL_SIZES.items()
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
            pool.close()

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
