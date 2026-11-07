"""Record native crashes, which Python logging never gets to see.

A fault in a DLL or C extension, such as a package a plugin brought along, ends the
process at once: no exception, no `except`, no log line. `faulthandler` writes the
stacks of all threads into an already open file from inside the fault instead.

Windows also reports exceptions that are handled later, such as a shell extension's
COM error, so a run may write here and live on. A clean exit therefore removes the
file; what the next start finds belongs to a run that never exited cleanly, and
goes into the log, where it is kept and rotated with everything else. No Qt here.
"""

from __future__ import annotations

import faulthandler
import logging
from datetime import datetime
from pathlib import Path
from typing import TextIO

CRASH_FILE = "crash.log"
MAX_REPORT_BYTES = 64_000  # The last fault is at the end; older handled ones matter less.
logger = logging.getLogger(__name__)


class CrashRecorder:
    """Owns the crash file of one data directory; only the instance owner may use it."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / CRASH_FILE
        self._file: TextIO | None = None

    def start(self) -> None:
        """Log what the previous run left behind, then record this run's faults."""
        try:
            self._report_previous_run()
            self._file = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            logger.warning("Native crashes will not be recorded: %s", exc)
            return
        faulthandler.enable(file=self._file, all_threads=True)

    def stop(self) -> None:
        """A clean exit: whatever this run recorded, it survived."""
        if self._file is None:
            return
        faulthandler.disable()
        self._file.close()
        self._file = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", self.path, exc)

    def _report_previous_run(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if not data.strip():
            return
        written = datetime.fromtimestamp(self.path.stat().st_mtime)
        logger.error(
            "The previous run ended without a clean exit; native faults it recorded, "
            "the last one at %s (a fault Windows handled may be listed too):\n%s",
            written.isoformat(sep=" ", timespec="seconds"),
            data[-MAX_REPORT_BYTES:].decode("utf-8", errors="replace").strip(),
        )
