"""Keep plugins that search too slowly out of ordinary searches.

Plugin items are ranked in Python on a search worker, and Python runs one thread
at a time: while a big plugin ranks, the search bar can hardly paint. A plugin
whose ranking takes longer than the user's time limit in `SLOW_SEARCHES_IN_A_ROW`
ordinary searches in a row is therefore left out of them until its items change,
such as at its next refresh. Its keyword still searches it, because then the user
asks for that plugin alone.

Thread-safe and free of Qt: searches record on the search workers, the Settings
page reads on the GUI thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

SLOW_SEARCHES_IN_A_ROW = 3  # One slow search, such as for a single letter, is no pattern.
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchRecord:
    """How a plugin's searches went lately, for the log and its settings card."""

    last_ms: float | None = None  # The last ranking, ordinary or by keyword.
    slow_in_a_row: int = 0  # Ordinary searches over the limit, the last ones in a row.
    left_out: bool = False  # Out of ordinary searches until its items change.


class SearchBudget:
    def __init__(self) -> None:
        self._records: dict[str, SearchRecord] = {}
        self._guard = threading.Lock()

    def record(self, plugin_id: str) -> SearchRecord:
        with self._guard:
            return self._records.get(plugin_id, SearchRecord())

    def allows(self, plugin_id: str, limit_ms: int) -> bool:
        """Whether an ordinary search ranks the plugin; a limit of 0 allows every plugin."""
        return limit_ms == 0 or not self.record(plugin_id).left_out

    def note(self, plugin_id: str, elapsed_ms: float, limit_ms: int, *, ordinary: bool) -> None:
        """Count one finished ranking; only ordinary searches can leave a plugin out."""
        with self._guard:
            record = self._records.get(plugin_id, SearchRecord())
            slow = ordinary and limit_ms > 0 and elapsed_ms > limit_ms
            if ordinary:
                in_a_row = record.slow_in_a_row + 1 if slow else 0
            else:
                in_a_row = record.slow_in_a_row
            left_out = record.left_out or in_a_row >= SLOW_SEARCHES_IN_A_ROW
            self._records[plugin_id] = SearchRecord(elapsed_ms, in_a_row, left_out)
        if left_out and not record.left_out:
            logger.warning(
                "Plugin %s left out of ordinary searches: %d in a row took longer than "
                "%d ms, the last %.0f ms. Its keywords still search it; it is tried again "
                "once its items change.",
                plugin_id,
                in_a_row,
                limit_ms,
                elapsed_ms,
            )

    def forget(self, plugin_id: str) -> None:
        """The plugin's items changed: its next searches are measured afresh."""
        with self._guard:
            record = self._records.pop(plugin_id, None)
        if record is not None and record.left_out:
            logger.info("Plugin %s is tried in ordinary searches again: new items.", plugin_id)
