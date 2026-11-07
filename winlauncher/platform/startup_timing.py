"""Tell where a slow start went: one log line with the time of each startup phase.

The first phase ends before any logging exists: it reaches back to the process's
creation and so covers Python's start and all imports, which a cold disk makes slow.
The uptime tells that apart from Windows starting WinLauncher late after a boot.
"""

from __future__ import annotations

import logging
import time

PROCESS_PHASE = "imports"
LAST_PHASE = "first_idle"  # Until the event loop finished the work queued at startup.
MILLISECONDS_PER_SECOND = 1000
logger = logging.getLogger(__name__)


def _process_age_seconds() -> float:
    import win32api
    import win32process

    created = win32process.GetProcessTimes(win32api.GetCurrentProcess())["CreationTime"]
    return max(0.0, time.time() - created.timestamp())


def _system_uptime_seconds() -> float:
    import win32api

    return win32api.GetTickCount() / MILLISECONDS_PER_SECOND


class StartupTimings:
    """Created first thing in `main`; each `mark` ends the phase with that name."""

    def __init__(self) -> None:
        self._phases = {PROCESS_PHASE: _process_age_seconds()}
        self._phase_started = time.perf_counter()

    def mark(self, phase: str) -> None:
        now = time.perf_counter()
        self._phases[phase] = now - self._phase_started
        self._phase_started = now

    def report(self) -> None:
        self.mark(LAST_PHASE)
        phases = " ".join(
            f"{phase}={seconds * MILLISECONDS_PER_SECOND:.0f}ms"
            for phase, seconds in self._phases.items()
        )
        logger.info(
            "Startup timings: %s total=%.0fms uptime=%.0fs",
            phases,
            sum(self._phases.values()) * MILLISECONDS_PER_SECOND,
            _system_uptime_seconds(),
        )
