"""Schedule catalog work: one job at a time, most important pending work first.

Priority: a pending full scan, then the broadest dirty folder, then a batch of
changed files. Folder and file repairs never postpone the periodic full scan.
While an Explorer search session is open, full scans wait so its scoped
snapshot is not replaced underneath the user.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from winlauncher.catalog.indexer import CatalogIndexer
from winlauncher.catalog.model import ScanReport, is_within
from winlauncher.catalog.store import CatalogStore
from winlauncher.platform.shell import application_roots, registered_applications
from winlauncher.platform.store_apps import store_app_folder, sync_store_apps
from winlauncher.settings import Settings
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory

FILE_REPAIR_BATCH_SIZE = 500
MILLISECONDS_PER_SECOND = 1000


class IndexJobKind(StrEnum):
    FULL = "full"
    FOLDER = "folder"
    FILES = "files"


@dataclass(frozen=True, eq=False)
class IndexJob:
    """Compared by identity: a result belongs to exactly the job that produced it."""

    kind: IndexJobKind
    folder: Path | None = None
    files: tuple[Path, ...] = ()
    cancel: threading.Event = field(default_factory=threading.Event)


class IndexScheduler(QObject):
    started = Signal(object)  # IndexJob
    completed = Signal(object)  # ScanReport
    failed = Signal(str)
    progress = Signal(int)  # Emitted from the worker thread; Qt queues it.

    def __init__(
        self,
        tasks: BackgroundTasks,
        indexer: CatalogIndexer,
        store: CatalogStore,
        settings: Callable[[], Settings],
        full_scan_deferred: Callable[[], bool],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.tasks, self.indexer, self.store = tasks, indexer, store
        self.settings, self.full_scan_deferred = settings, full_scan_deferred
        self.running: IndexJob | None = None
        self.full_scan_pending = False
        self.dirty_folders: set[Path] = set()
        self.dirty_files: set[Path] = set()
        self.catalog_count = store.count()
        self.last_report: ScanReport | None = None
        self.periodic = QTimer(self)
        self.periodic.setSingleShot(True)
        self.periodic.timeout.connect(self.request_full)
        self._closed = False
        self.restart_periodic_timer()

    # Requests -----------------------------------------------------------------

    def request_full(self) -> None:
        self.full_scan_pending = True
        self.run_pending()

    def request_folder(self, folder: Path) -> None:
        self.mark_folder_dirty(folder)
        self.run_pending()

    def mark_folder_dirty(self, folder: Path) -> None:
        """Queue a folder repair; call `run_pending` after a batch of marks."""
        self.dirty_folders.add(folder)

    def mark_file_dirty(self, path: Path) -> None:
        self.dirty_files.add(path)

    def restart(self) -> None:
        """Settings changed: drop queued repairs, cancel the current job, rescan everything."""
        self.dirty_folders.clear()
        self.dirty_files.clear()
        if self.running is not None:
            self.running.cancel.set()
        self.request_full()

    def restart_periodic_timer(self) -> None:
        self.periodic.start(self.settings().rescan_seconds * MILLISECONDS_PER_SECOND)

    def close(self) -> None:
        self._closed = True
        self.periodic.stop()
        if self.running is not None:
            self.running.cancel.set()

    # Scheduling ---------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.running is not None

    def run_pending(self) -> None:
        if self._closed or self.busy:
            return
        if self.full_scan_pending and not self.full_scan_deferred():
            self._start(IndexJob(IndexJobKind.FULL))
        elif self.dirty_folders:
            self._start(self._take_folder_job())
        elif self.dirty_files:
            self._start(self._take_files_job())

    def _take_folder_job(self) -> IndexJob:
        """The shortest dirty path contains no other; repairing it covers its descendants."""
        folder = min(self.dirty_folders, key=lambda path: len(str(path)))
        self.dirty_folders = {path for path in self.dirty_folders if not is_within(path, folder)}
        self.dirty_files = {path for path in self.dirty_files if not is_within(path, folder)}
        return IndexJob(IndexJobKind.FOLDER, folder=folder)

    def _take_files_job(self) -> IndexJob:
        files = tuple(sorted(self.dirty_files))[:FILE_REPAIR_BATCH_SIZE]
        self.dirty_files.difference_update(files)
        return IndexJob(IndexJobKind.FILES, files=files)

    def _start(self, job: IndexJob) -> None:
        self.running = job
        if job.kind == IndexJobKind.FULL:
            self.full_scan_pending = False
            self.periodic.stop()
        self.started.emit(job)
        work = partial(self._work, job, self.settings())
        self.tasks.submit(
            TaskCategory.INDEX,
            work,
            partial(self._job_completed, job),
            partial(self._job_failed, job),
        )

    # Worker thread ------------------------------------------------------------

    def _work(self, job: IndexJob, settings: Settings) -> tuple[ScanReport, int]:
        full = job.kind == IndexJobKind.FULL
        if full and settings.index_apps and (folder := store_app_folder()) is not None:
            sync_store_apps(folder)  # Before the roots are listed: it may create the folder.
        roots = application_roots() if settings.index_apps else ()
        if job.kind == IndexJobKind.FILES:
            report = self.indexer.refresh_files(
                settings, job.files, application_roots=roots, cancel=job.cancel
            )
        else:
            report = self.indexer.scan(
                settings,
                scope=job.folder,
                application_roots=roots,
                applications=registered_applications() if full and settings.index_apps else (),
                cancel=job.cancel,
                progress=self.progress.emit,
            )
        return report, self.store.count()

    # Results ------------------------------------------------------------------

    def _job_completed(self, job: IndexJob, result: tuple[ScanReport, int]) -> None:
        if job is not self.running:
            return
        report, self.catalog_count = result
        self.last_report = report
        self._job_ended(job)
        self.completed.emit(report)
        self.run_pending()

    def _job_failed(self, job: IndexJob, message: str) -> None:
        if job is not self.running:
            return
        self._job_ended(job)
        self.failed.emit(message)
        self.run_pending()

    def _job_ended(self, job: IndexJob) -> None:
        self.running = None
        if job.kind == IndexJobKind.FULL:
            self.restart_periodic_timer()
