"""Python's cycle collector, run on the GUI thread only.

Qt destroys an object safely only on the thread it lives on, for widgets and timers
the GUI thread. When the Python wrapper of such an object sits in a reference cycle,
the cycle collector frees it, and Python runs that collector on whichever thread
happens to allocate at that moment: a search worker, a plugin refresh, the stall
tracer. A widget destroyed there leaves Qt with dangling pointers, and the process
crashes later at a random spot, typically in the next event loop pass.

So automatic collection is switched off, and a timer on the GUI thread collects
with the thresholds Python would have used itself.
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

CHECK_INTERVAL_MS = 500  # Often enough that garbage never piles up noticeably.


class GuiThreadCollector(QObject):
    """Owns cycle collection while it exists; create it once, on the GUI thread."""

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.thresholds = gc.get_threshold()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.collect_due)
        self._timer.start()

    def collect_due(self) -> None:
        """Collect each generation whose count passed its threshold, youngest first,
        as Python's automatic collection does."""
        counts = gc.get_count()
        for generation, (count, threshold) in enumerate(zip(counts, self.thresholds, strict=True)):
            if threshold <= 0 or count <= threshold:
                return
            gc.collect(generation)
