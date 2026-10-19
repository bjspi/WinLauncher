"""Debounced, cancellable launcher searches and the "Enter opens" contract.

Enter must open a result of the query the user currently sees typed. If that
query's results are not displayed yet, opening waits for them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial

from PySide6.QtCore import QObject, QTimer, Signal

from pathscout.search.query import SearchQuery
from pathscout.search.query_parser import parse_query
from pathscout.search.ranking import SearchHit
from pathscout.search.service import SearchService
from pathscout.settings import Settings
from pathscout.ui.tasks import BackgroundTasks, TaskCategory
from pathscout.ui.window import LauncherWindow

DEBOUNCE_MS = 70
logger = logging.getLogger(__name__)


class SearchSession(QObject):
    open_requested = Signal(object, str)  # SearchHit, history key of its query

    def __init__(
        self,
        window: LauncherWindow,
        service: SearchService,
        tasks: BackgroundTasks,
        settings: Callable[[], Settings],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.window, self.service, self.tasks, self.settings = window, service, tasks, settings
        self.running: threading.Event | None = None  # Cancel flag of the search in flight.
        self.displayed_query: SearchQuery | None = None
        self.open_pending = False
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(DEBOUNCE_MS)
        self.debounce.timeout.connect(self.run)

    def current_query(self) -> SearchQuery:
        """The query as typed, with the filter buttons applied unless `type:` was typed."""
        query = parse_query(self.window.query.text(), self.window.scope)
        if self.window.explorer_mode and query.scope == self.window.scope:
            query = replace(query, explorer=True)
        if query.kind is None:
            query = replace(query, kind=self.window.kind)
        self.window.show_kind(query.kind)
        return query

    def text_changed(self) -> None:
        self.open_pending = False
        self._abandon_running()
        if self._shows_other_query():
            self.window.show_searching()
        self.debounce.start()

    def run(self) -> None:
        self.debounce.stop()
        self._abandon_running()
        try:
            query = self.current_query()
        except ValueError as exc:
            self.open_pending = False
            self.window.show_query_error(str(exc))
            return
        cancel = self.running = threading.Event()
        self.tasks.submit(
            TaskCategory.SEARCH,
            partial(self._work, query, self.settings(), cancel),
            partial(self._completed, cancel, query),
            partial(self._failed, cancel),
        )

    def refresh(self) -> None:
        """The catalog or personal signals changed; hidden windows search on next show."""
        if self.window.isVisible():
            self.run()
        else:
            self.displayed_query = None

    def request_open(self, hit: SearchHit | None) -> None:
        try:
            query = self.current_query()
        except ValueError:
            return
        if self._must_wait_for_results(hit, query):
            self.open_pending = True
            self.run()
            return
        if hit is not None:
            self.open_requested.emit(hit, query.history_key)

    def _must_wait_for_results(self, hit: SearchHit | None, query: SearchQuery) -> bool:
        """Enter refers to the typed query; its results may not be displayed yet."""
        if self.displayed_query != query:
            return True
        return hit is None and self.running is not None

    def window_hidden(self) -> None:
        self.open_pending = False
        self.debounce.stop()
        self._abandon_running()

    def close(self) -> None:
        self.debounce.stop()
        self._abandon_running()

    def _shows_other_query(self) -> bool:
        try:
            return self.current_query() != self.displayed_query
        except ValueError:
            return True

    def _abandon_running(self) -> None:
        if self.running is not None:
            self.running.set()
            self.running = None

    def _work(
        self, query: SearchQuery, settings: Settings, cancel: threading.Event
    ) -> tuple[list[SearchHit], float]:
        started = time.perf_counter()
        hits = self.service.search(query, settings, cancel=cancel)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "Search finished: requested_backend=%s elapsed_ms=%.1f hits=%d cancelled=%s",
            settings.search_backend,
            elapsed_ms,
            len(hits),
            cancel.is_set(),
        )
        return hits, elapsed_ms

    def _completed(
        self, cancel: threading.Event, query: SearchQuery, result: tuple[list[SearchHit], float]
    ) -> None:
        if cancel is not self.running:
            return
        self.running = None
        if not self.window.isVisible():
            self.displayed_query = None
            return
        same_query = self.displayed_query == query
        self.displayed_query = query
        hits, elapsed_ms = result
        self.window.set_results(hits, elapsed_ms, preserve_selection=same_query, query=query)
        if self.open_pending:
            self.open_pending = False
            self.request_open(self.window.selected_hit())

    def _failed(self, cancel: threading.Event, message: str) -> None:
        if cancel is not self.running:
            return
        self.running = None
        self.displayed_query = None
        self.open_pending = False
        self.window.show_search_error(message)
