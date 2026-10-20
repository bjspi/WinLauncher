"""Background release watcher and one-click updates to the newest release tag.

Updates follow release tags, never untested commits, because only a release
ships the matching prebuilt Rust engine. Every installation can check for a
release through `git ls-remote`; only a source checkout applies it itself, by
fast-forwarding to the tag. Its packages are reinstalled at the next start
(see `pathscout.install`), when the old native engine is no longer loaded.

Git runs through QProcess, so neither the periodic check nor the update blocks
the GUI. The updater only reports outcomes; the controller decides what to show.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from pathscout import __version__
from pathscout.platform.git import checkout_root, current_head, git_available
from pathscout.releases import (
    REPOSITORY_URL,
    Installation,
    Release,
    installation,
    newest_release,
    remote_tags,
)

logger = logging.getLogger(__name__)
FIRST_CHECK_DELAY_MS = 20_000  # Do not slow down startup.
CHECK_INTERVAL_MS = 30 * 60 * 1000
GIT_TIMEOUT_MS = 120_000  # A stalled network or credential helper is killed.


class ReleaseUpdater(QObject):
    availability_changed = Signal(bool)
    checked = Signal(bool)  # Whether the remote could be asked.
    update_finished = Signal(bool, bool, str)  # Succeeded, HEAD moved, git output.

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.root = checkout_root()
        self.current = Release.from_version(__version__)
        self.latest: Release | None = None  # Newer than `current`, once found.
        self._check: QProcess | None = None
        self._update: QProcess | None = None
        self._head_before_update: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check_in_background)

    @property
    def installation(self) -> Installation:
        return installation(self.root)

    @property
    def supported(self) -> bool:
        """Whether new releases can be found; installed packages need git for it, too."""
        return git_available() and self.current is not None

    @property
    def can_apply(self) -> bool:
        return self.supported and self.installation is Installation.CHECKOUT

    @property
    def available(self) -> bool:
        return self.latest is not None

    @property
    def updating(self) -> bool:
        return self._update is not None

    def start_watching(self) -> None:
        if not self.supported or self._timer.isActive():
            return
        self._timer.start()
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self._check_in_background)

    def stop(self) -> None:
        self._timer.stop()
        for process in (self._check, self._update):
            if process is not None:
                process.kill()

    def apply(self) -> None:
        """Fast-forward the checkout to the newest release.

        `--ff-only` refuses diverged or dirty checkouts instead of opening a merge,
        and a checkout already past the release simply stays where it is.
        """
        if not self.can_apply or self.updating or self.latest is None:
            return
        self._head_before_update = current_head(self.root)
        tag = self.latest.tag
        self._update = self._start_git(
            ["fetch", "--quiet", "--tags", "origin"],
            lambda code, output: self._fetched(code, output, tag),
        )

    def _fetched(self, exit_code: int, output: str, tag: str) -> None:
        if exit_code != 0:
            self._update_done(exit_code, output)
            return
        self._update = self._start_git(
            ["merge", "--ff-only", f"refs/tags/{tag}"], self._update_done
        )

    def _update_done(self, exit_code: int, output: str) -> None:
        self._update = None
        logger.info("Update finished with %s: %s", exit_code, output)
        if exit_code != 0:
            self.update_finished.emit(False, False, output)
            return
        moved = current_head(self.root) != self._head_before_update
        if moved:
            self._set_latest(None)
        self.update_finished.emit(True, moved, output)

    def check_now(self) -> None:
        """Ask for the newest release now; `checked` reports when the answer is in."""
        if not self.supported or self.updating:
            self.checked.emit(False)
        elif self._check is None:
            self._start_check()

    def _check_in_background(self) -> None:
        if not (self.available or self.updating or self._check is not None):
            self._start_check()

    def _start_check(self) -> None:
        # A checkout asks its own remote; an installed package asks the project.
        remote = "origin" if self.root is not None else REPOSITORY_URL
        self._check = self._start_git(["ls-remote", "--tags", "--refs", remote], self._checked)

    def _checked(self, exit_code: int, output: str) -> None:
        self._check = None
        if exit_code != 0:
            logger.debug("Update check failed and will be retried: %s", output)
            self.checked.emit(False)
            return
        newest = newest_release(remote_tags(output))
        if newest is not None and self.current is not None and newest > self.current:
            self._set_latest(newest)
        self.checked.emit(True)

    def _start_git(self, arguments: list[str], on_done: Callable[[int, str], None]) -> QProcess:
        process = QProcess(self)
        process.setWorkingDirectory(str(self.root or Path.home()))
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

    def _set_latest(self, release: Release | None) -> None:
        self.latest = release
        self.availability_changed.emit(release is not None)


def _read_output(process: QProcess) -> str:
    output = bytes(process.readAll().data()).decode("utf-8", errors="replace").strip()
    process.deleteLater()
    return output


def _report_start_failure(process: QProcess, error: QProcess.ProcessError) -> None:
    """A git that cannot start emits no `finished`; report it like a failed run."""
    if error == QProcess.ProcessError.FailedToStart:
        process.finished.emit(-1, QProcess.ExitStatus.CrashExit)
