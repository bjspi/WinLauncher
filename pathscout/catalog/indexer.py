"""Cancellable, bounded-memory filesystem scans with atomic publication.

Responsibilities are split along the life of one scan:

- `_EntryRules` decides whether an entry belongs in the catalog (policy).
- `_ScanSession` walks directories and applies the rules (acquisition).
- `_StagedRows` writes accepted items into the private snapshot (persistence).
- `_ScanOutcome` records skips, failures and old rows that must survive (reporting).
- `CatalogIndexer` decides what a completed scan replaces (publication).

A cancelled scan publishes nothing. A path that cannot be read keeps its previous
snapshot while readable siblings are still refreshed.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from pathscout.catalog import long_paths
from pathscout.catalog.model import (
    REGISTRY_SOURCE,
    CatalogItem,
    CatalogSource,
    ItemKind,
    ScanReport,
    is_within,
    path_key,
)
from pathscout.catalog.priority import PriorityPolicy, SearchPriority
from pathscout.catalog.sources import catalog_sources, owning_source
from pathscout.catalog.store import CatalogStore
from pathscout.settings import Settings

APP_SUFFIXES = frozenset({".exe", ".lnk", ".url", ".appref-ms"})
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # Junctions.
IO_REPARSE_TAG_SYMLINK = 0xA000000C
FILE_ATTRIBUTE_OFFLINE = 0x1000  # Cloud placeholders; reading them may download content.
SCAN_BATCH_SIZE = 500

ProgressCallback = Callable[[int], None]


def _is_link_or_offline(info: os.stat_result, directory: bool) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_reparse_tag", 0) in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK):
        return True
    return directory and bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_OFFLINE)


def classify_kind(path: Path, directory: bool) -> ItemKind:
    if directory:
        return ItemKind.FOLDER
    if path.suffix.lower() in APP_SUFFIXES:
        return ItemKind.APP
    return ItemKind.FILE


def catalog_item(path: Path, info: os.stat_result, kind: ItemKind) -> CatalogItem:
    attributes = getattr(info, "st_file_attributes", 0)
    return CatalogItem(path, kind, info.st_size, info.st_mtime, attributes=attributes)


def _folders_up_to(folder: Path, root: Path) -> Iterator[Path]:
    """`folder` and each parent up to and including `root`; nothing if outside `root`."""
    while is_within(folder, root):
        yield folder
        if folder == root:
            return
        folder = folder.parent


class _EntryRules:
    """Whether an entry belongs in the catalog. Disk errors propagate as OSError."""

    def __init__(self, settings: Settings):
        self.exclusions = settings.exclusions(raise_marker_errors=True)
        self.priorities: PriorityPolicy = settings.priorities()

    def entry_item(
        self, path: Path, info: os.stat_result, directory: bool, *, application: bool = False
    ) -> CatalogItem | None:
        if _is_link_or_offline(info, directory):
            return None
        if not self.exclusions.allows(path, directory=directory, application=application):
            return None
        return self._unless_ignored(catalog_item(path, info, classify_kind(path, directory)))

    def application_item(self, path: Path) -> CatalogItem | None:
        try:
            info = long_paths.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or not self.exclusions.allows(path, application=True):
            return None
        return self._unless_ignored(catalog_item(path, info, ItemKind.APP))

    def allows_scope(self, folder: Path, source_root: Path) -> bool:
        """A scoped scan inherits every pruning decision of the folders above it.

        The source root may be a junction the user chose explicitly; folders below
        it are never followed through links.
        """
        return all(
            self._allows_folder(ancestor, explicit_root=ancestor == source_root)
            for ancestor in _folders_up_to(folder, source_root)
        )

    def _allows_folder(self, folder: Path, *, explicit_root: bool) -> bool:
        info = long_paths.stat(folder) if explicit_root else long_paths.lstat(folder)
        if not stat.S_ISDIR(info.st_mode):
            return False
        if not explicit_root and _is_link_or_offline(info, directory=True):
            return False
        if not self.exclusions.allows(folder, directory=True):
            return False
        return self._unless_ignored(catalog_item(folder, info, ItemKind.FOLDER)) is not None

    def _unless_ignored(self, item: CatalogItem) -> CatalogItem | None:
        if self.priorities.evaluate(item).search == SearchPriority.IGNORED:
            return None
        return item


class _StagedRows:
    """Batches accepted items into one snapshot connection."""

    def __init__(self, store: CatalogStore, connection, progress: ProgressCallback | None):
        self.store, self.connection, self.progress = store, connection, progress
        self.count = 0
        self._batch: list[tuple[CatalogItem, str]] = []

    def add(self, item: CatalogItem, source_id: str) -> None:
        self._batch.append((item, source_id))
        self.count += 1
        if len(self._batch) < SCAN_BATCH_SIZE:
            return
        self.flush()
        if self.progress:
            self.progress(self.count)

    def flush(self) -> None:
        self.store.stage(self.connection, self._batch)
        self._batch.clear()

    def retain_previous(self, paths: Iterable[Path]) -> None:
        """Copy the previous rows of unreadable or untouched subtrees into the snapshot."""
        for path in paths:
            self.store.preserve_subtree(self.connection, path)


class _ScanOutcome:
    """What a scan skipped, what failed, and which previous rows must survive."""

    def __init__(self):
        self.started = time.monotonic()
        self.skipped = 0
        self.errors: list[str] = []
        self.retained_paths: set[Path] = set()
        self.unavailable_sources: set[str] = set()

    def skip(self) -> None:
        self.skipped += 1

    def retain(self, path: Path) -> None:
        self.retained_paths.add(path)

    def failure(self, path: Path, action: str, error: OSError) -> None:
        self.errors.append(f"{action}: {path}: {error}")
        self.retain(path)

    def source_unavailable(self, source: CatalogSource, root: Path, error: OSError) -> None:
        self.unavailable_sources.add(source.id)
        self.failure(root, "Search root unavailable", error)

    def report(self, entries: int, *, cancelled: bool) -> ScanReport:
        elapsed = time.monotonic() - self.started
        return ScanReport(entries, self.skipped, tuple(self.errors), elapsed, cancelled)


class _ScanSession:
    """Walks directories, applies the entry rules, and stages accepted items.

    `nested_source_keys` names configured sources that a broad Explorer scope may
    contain. They keep their own catalog rows and indexing mode.
    """

    def __init__(
        self,
        settings: Settings,
        rows: _StagedRows,
        cancel: threading.Event,
        nested_source_keys: frozenset[str] = frozenset(),
    ):
        self.rules = _EntryRules(settings)
        self.rows = rows
        self.outcome = _ScanOutcome()
        self.cancel = cancel
        self.nested_source_keys = nested_source_keys

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def report(self, *, cancelled: bool) -> ScanReport:
        return self.outcome.report(self.rows.count, cancelled=cancelled)

    def finish_staging(self) -> None:
        """Write the last partial batch, then keep old rows the scan could not replace."""
        self.rows.flush()
        self.rows.retain_previous(self.outcome.retained_paths)

    def scan_tree(self, root: Path, source: CatalogSource) -> None:
        if self.cancelled or not self._enter_root(root, source):
            return
        pending = [root]
        while pending and not self.cancelled:
            pending.extend(self._scan_folder(pending.pop(), source))

    def scan_applications(self, paths: Iterable[Path]) -> None:
        for path in paths:
            if self.cancelled:
                return
            try:
                item = self.rules.application_item(path)
            except OSError as exc:
                self.outcome.failure(path, "Cannot read application", exc)
                continue
            self._stage_or_skip(item, REGISTRY_SOURCE)

    def refresh_file(self, path: Path, source: CatalogSource) -> None:
        try:
            item = self._file_item(path, source)
        except FileNotFoundError:
            return  # Deleted files disappear when the file set is published.
        except OSError as exc:
            self.outcome.failure(path, "Cannot refresh", exc)
            return
        self._stage_or_skip(item, source.id)

    def _enter_root(self, root: Path, source: CatalogSource) -> bool:
        try:
            if not self.rules.allows_scope(root, source.root):
                self.outcome.skip()
                return False
            if root != source.root and not source.apps_only:
                # A scoped root is itself an entry of its parent folder.
                info = long_paths.stat(root)
                self.rows.add(catalog_item(root, info, ItemKind.FOLDER), source.id)
        except OSError as exc:
            self.outcome.source_unavailable(source, root, exc)
            return False
        return True

    def _scan_folder(self, folder: Path, source: CatalogSource) -> list[Path]:
        """Stage the folder's entries and return the subfolders still to visit.

        Entries stream from the directory, so memory stays bounded and a listing
        error keeps everything already staged (the folder's old rows are retained).
        """
        subfolders = []
        try:
            with long_paths.scandir(folder) as entries:
                for entry in entries:
                    if self.cancelled:
                        break
                    subfolder = self._visit(entry, source)
                    if subfolder is not None:
                        subfolders.append(subfolder)
        except OSError as exc:
            self.outcome.failure(folder, "Cannot scan", exc)
        return subfolders

    def _visit(self, entry: os.DirEntry, source: CatalogSource) -> Path | None:
        path = long_paths.ordinary_path(entry.path)
        if path_key(path) in self.nested_source_keys:
            self._retain_nested_source(path)
            return None
        try:
            directory = entry.is_dir(follow_symlinks=False)
            info = entry.stat(follow_symlinks=False)
            item = self.rules.entry_item(path, info, directory, application=source.apps_only)
        except OSError as exc:
            self.outcome.failure(path, "Cannot read", exc)
            return None
        if item is None:
            self.outcome.skip()
            return None
        if source.accepts(item):
            self.rows.add(item, source.id)
        return path if directory else None

    def _retain_nested_source(self, root: Path) -> None:
        self.outcome.skip()
        try:
            allowed = self.rules.allows_scope(root, root)
        except OSError as exc:
            self.outcome.failure(root, "Cannot read", exc)
            return
        if allowed:
            self.outcome.retain(root)

    def _file_item(self, path: Path, source: CatalogSource) -> CatalogItem | None:
        if not self.rules.allows_scope(path.parent, source.root):
            return None
        info = long_paths.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        item = self.rules.entry_item(path, info, directory=False, application=source.apps_only)
        if item is None or not source.accepts(item):
            return None
        return item

    def _stage_or_skip(self, item: CatalogItem | None, source_id: str) -> None:
        if item is None:
            self.outcome.skip()
        else:
            self.rows.add(item, source_id)


class CatalogIndexer:
    def __init__(self, store: CatalogStore):
        self.store = store

    def scan(
        self,
        settings: Settings,
        *,
        application_roots: Iterable[Path] = (),
        applications: Iterable[Path] = (),
        scope: Path | None = None,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> ScanReport:
        """Rescan every source, or only `scope` when an Explorer folder needs repair."""
        cancel = cancel or threading.Event()
        sources = catalog_sources(settings, application_roots)
        if scope is None:
            return self._scan_all(settings, sources, applications, cancel, progress)
        return self._scan_scope(settings, scope, sources, cancel, progress)

    def refresh_files(
        self,
        settings: Settings,
        paths: tuple[Path, ...],
        *,
        application_roots: Iterable[Path] = (),
        cancel: threading.Event | None = None,
    ) -> ScanReport:
        """Refresh changed file metadata without traversing neighboring directories."""
        cancel = cancel or threading.Event()
        sources = catalog_sources(settings, application_roots)
        with self.store.snapshot() as connection:
            session = _ScanSession(settings, _StagedRows(self.store, connection, None), cancel)
            for path in paths:
                if session.cancelled:
                    break
                session.refresh_file(path, owning_source(path.parent, sources))
            if session.cancelled:
                return session.report(cancelled=True)
            session.finish_staging()
            self.store.publish_files(connection, paths)
        return session.report(cancelled=False)

    def _scan_all(
        self,
        settings: Settings,
        sources: list[CatalogSource],
        applications: Iterable[Path],
        cancel: threading.Event,
        progress: ProgressCallback | None,
    ) -> ScanReport:
        with self.store.snapshot() as connection:
            session = _ScanSession(settings, _StagedRows(self.store, connection, progress), cancel)
            for source in sources:
                session.scan_tree(source.root, source)
            active = {source.id for source in sources}
            if settings.index_apps:
                session.scan_applications(applications)
                active.add(REGISTRY_SOURCE)
            if session.cancelled:
                return session.report(cancelled=True)  # The previous catalog stays untouched.
            session.finish_staging()
            # Unavailable sources keep their previous snapshot instead of being emptied.
            unavailable = session.outcome.unavailable_sources
            self.store.publish(
                connection, active_roots=active - unavailable, preserved_roots=unavailable
            )
        return session.report(cancelled=False)

    def _scan_scope(
        self,
        settings: Settings,
        scope: Path,
        sources: list[CatalogSource],
        cancel: threading.Event,
        progress: ProgressCallback | None,
    ) -> ScanReport:
        nested_source_keys = frozenset(path_key(source.root) for source in sources)
        with self.store.snapshot() as connection:
            rows = _StagedRows(self.store, connection, progress)
            session = _ScanSession(settings, rows, cancel, nested_source_keys)
            session.scan_tree(scope, owning_source(scope, sources))
            if session.cancelled:
                return session.report(cancelled=True)
            session.finish_staging()
            self.store.publish_subtree(connection, scope)
        return session.report(cancelled=False)
