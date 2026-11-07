"""Who blocks the GUI thread: a watchdog that logs its stack in the middle of a stall.

The controller already warns when its 100 ms timer fired late, but by then the
culprit has returned. A thread of its own watches the heartbeat the GUI thread
leaves and, once it is older than the threshold, reads the GUI thread's current
Python stack and logs it. Native work, such as Qt painting, shows as the event
loop with no Python frame above it, which is a finding too.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback

STALL_MS = 250  # As the controller's own warning.
PAUSED_AFTER_S = 2.0  # No heartbeat this long: the timer is stopped, not stalled.
POLL_S = 0.1
STACK_FRAMES = 14
logger = logging.getLogger(__name__)


class StallTracer:
    """Call `heartbeat` from the GUI thread regularly; stalls are logged from `_run`."""

    def __init__(self, thread_id: int | None = None, stall_ms: int = STALL_MS):
        self.thread_id = thread_id or threading.main_thread().ident
        self.stall_s = stall_ms / 1000
        self._beat = 0.0
        self._reported = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stall-tracer", daemon=True)

    def start(self) -> None:
        self._beat = time.monotonic()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def heartbeat(self) -> None:
        self._beat = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(POLL_S):
            age = time.monotonic() - self._beat
            if age < self.stall_s:
                self._reported = False
            elif age < PAUSED_AFTER_S and not self._reported:
                self._reported = True  # One stack per stall.
                logger.warning(
                    "UI stalled for %.0f ms so far; GUI thread at:\n%s", age * 1000, self._stack()
                )

    def _stack(self) -> str:
        frame = sys._current_frames().get(self.thread_id)
        if frame is None:
            return "  (no Python frame: inside Qt or other native code)"
        lines = traceback.format_stack(frame, limit=STACK_FRAMES)
        return "".join(lines).rstrip() or "  (no Python frame: inside Qt or other native code)"
