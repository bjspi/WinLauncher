"""Background release watcher and one-click updates to the newest release tag.

Updates follow release tags, never untested commits, because only a release
ships the matching prebuilt Rust engine. Every installation can check for a
release through `git ls-remote`; only a source checkout applies it itself, by
fast-forwarding to the tag, or on request by setting it to the tag after
stashing local changes. Its packages are reinstalled at the next start
(see `winlauncher.install`), when the old native engine is no longer loaded.

Git runs through QProcess, so neither the periodic check nor the update blocks
the GUI. The updater only reports outcomes; the controller decides what to show.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from winlauncher import __version__
from winlauncher.platform.git import checkout_root, current_head, git_available
from winlauncher.releases import (
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
# A new tag's release is published once its workflow passed, about ten minutes later.
UNPUBLISHED_RETRY_MS = 5 * 60 * 1000
WHEEL_CHECK_TIMEOUT_MS = 20_000
NOT_FOUND = 404
FORCE_UPDATE_STASH_MESSAGE = "WinLauncher: local changes before a forced update"

# Answers whether a release's engine wheel can be downloaded; None if unknown.
PublishedCheck = Callable[[Release, Callable[[bool | None], None]], None]


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
        self._update_steps: list[list[str]] = []
        self._update_output: list[str] = []
        self.forced = False  # Whether the running or last update was a forced one.
        self._confirming = False  # Asking whether a new tag's release is published.
        self.ask_published: PublishedCheck = self._ask_published
        self._network: QNetworkAccessManager | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check_in_background)
        self._unpublished_retry = QTimer(self)
        self._unpublished_retry.setSingleShot(True)
        self._unpublished_retry.setInterval(UNPUBLISHED_RETRY_MS)
        self._unpublished_retry.timeout.connect(self._check_in_background)

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
        self._unpublished_retry.stop()
        for process in (self._check, self._update):
            if process is not None:
                process.kill()

    def apply(self) -> None:
        """Fast-forward the checkout to the newest release.

        `--ff-only` refuses diverged or dirty checkouts instead of opening a merge,
        and a checkout already past the release simply stays where it is.
        """
        if self._can_start_update():
            tag = self.latest.tag
            self._run_update(False, [_fetch_tag(tag), ["merge", "--ff-only", f"refs/tags/{tag}"]])

    def force_apply(self) -> None:
        """Set the checkout to the newest release even with local changes or commits.

        Local changes, untracked files included, go to a stash first; ignored files
        stay untouched. Replaced commits stay in the reflog, and the output starts
        with the previous HEAD so they can be found.
        """
        if self._can_start_update():
            tag = self.latest.tag
            stash = ["stash", "push", "--include-untracked", "-m", FORCE_UPDATE_STASH_MESSAGE]
            self._run_update(
                True, [_fetch_tag(tag), stash, ["reset", "--hard", f"refs/tags/{tag}"]]
            )

    def _can_start_update(self) -> bool:
        return self.can_apply and not self.updating and self.latest is not None

    def _run_update(self, forced: bool, steps: list[list[str]]) -> None:
        """Run the git steps in order; the first failing one ends the update."""
        self.forced = forced
        self._head_before_update = current_head(self.root)
        self._update_steps = steps
        self._update_output = []
        if forced and self._head_before_update:
            self._update_output.append(f"HEAD {self._head_before_update}")
        self._next_update_step()

    def _next_update_step(self) -> None:
        self._update = self._start_git(self._update_steps.pop(0), self._update_step_done)

    def _update_step_done(self, exit_code: int, output: str) -> None:
        if output:
            self._update_output.append(output)
        if exit_code == 0 and self._update_steps:
            self._next_update_step()
            return
        self._update_steps = []
        self._update_done(exit_code, "\n".join(self._update_output))

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
        elif self._check is None and not self._confirming:
            self._start_check()

    def _check_in_background(self) -> None:
        if not (self.available or self.updating or self._check is not None or self._confirming):
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
            self._confirming = True
            self.ask_published(newest, lambda published: self._confirmed(newest, published))
            return
        self.checked.emit(True)

    def _confirmed(self, release: Release, published: bool | None) -> None:
        """Offer `release` only once installing it can download its engine wheel."""
        self._confirming = False
        if published and self.root is not None:
            # The checkout fetches the release now, so its changes can be shown offline.
            self._check = self._start_git(
                _fetch_tag(release.tag), lambda code, output: self._fetched(release, code, output)
            )
            return
        if published:
            self._set_latest(release)
        elif published is False:
            logger.info("Release %s is tagged but not published yet", release.tag)
            self._unpublished_retry.start()
        self.checked.emit(published is not None)

    def _fetched(self, release: Release, exit_code: int, output: str) -> None:
        """A release that could not be fetched is offered after the next check."""
        self._check = None
        if exit_code == 0:
            self._set_latest(release)
        else:
            logger.debug("Fetching release %s failed and will be retried: %s", release.tag, output)
        self.checked.emit(exit_code == 0)

    def _ask_published(self, release: Release, answer: Callable[[bool | None], None]) -> None:
        """Ask GitHub for the wheel without following its redirect to the download host.

        An uploaded asset answers with a redirect, a missing one with 404; the
        download host may still answer 404 for a short while after the upload.
        """
        if self._network is None:
            self._network = QNetworkAccessManager(self)
        request = QNetworkRequest(QUrl(release.native_wheel_url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(WHEEL_CHECK_TIMEOUT_MS)
        reply = self._network.head(request)
        reply.finished.connect(lambda: answer(_published(reply)))

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


def _published(reply: QNetworkReply) -> bool | None:
    """True for a found or redirected asset, False for 404, None if GitHub was unreachable."""
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    reply.deleteLater()
    if not status:
        logger.debug("Release check unanswered: %s", reply.errorString())
        return None
    if status == NOT_FOUND:
        return False
    return 200 <= status < 400 or None


def _fetch_tag(tag: str) -> list[str]:
    """Fetch only this release tag; `+` replaces a local tag of the same name."""
    return ["fetch", "--quiet", "--no-tags", "origin", f"+refs/tags/{tag}:refs/tags/{tag}"]


def _read_output(process: QProcess) -> str:
    output = bytes(process.readAll().data()).decode("utf-8", errors="replace").strip()
    process.deleteLater()
    return output


def _report_start_failure(process: QProcess, error: QProcess.ProcessError) -> None:
    """A git that cannot start emits no `finished`; report it like a failed run."""
    if error == QProcess.ProcessError.FailedToStart:
        process.finished.emit(-1, QProcess.ExitStatus.CrashExit)
