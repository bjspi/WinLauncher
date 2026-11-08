"""Whether a path's data can be reached now: drives that may not answer, cloud files.

A dead server, a sleeping NAS or a mapped drive without VPN blocks the first
filesystem call on it for about 20 seconds before Windows reports "network path
not found" (which Python raises as `FileNotFoundError`). `drive_state` asks the
drive or share root on a daemon thread and waits at most `DRIVE_PROBE_TIMEOUT_S`,
so no caller, least of all the GUI thread, waits for a dead server.

Cloud placeholders (OneDrive, Dropbox and others with "files on demand") list
like ordinary files, but reading one downloads it. `is_online_only` tells them
apart from their metadata, which never triggers a download.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from winlauncher.catalog.model import path_key

DRIVE_PROBE_TIMEOUT_S = 2.0
FILE_ATTRIBUTE_OFFLINE = 0x0000_1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x0004_0000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x0040_0000
# Attributes of a file whose content is not on this computer; reading it downloads it.
ONLINE_ONLY_ATTRIBUTES = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


class DriveState(StrEnum):
    """What a drive or share root answered within the probe timeout."""

    PRESENT = "present"
    MISSING = "missing"
    UNRESPONSIVE = "unresponsive"


class DriveUnavailable(OSError):
    """The drive or share of a path is not connected or does not answer."""


@dataclass(slots=True)
class _Probe:
    """One running root check; every waiter for the same root shares it."""

    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    exists: bool = False


class DriveProbe:
    """Checks drive and share roots on daemon threads with a bounded wait.

    At most one check per root runs at a time, so repeated questions about a dead
    server leave one hanging thread, not one per question. Every waiter waits until
    that check's deadline, not a fresh timeout: once a root has failed to answer
    in time, later questions get "unresponsive" at once until the check returns.
    Results are not cached, so a share that comes back answers the next question.
    """

    def __init__(self, timeout_s: float, exists: Callable[[str], bool] = os.path.isdir) -> None:
        self._timeout_s = timeout_s
        self._exists = exists
        self._lock = threading.Lock()
        self._running: dict[str, _Probe] = {}

    def state(self, path: Path) -> DriveState:
        probe = self._probe(path.anchor)
        if not probe.done.wait(max(0.0, probe.deadline - time.monotonic())):
            return DriveState.UNRESPONSIVE
        return DriveState.PRESENT if probe.exists else DriveState.MISSING

    def _probe(self, root: str) -> _Probe:
        key = path_key(root)
        with self._lock:
            if (probe := self._running.get(key)) is None:
                probe = self._running[key] = _Probe(time.monotonic() + self._timeout_s)
                threading.Thread(
                    target=self._check, args=(root, key, probe), name="drive_probe", daemon=True
                ).start()
        return probe

    def _check(self, root: str, key: str, probe: _Probe) -> None:
        try:
            probe.exists = self._exists(root)
        finally:
            with self._lock:
                del self._running[key]
            probe.done.set()


_DRIVES = DriveProbe(DRIVE_PROBE_TIMEOUT_S)


def drive_state(path: Path) -> DriveState:
    """Whether the drive or share holding `path` answers; waits at most a moment."""
    return _DRIVES.state(path)


def require_drive(path: Path) -> None:
    """Raise `DriveUnavailable` with a user-facing message unless `path`'s drive answers."""
    if (state := drive_state(path)) is not DriveState.PRESENT:
        raise DriveUnavailable(unavailable_message(path, state))


def unavailable_message(path: Path, state: DriveState) -> str:
    """Why `path` cannot be reached, for a drive that is not `PRESENT`."""
    if state is DriveState.UNRESPONSIVE:
        return f"{path.anchor} does not respond. Check the network or drive connection."
    return f"{path.anchor} is not connected."


def is_online_only(info: os.stat_result) -> bool:
    """Whether the file's content lives only in the cloud, by its stat attributes."""
    return bool(getattr(info, "st_file_attributes", 0) & ONLINE_ONLY_ATTRIBUTES)
