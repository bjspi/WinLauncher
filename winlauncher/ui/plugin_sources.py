"""Plugin sources in the GUI: periodic checks, update notices, installs and activations.

Git, pip and disk work runs on the plugin source worker, one job at a time, so a
check never races an install on the same clone. The watcher keeps the last result
of every configured source for the Settings page and announces each update once.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from winlauncher.platform.git import checkout_root
from winlauncher.plugins.config import source_key
from winlauncher.plugins.packages import install_packages
from winlauncher.plugins.sources import InstallState, Offer, PluginSources, Source
from winlauncher.releases import installation
from winlauncher.settings import Settings
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory

FIRST_CHECK_DELAY_MS = 60_000  # After startup, once indexing and plugins had their turn.
CHECK_INTERVAL_HOURS = 6
CHECK_INTERVAL_MS = CHECK_INTERVAL_HOURS * 60 * 60 * 1000
logger = logging.getLogger(__name__)


class SourceAction(StrEnum):
    INSTALL = "install"  # Also an update: a remote plugin copied into the plugins folder.
    UNINSTALL = "uninstall"
    ACTIVATE = "activate"  # A local plugin's packages; the activation itself is a setting.


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """How an install, uninstall or activation ended, for the page that asked for it."""

    action: SourceAction
    offer: Offer  # As it is now: an installed offer carries its new state.
    loads_from: Path  # The folder WinLauncher loads the plugin from.
    error: str = ""  # Why it failed, for the user; empty when it worked.

    @property
    def failed(self) -> bool:
        return bool(self.error)


class SourceWatcher(QObject):
    """The configured sources as last read, kept current in the background."""

    changed = Signal()  # A check started or finished, or a plugin was installed or removed.
    updates_found = Signal(list)  # `Offer`s with a version not announced before.
    finished = Signal(object)  # The `SourceOutcome` of an install, uninstall or activation.

    def __init__(
        self,
        library: PluginSources,
        tasks: BackgroundTasks,
        settings: Callable[[], Settings],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.library, self.tasks, self.settings = library, tasks, settings
        self.sources: tuple[Source, ...] = ()
        self.checking = False
        self.working = False  # An install, uninstall or activation is running.
        self.checked_at: float | None = None
        self._announced: set[tuple[str, str, str]] = set()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check)

    def start(self) -> None:
        """List what lies on disk at once, then ask the remotes after a delay and periodically."""
        self.tasks.submit(
            TaskCategory.PLUGIN_SOURCE,
            partial(self._read_all, self.locations()),
            self._read,
            partial(logger.warning, "Plugin sources could not be read: %s"),
        )
        self._timer.start()
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self, self.check)

    def locations(self) -> tuple[str, ...]:
        return self.settings().plugin_sources

    def apply(self) -> None:
        """Follow saved settings: a changed source list is checked right away."""
        if not self._shows(self.locations()):
            self.check()

    def check(self) -> None:
        if self._closed or self.checking:
            return
        self.checking = True
        self.changed.emit()
        self.tasks.submit(
            TaskCategory.PLUGIN_SOURCE,
            partial(self._fetch_all, self.locations()),
            self._checked,
            self._check_failed,
        )

    def install(self, offer: Offer) -> None:
        """Install or update a remote plugin: its packages first, then its folder."""
        self._run(SourceAction.INSTALL, offer, partial(self._install, offer))

    def uninstall(self, offer: Offer) -> None:
        self._run(SourceAction.UNINSTALL, offer, partial(self._uninstall, offer))

    def activate(self, offer: Offer) -> None:
        """Install a local plugin's packages; the page then marks it activated."""
        self._run(SourceAction.ACTIVATE, offer, partial(self._activate, offer))

    def close(self) -> None:
        self._closed = True
        self._timer.stop()

    # Worker side -----------------------------------------------------------------

    def _read_all(self, locations: Sequence[str]) -> tuple[Source, ...]:
        return tuple(self.library.read(location) for location in locations)

    def _fetch_all(self, locations: Sequence[str]) -> tuple[Source, ...]:
        self.library.prune(locations)
        return tuple(self.library.fetch(location) for location in locations)

    def _install(self, offer: Offer) -> SourceOutcome:
        install_packages(installation(checkout_root()), offer.plugin.manifest.requires)
        installed = self.library.install(offer)
        return SourceOutcome(SourceAction.INSTALL, installed, self.installed_folder(offer))

    def _uninstall(self, offer: Offer) -> SourceOutcome:
        removed = self.library.uninstall(offer)
        return SourceOutcome(SourceAction.UNINSTALL, removed, self.installed_folder(offer))

    def _activate(self, offer: Offer) -> SourceOutcome:
        install_packages(installation(checkout_root()), offer.plugin.manifest.requires)
        return SourceOutcome(SourceAction.ACTIVATE, offer, offer.folder)

    def installed_folder(self, offer: Offer) -> Path:
        """Where a remote plugin lies once installed."""
        return self.library.plugins / offer.folder.name

    # GUI side --------------------------------------------------------------------

    def _read(self, sources: tuple[Source, ...]) -> None:
        if not self.checking and self.checked_at is None:  # A check may have been faster.
            self.sources = sources
            self.changed.emit()

    def _checked(self, sources: tuple[Source, ...]) -> None:
        self.checking = False
        self.sources, self.checked_at = sources, time.time()
        self.changed.emit()
        self._announce_updates()
        if not self._shows(self.locations()):  # The list was edited while the check ran.
            self.check()

    def _check_failed(self, message: str) -> None:
        logger.warning("Plugin source check failed: %s", message)
        self.checking = False
        self.changed.emit()

    def _shows(self, locations: Sequence[str]) -> bool:
        shown = [source_key(source.location) for source in self.sources]
        return shown == [source_key(location) for location in locations]

    def _announce_updates(self) -> None:
        fresh = []
        for source in self.sources:
            for offer in source.offers:
                key = (source_key(source.location), offer.folder.name, offer.version)
                if offer.install is InstallState.UPDATE and key not in self._announced:
                    self._announced.add(key)
                    fresh.append(offer)
        if fresh:
            self.updates_found.emit(fresh)

    def _run(self, action: SourceAction, offer: Offer, work: Callable[[], SourceOutcome]) -> None:
        if self.working:
            return
        self.working = True
        self.changed.emit()
        loads_from = (
            offer.folder if action is SourceAction.ACTIVATE else self.installed_folder(offer)
        )
        self.tasks.submit(
            TaskCategory.PLUGIN_SOURCE,
            work,
            self._done,
            lambda error: self._done(SourceOutcome(action, offer, loads_from, error)),
        )

    def _done(self, outcome: SourceOutcome) -> None:
        self.working = False
        if not outcome.failed:
            self.sources = tuple(
                source.with_offer(outcome.offer)
                if source_key(source.location) == source_key(outcome.offer.source)
                else source
                for source in self.sources
            )
        self.changed.emit()
        self.finished.emit(outcome)
