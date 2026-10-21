"""ReadDirectoryChangesW adapter that reports which catalog paths need repair.

Ownership: each watcher thread owns one directory handle, one event and one
read buffer. An overlapped read may only release them after the kernel has
completed or acknowledged cancellation of that read (`_pending_read`).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pywintypes
import win32con
import win32event
import win32file
import winerror

from winlauncher.catalog import long_paths
from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.model import is_within, normalize
from winlauncher.platform.changes import (
    FileChange,
    FolderChange,
    WatchEvent,
    WatchFailed,
    WatchRestored,
)

logger = logging.getLogger(__name__)

FILE_LIST_DIRECTORY = 0x0001
FILE_ACTION_ADDED = 1
FILE_ACTION_MODIFIED = 3
NOTIFY_FILTER = (
    win32con.FILE_NOTIFY_CHANGE_FILE_NAME
    | win32con.FILE_NOTIFY_CHANGE_DIR_NAME
    | win32con.FILE_NOTIFY_CHANGE_SIZE
    | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE
    | win32con.FILE_NOTIFY_CHANGE_ATTRIBUTES
)
NOTIFY_BUFFER_BYTES = 64 * 1024
STOP_POLL_MS = 200
RETRY_SECONDS = 30


class _Overflow(Exception):
    """The kernel dropped notifications; only a full root repair is safe."""


@contextmanager
def _directory_handle(root: Path) -> Iterator[pywintypes.HANDLE]:
    handle = win32file.CreateFile(
        str(root),
        FILE_LIST_DIRECTORY,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OVERLAPPED,
        None,
    )
    try:
        yield handle
    finally:
        handle.Close()


@contextmanager
def _manual_reset_event() -> Iterator[pywintypes.HANDLE]:
    event = win32event.CreateEvent(None, True, False, None)
    try:
        yield event
    finally:
        event.Close()


class _PendingRead:
    """One overlapped ReadDirectoryChangesW call.

    Leaving the block before `collect` was called cancels the read and
    waits for the kernel's acknowledgement, so the buffer and OVERLAPPED stay
    alive for as long as the kernel may write to them.
    """

    def __init__(self, handle, event, buffer):
        self.handle, self.event, self.buffer = handle, event, buffer
        self.overlapped = pywintypes.OVERLAPPED()
        self.overlapped.hEvent = event
        self._collected = False

    def __enter__(self) -> _PendingRead:
        win32event.ResetEvent(self.event)
        win32file.ReadDirectoryChangesW(
            self.handle, self.buffer, True, NOTIFY_FILTER, self.overlapped, None
        )
        return self

    def collect(self) -> int:
        """Take ownership of a completed read and return its byte count.

        Raises `_Overflow` if the kernel dropped notifications.
        """
        self._collected = True
        try:
            count = win32file.GetOverlappedResult(self.handle, self.overlapped, False)
        except pywintypes.error as exc:
            if exc.winerror == winerror.ERROR_NOTIFY_ENUM_DIR:
                raise _Overflow from None
            raise
        if count == 0:
            raise _Overflow
        return count

    def __exit__(self, *exc_info) -> None:
        if self._collected:
            return
        win32file.CancelIo(self.handle)
        try:
            win32file.GetOverlappedResult(self.handle, self.overlapped, True)
        except pywintypes.error as exc:
            if exc.winerror != winerror.ERROR_OPERATION_ABORTED:
                logger.debug("Cancelled folder watch read ended with %s", exc)


class DirectoryWatcher(threading.Thread):
    def __init__(
        self,
        root: Path,
        events: queue.SimpleQueue[WatchEvent],
        *,
        exclusions: ExclusionPolicy | None = None,
    ):
        super().__init__(name="watch-" + root.name, daemon=True)
        self.root, self.events = root, events
        self.exclusions = exclusions or ExclusionPolicy()
        self.markers = {normalize(marker) for marker in self.exclusions.markers}
        self.ready = threading.Event()
        self._stop_event = threading.Event()
        self._failed = False

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Watch until stopped; after a failure, report it and retry periodically."""
        while not self._stop_event.is_set():
            try:
                self._watch()
            except Exception as exc:
                self._report_failure(exc)
            self._stop_event.wait(RETRY_SECONDS)

    def _report_failure(self, error: Exception) -> None:
        """Report the transition to failed once; retries of a lost root stay quiet."""
        if self._stop_event.is_set() or self._failed:
            return
        logger.warning("Folder watch unavailable for %s: %s", self.root, error)
        self._failed = True
        self.events.put(WatchFailed(self.root, str(error)))

    def _watch(self) -> None:
        buffer = win32file.AllocateReadBuffer(NOTIFY_BUFFER_BYTES)
        with _directory_handle(self.root) as handle, _manual_reset_event() as event:
            while self._read_once(handle, event, buffer):
                pass

    def _read_once(self, handle, event, buffer) -> bool:
        """Publish one batch of changes; False once the watcher was stopped."""
        with _PendingRead(handle, event, buffer) as read:
            self._mark_armed()
            if not self._wait_for_completion(event):
                return False
            try:
                count = read.collect()
            except _Overflow:
                self.events.put(FolderChange(self.root))
                return True
        for change in self._classify_batch(win32file.FILE_NOTIFY_INFORMATION(buffer, count)):
            self.events.put(change)
        return True

    def _mark_armed(self) -> None:
        self.ready.set()
        if self._failed:
            logger.info("Folder watch restored for %s", self.root)
            self._failed = False
            self.events.put(WatchRestored(self.root))

    def _wait_for_completion(self, event) -> bool:
        """True when the read completed; False when the watcher was stopped first."""
        while not self._stop_event.is_set():
            result = win32event.WaitForSingleObject(event, STOP_POLL_MS)
            if result == win32event.WAIT_OBJECT_0:
                return True
            if result != win32event.WAIT_TIMEOUT:
                raise OSError(f"Folder watch wait failed: {result}")
        return False

    def _classify_batch(self, notifications: list[tuple[int, str]]) -> set[WatchEvent]:
        policy = self.exclusions.fresh()  # Marker files may have changed in this batch.
        changes: set[WatchEvent] = set()
        for action, relative in notifications:
            change = self._classify(action, self.root / relative, policy)
            if change is not None:
                changes.add(change)
        return changes

    def _classify(self, action: int, path: Path, policy: ExclusionPolicy) -> WatchEvent | None:
        if not is_within(path, self.root):
            return None
        if normalize(path.name) in self.markers:
            # A marker decides about its whole directory, even an already excluded one.
            return FolderChange(path.parent)
        is_file = os.path.isfile(long_paths.os_path(path))
        if action == FILE_ACTION_MODIFIED and not is_file:
            return None  # Directory metadata waits for periodic reconciliation.
        if not policy.allows(path):
            return None
        if is_file and action in (FILE_ACTION_ADDED, FILE_ACTION_MODIFIED):
            return FileChange(path)
        # Removals and renames change the parent's entries.
        return FolderChange(path.parent)


class ChangeMonitor:
    """One watcher per outermost root; nested roots are covered recursively."""

    def __init__(
        self,
        roots: tuple[Path, ...],
        events: queue.SimpleQueue[WatchEvent],
        *,
        exclusions: ExclusionPolicy | None = None,
    ):
        self.watchers = [
            DirectoryWatcher(root, events, exclusions=exclusions) for root in _outermost(roots)
        ]

    def start(self) -> None:
        for watcher in self.watchers:
            watcher.start()

    def stop(self) -> None:
        for watcher in self.watchers:
            watcher.stop()
        for watcher in self.watchers:
            watcher.join(timeout=1)


def _outermost(roots: tuple[Path, ...]) -> list[Path]:
    unique = list(dict.fromkeys(roots))
    return [
        root
        for root in unique
        if not any(other != root and is_within(root, other) for other in unique)
    ]
