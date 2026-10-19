"""Background update watcher and one-click `git pull --ff-only` for source checkouts.

Git runs through QProcess, so neither the periodic fetch nor the pull blocks the
GUI. The updater only reports outcomes; the controller decides what to show.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from pathscout.platform.git import (
    checkout_root,
    current_head,
    git_available,
)

logger = logging.getLogger(__name__)
FIRST_CHECK_DELAY_MS = 20_000  # Do not slow down startup.
CHECK_INTERVAL_MS = 30 * 60 * 1000
GIT_TIMEOUT_MS = 120_000  # A stalled network or credential helper is killed.


class GitUpdater(QObject):
    availability_changed = Signal(bool)
    pull_finished = Signal(bool, bool, str)  # Succeeded, HEAD moved, git output.

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.root = checkout_root()
        self.available = False
        self._fetch: QProcess | None = None
        self._pull: QProcess | None = None
        self._head_before_pull: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._fetch_in_background)

    @property
    def supported(self) -> bool:
        return self.root is not None and git_available()

    @property
    def pulling(self) -> bool:
        return self._pull is not None

    def start_watching(self) -> None:
        if not self.supported or self._timer.isActive():
            return
        self._timer.start()
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self._fetch_in_background)

    def stop(self) -> None:
        self._timer.stop()
        for process in (self._fetch, self._pull):
            if process is not None:
                process.kill()

    def pull(self) -> None:
        """`--ff-only` refuses diverged or dirty checkouts instead of opening a merge."""
        if not self.supported or self.pulling:
            return
        self._head_before_pull = current_head(self.root)
        self._pull = self._start_git(["pull", "--ff-only"], self._pull_done)

    def _fetch_in_background(self) -> None:
        if self.available or self.pulling or self._fetch is not None:
            return
        self._fetch = self._start_git(["fetch", "--quiet"], self._fetch_done)

    def _start_git(self, arguments: list[str], on_done: Callable[[int, str], None]) -> QProcess:
        process = QProcess(self)
        process.setWorkingDirectory(str(self.root))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("GIT_TERMINAL_PROMPT", "0")
        environment.insert("GCM_INTERACTIVE", "Never")
        process.setProcessEnvironment(environment)
        process.finished.connect(lambda code, _status: on_done(code, _read_output(process)))
        process.errorOccurred.connect(lambda error: _report_start_failure(process, error))
        watchdog = QTimer(process)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(process.kill)
        watchdog.start(GIT_TIMEOUT_MS)
        process.start("git", arguments)
        logger.info("Background git started: operation=%s", arguments[0])
        return process

    def _fetch_done(self, exit_code: int, output: str) -> None:
        self._fetch = None
        if exit_code != 0:
            logger.debug("Update check failed and will be retried: %s", output)
            return
        self._fetch = self._start_git(["rev-list", "--count", "HEAD..@{u}"], self._behind_done)

    def _behind_done(self, exit_code: int, output: str) -> None:
        self._fetch = None
        if exit_code == 0 and output.isdigit() and int(output) > 0:
            self._set_available(True)

    def _pull_done(self, exit_code: int, output: str) -> None:
        self._pull = None
        logger.info("git pull finished with %s: %s", exit_code, output)
        if exit_code != 0:
            self.pull_finished.emit(False, False, output)
            return
        moved = current_head(self.root) != self._head_before_pull
        if moved:
            self._set_available(False)
        self.pull_finished.emit(True, moved, output)

    def _set_available(self, available: bool) -> None:
        self.available = available
        self.availability_changed.emit(available)


def _read_output(process: QProcess) -> str:
    output = bytes(process.readAll().data()).decode("utf-8", errors="replace").strip()
    process.deleteLater()
    return output


def _report_start_failure(process: QProcess, error: QProcess.ProcessError) -> None:
    """A git that cannot start emits no `finished`; report it like a failed run."""
    if error == QProcess.ProcessError.FailedToStart:
        process.finished.emit(-1, QProcess.ExitStatus.CrashExit)
