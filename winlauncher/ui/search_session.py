"""Debounced, cancellable launcher searches and the "Enter opens" contract.

Enter must open a result of the query the user currently sees typed. If that
query's results are not displayed yet, opening waits for them.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TypeAlias

from PySide6.QtCore import QObject, QTimer, Signal

from winlauncher.catalog import long_paths
from winlauncher.plugins.host import PluginHost, PluginSearch
from winlauncher.plugins.search import PluginQuery, merge_hits, plugin_query
from winlauncher.search.native_engine import native_availability
from winlauncher.search.query import SearchQuery
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import SearchHit
from winlauncher.search.service import SearchService
from winlauncher.settings import Settings
from winlauncher.ui.result_rows import LauncherHit
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory
from winlauncher.ui.window import LauncherWindow
from winlauncher.websites import WebsiteQuery, website_query

DEBOUNCE_MS = 40
# Words only: no filters, exclusions, quotes, wildcards, paths or navigation.
_PLAIN_WORDS = re.compile(r"[^:!*?\[\]\"\\/%~]+")
# What the typed text asks for: a file search, a website plus its file search, or
# one plugin's items.
LauncherQuery: TypeAlias = SearchQuery | WebsiteQuery | PluginQuery
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: list[LauncherHit]
    elapsed_ms: float
    # Catalog hits whose file is gone; the catalog has not noticed yet.
    missing: frozenset[Path] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class StartList:
    """The results of the empty query: favorites and recently opened items."""

    query: SearchQuery
    results: SearchResults


class SearchSession(QObject):
    open_requested = Signal(object, str)  # LauncherHit, history key of its file query

    def __init__(
        self,
        window: LauncherWindow,
        service: SearchService,
        tasks: BackgroundTasks,
        settings: Callable[[], Settings],
        parent: QObject | None = None,
        plugins: PluginHost | None = None,
    ):
        super().__init__(parent)
        self.window, self.service, self.tasks, self.settings = window, service, tasks, settings
        self.plugins = plugins
        self.running: threading.Event | None = None  # Cancel flag of the search in flight.
        self.displayed_query: LauncherQuery | None = None
        self.open_pending = False
        # Kept ready while the bar is hidden, so it opens with its list at once.
        self.start_list: StartList | None = None
        self.prefetching: threading.Event | None = None  # Cancel flag of the prefetch.
        self.closed = False
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(DEBOUNCE_MS)
        self.debounce.timeout.connect(self.run)

    def current_query(self) -> LauncherQuery:
        """The query as typed, with the filter buttons applied unless `type:` was typed."""
        if self.window.scope is None and self.window.kind is None and not self.window.explorer_mode:
            text = self.window.query.text()
            if self.plugins is not None:
                keywords = self.plugins.keywords(self.settings().plugins)
                if (plugin := plugin_query(text, keywords)) is not None:
                    self.window.show_kind(None)
                    return plugin
            website = website_query(text, self.settings().websites)
            if website is not None:
                self.window.show_kind(None)
                return website
        query = parse_query(self.window.query.text(), self.window.scope)
        # The compact bar of a file dialog has no folder: it searches everywhere.
        scoped = self.window.scope is not None and query.scope == self.window.scope
        if self.window.explorer_mode and scoped:
            query = replace(query, explorer=True)
        if query.kind is None:
            query = replace(query, kind=self.window.kind)
        self.window.show_kind(query.kind)
        return query

    def text_changed(self) -> None:
        self.open_pending = False
        self._abandon_running()
        if (start := self._start_list_for_typed_query()) is not None:
            # Shown at once; a fresh search confirms it, invisibly when nothing changed.
            self._show(start.query, start.results)
            self.run()
            return
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
            _task_category(query),
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
            self.prefetch_start_list()

    def prefetch_start_list(self) -> None:
        """Recompute the start list in the background while the bar is hidden."""
        if self.closed or self.window.isVisible():
            return
        query = replace(parse_query(""), kind=self.window.kind)
        if self.prefetching is not None:
            self.prefetching.set()
        cancel = self.prefetching = threading.Event()
        self.tasks.submit(
            TaskCategory.PREFETCH,
            partial(self._work, query, self.settings(), cancel),
            partial(self._prefetched, cancel, query),
            lambda message: logger.info("Start list prefetch failed: %s", message),
        )

    def _prefetched(
        self, cancel: threading.Event, query: SearchQuery, result: SearchResults
    ) -> None:
        if cancel is not self.prefetching:
            return
        self.prefetching = None
        self.start_list = StartList(query, result)

    def _start_list_for_typed_query(self) -> StartList | None:
        if self.start_list is None:
            return None
        try:
            query = self.current_query()
        except ValueError:
            return None
        return self.start_list if query == self.start_list.query else None

    def request_open(self, hit: LauncherHit | None) -> None:
        try:
            query = self.current_query()
        except ValueError:
            return
        if self._must_wait_for_results(hit, query):
            self.open_pending = True
            self.run()
            return
        if hit is not None:
            files = _file_query(query)
            self.open_requested.emit(hit, files.history_key if files else "")

    def _must_wait_for_results(self, hit: LauncherHit | None, query: LauncherQuery) -> bool:
        """Enter refers to the typed query; its results may not be displayed yet."""
        if self.displayed_query != query:
            return True
        return hit is None and self.running is not None

    def window_hidden(self) -> None:
        self.open_pending = False
        self.debounce.stop()
        self._abandon_running()
        self.prefetch_start_list()

    def close(self) -> None:
        """Stop searching for good; hiding the bar while quitting starts nothing new."""
        self.closed = True
        self.debounce.stop()
        self._abandon_running()
        if self.prefetching is not None:
            self.prefetching.set()

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
        self, query: LauncherQuery, settings: Settings, cancel: threading.Event
    ) -> SearchResults:
        """Search the catalog; a website or a plugin's keyword answers above the files.

        A plugin keyword puts that plugin's items first and still searches the
        catalog for the same text, so the keyword takes no word away from it;
        `@keyword` leaves the catalog out. Plain word searches merge the items of
        every enabled plugin into the files by score. Runs on a worker, so it also
        checks which found files still exist.
        """
        started = time.perf_counter()
        hits: list[LauncherHit] = []
        limit = settings.max_results
        plugins = PluginSearch()
        if isinstance(query, PluginQuery):
            plugins = self._search_plugins(query, settings, cancel)
            hits.extend(plugins.hits)
        elif isinstance(query, WebsiteQuery):
            hits.append(query.target())
        if (files := _file_query(query)) is not None:
            found = self.service.search(files, settings, cancel=cancel)
            # Only a plain search adds every plugin; a keyword already named one.
            if isinstance(query, SearchQuery):
                plugins = self._search_plugins(files, settings, cancel)
                found = merge_hits(found, plugins.hits, limit)
            hits.extend(found)
        hits = [] if cancel.is_set() else hits[:limit]
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "Search finished: elapsed_ms=%.1f hits=%d cancelled=%s plugins=%s",
            elapsed_ms,
            len(hits),
            cancel.is_set(),
            plugins.summary(),
        )
        return SearchResults(hits, elapsed_ms, _missing_files(hits))

    def _search_plugins(
        self, query: SearchQuery | PluginQuery, settings: Settings, cancel: threading.Event
    ) -> PluginSearch:
        if self.plugins is None:
            return PluginSearch()
        return self.plugins.search(
            query,
            settings.plugins,
            settings.max_results,
            cancel=cancel,
            time_limit_ms=settings.plugin_time_limit_ms,
        )

    def _completed(
        self,
        cancel: threading.Event,
        query: LauncherQuery,
        result: SearchResults,
    ) -> None:
        if cancel is not self.running:
            return
        self.running = None
        if self.start_list is not None and query == self.start_list.query:
            self.start_list = StartList(query, result)
        if not self.window.isVisible():
            self.displayed_query = None
            return
        self._show(query, result)
        if self.open_pending:
            self.open_pending = False
            self.request_open(self.window.selected_hit())

    def _show(self, query: LauncherQuery, result: SearchResults) -> None:
        same_query = self.displayed_query == query
        self.displayed_query = query
        files = _file_query(query)
        terms = files or (query.search if isinstance(query, PluginQuery) else None)
        offer = (
            content_search_text(self.window.query.text())
            if not result.hits and files is not None and files.content_text is None
            else None
        )
        self.window.set_results(
            result.hits,
            result.elapsed_ms,
            preserve_selection=same_query,
            query=terms,
            missing=result.missing,
            content_search=offer,
        )

    def _failed(self, cancel: threading.Event, message: str) -> None:
        if cancel is not self.running:
            return
        self.running = None
        self.displayed_query = None
        self.open_pending = False
        self.window.show_search_error(message)


def _file_query(query: LauncherQuery) -> SearchQuery | None:
    """The catalog search behind `query`; a website or plugin query may have none."""
    if isinstance(query, WebsiteQuery | PluginQuery):
        return query.files
    return query


def _task_category(query: LauncherQuery) -> TaskCategory:
    """Direct navigation may wait for a network share, so it has its own workers."""
    files = _file_query(query)
    navigates = files is not None and files.navigation is not None
    return TaskCategory.NAVIGATION if navigates else TaskCategory.SEARCH


def content_search_text(text: str) -> str | None:
    """`contains:` for typed words that matched no name, if the Rust engine can run it.

    Text with any query syntax is left alone: turning it into a content search could
    change what its filters mean.
    """
    words = text.strip()
    if not words or not _PLAIN_WORDS.fullmatch(words):
        return None
    if any(word.startswith("-") for word in words.split()):
        return None  # An exclusion.
    if native_availability().module is None:
        return None
    return f'contains:"{words}"'


def _missing_files(hits: list[LauncherHit]) -> frozenset[Path]:
    """Found files that no longer exist; unreadable ones count as present."""
    missing = set()
    for hit in hits:
        if not isinstance(hit, SearchHit):
            continue
        try:
            long_paths.lstat(hit.item.path)
        except (FileNotFoundError, NotADirectoryError):
            missing.add(hit.item.path)
        except OSError:
            pass  # Offline or denied: it may well still be there.
    return frozenset(missing)
