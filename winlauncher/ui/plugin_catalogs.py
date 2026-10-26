"""Plugin catalogs in the GUI: periodic checks, update notices, installs and removals.

Git and disk work runs on the plugin catalog worker, one job at a time, so a
check never races an install on the same clone. The watcher keeps the last
result of every configured catalog for the Settings page and announces each
plugin update once.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QObject, QTimer, Signal

from winlauncher.plugins.catalog import Catalog, CatalogPlugin, InstallState, PluginCatalogs
from winlauncher.plugins.config import catalog_key
from winlauncher.settings import Settings
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory

FIRST_CHECK_DELAY_MS = 60_000  # After startup, once indexing and plugins had their turn.
CHECK_INTERVAL_HOURS = 6
CHECK_INTERVAL_MS = CHECK_INTERVAL_HOURS * 60 * 60 * 1000
RESTART_HINT = "Plugins are loaded at start: restart WinLauncher from the tray to apply it."
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogOutcome:
    """How an install or removal ended, for the page that asked for it."""

    text: str  # A sentence for the user.
    failed: bool


class CatalogWatcher(QObject):
    """The configured catalogs as last read, kept current in the background."""

    changed = Signal()  # A check started or finished, or a plugin was installed or removed.
    updates_found = Signal(list)  # `CatalogPlugin`s with a version not announced before.
    finished = Signal(object)  # The `CatalogOutcome` of an install or removal.

    def __init__(
        self,
        library: PluginCatalogs,
        tasks: BackgroundTasks,
        settings: Callable[[], Settings],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.library, self.tasks, self.settings = library, tasks, settings
        self.catalogs: tuple[Catalog, ...] = ()
        self.checking = False
        self.working = False  # An install or removal is running.
        self.checked_at: float | None = None
        self._announced: set[tuple[str, str, str]] = set()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check)

    def start(self) -> None:
        """List the clones at once, then ask the remotes after a delay and periodically."""
        urls = self.urls()
        self.tasks.submit(
            TaskCategory.PLUGIN_CATALOG,
            partial(self._read_all, urls),
            self._read,
            partial(logger.warning, "Plugin catalogs could not be read: %s"),
        )
        self._timer.start()
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, self, self.check)

    def urls(self) -> tuple[str, ...]:
        return self.settings().plugin_catalogs

    def apply(self) -> None:
        """Follow edited settings: a changed catalog list is checked right away."""
        if not self._shows(self.urls()):
            self.check()

    def check(self) -> None:
        if self._closed or self.checking:
            return
        self.checking = True
        self.changed.emit()
        self.tasks.submit(
            TaskCategory.PLUGIN_CATALOG,
            partial(self._fetch_all, self.urls()),
            self._checked,
            self._check_failed,
        )

    def install(self, catalog: Catalog, plugin: CatalogPlugin) -> None:
        verb = "updated" if plugin.state is InstallState.UPDATE else "installed"
        self._change(
            catalog, partial(self.library.install, catalog.url, plugin), f"{plugin.name} is {verb}"
        )

    def remove(self, catalog: Catalog, plugin: CatalogPlugin) -> None:
        self._change(
            catalog, partial(self.library.remove, catalog.url, plugin), f"{plugin.name} is removed"
        )

    def close(self) -> None:
        self._closed = True
        self._timer.stop()

    # Worker side -----------------------------------------------------------------

    def _read_all(self, urls: Sequence[str]) -> tuple[Catalog, ...]:
        return tuple(self.library.read(url) for url in urls)

    def _fetch_all(self, urls: Sequence[str]) -> tuple[Catalog, ...]:
        self.library.prune(urls)
        return tuple(self.library.fetch(url) for url in urls)

    # GUI side --------------------------------------------------------------------

    def _read(self, catalogs: tuple[Catalog, ...]) -> None:
        if not self.checking and self.checked_at is None:  # A check may have been faster.
            self.catalogs = catalogs
            self.changed.emit()

    def _checked(self, catalogs: tuple[Catalog, ...]) -> None:
        self.checking = False
        self.catalogs, self.checked_at = catalogs, time.time()
        self.changed.emit()
        self._announce_updates()
        if not self._shows(self.urls()):  # The list was edited while the check ran.
            self.check()

    def _check_failed(self, message: str) -> None:
        logger.warning("Plugin catalog check failed: %s", message)
        self.checking = False
        self.changed.emit()

    def _shows(self, urls: Sequence[str]) -> bool:
        shown = [catalog_key(catalog.url) for catalog in self.catalogs]
        return shown == [catalog_key(url) for url in urls]

    def _announce_updates(self) -> None:
        fresh = []
        for catalog in self.catalogs:
            for plugin in catalog.plugins:
                key = (catalog_key(catalog.url), plugin.folder, plugin.version)
                if plugin.state is InstallState.UPDATE and key not in self._announced:
                    self._announced.add(key)
                    fresh.append(plugin)
        if fresh:
            self.updates_found.emit(fresh)

    def _change(self, catalog: Catalog, work: Callable[[], CatalogPlugin], done_text: str) -> None:
        if self.working:
            return
        self.working = True
        self.changed.emit()
        self.tasks.submit(
            TaskCategory.PLUGIN_CATALOG,
            work,
            partial(self._changed, catalog.url, done_text),
            self._change_failed,
        )

    def _changed(self, url: str, done_text: str, plugin: CatalogPlugin) -> None:
        self.working = False
        self.catalogs = tuple(
            catalog.with_plugin(plugin) if catalog.url == url else catalog
            for catalog in self.catalogs
        )
        self.changed.emit()
        self.finished.emit(CatalogOutcome(f"{done_text}. {RESTART_HINT}", failed=False))

    def _change_failed(self, message: str) -> None:
        self.working = False
        self.changed.emit()
        self.finished.emit(CatalogOutcome(message, failed=True))
