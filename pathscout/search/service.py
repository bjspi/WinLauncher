"""Search use case: stream candidates, rank them, keep the best, apply exclusions.

Three things keep broad searches cheap without changing their results:

- Candidates are plain catalog rows; `Path` objects and `SearchHit`s are built
  only for the hits that are finally displayed.
- Text is compared in the normalized form the catalog stores.
- Once the result list is full, a candidate whose best possible sort key cannot
  beat the weakest kept hit is skipped before any text matching.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from pathscout.catalog.exclusions import ExclusionPolicy
from pathscout.catalog.model import ItemKind
from pathscout.catalog.priority import PriorityDecision
from pathscout.catalog.store import CatalogStore
from pathscout.search.candidates import Candidate, read_candidates
from pathscout.search.explorer import directory_candidates
from pathscout.search.native_engine import (
    log_native_failure,
    native_availability,
    native_required,
    rank_with_native,
    select_native,
)
from pathscout.search.navigation import search_paths
from pathscout.search.query import SearchQuery, SortOrder
from pathscout.search.ranking import PriorityBand, Ranker, Ranking, SearchHit
from pathscout.settings import Settings

SortKey = tuple  # Compared lexicographically; smaller keys rank first.
logger = logging.getLogger(__name__)


class ResultOrder:
    """Sort keys for one query. Low-priority hits always follow the others.

    In a folder scope (an Explorer search), the folder's direct entries come
    before deeper matches; relevance orders hits within each group.
    """

    def __init__(self, query: SearchQuery, ranker: Ranker):
        self.order = query.sort
        self.ranker = ranker
        self.scope_key = ranker.filters.scope_key
        self.explorer = query.explorer

    def key(self, candidate: Candidate, ranking: Ranking) -> SortKey:
        if self.order == SortOrder.SCORE:
            return (*self._groups(candidate, ranking.band), -ranking.score, *_ties(candidate))
        return self._unscored_key(candidate, ranking.band)

    def best_possible_key(self, candidate: Candidate, priority: PriorityDecision) -> SortKey:
        """A key no worse than the candidate's real key, computed without matching text."""
        band = self.ranker.band(priority)
        if self.order == SortOrder.SCORE:
            bound = self.ranker.best_possible_score(candidate, priority)
            return (*self._groups(candidate, band), -bound)
        return self._unscored_key(candidate, band)

    def _groups(self, candidate: Candidate, band: PriorityBand) -> tuple:
        folder = (candidate.kind != ItemKind.FOLDER,) if self.explorer else ()
        return (*folder, band, 0 if self._is_direct_entry(candidate) else 1)

    def _unscored_key(self, candidate: Candidate, band: PriorityBand) -> SortKey:
        low = band == PriorityBand.FALLBACK
        folder = (candidate.kind != ItemKind.FOLDER,) if self.explorer else ()
        if self.order == SortOrder.NAME:
            return *folder, low, *_ties(candidate)
        return *folder, low, -candidate.modified, candidate.path_text

    def _is_direct_entry(self, candidate: Candidate) -> bool:
        if self.scope_key is None:
            return True
        parent_key = candidate.identity[:-1].rpartition("/")[0] + "/"
        return parent_key == self.scope_key


def _ties(candidate: Candidate) -> tuple[str, str]:
    return candidate.name.casefold(), candidate.path_text


@dataclass(slots=True)
class _Kept:
    key: SortKey
    candidate: Candidate
    ranking: Ranking

    def __lt__(self, other: _Kept) -> bool:
        # Reversed, so the heap root is the weakest kept hit.
        return self.key > other.key


class _BestHits:
    """Keeps the best `limit` candidates in O(limit) memory while they stream in."""

    def __init__(self, limit: int):
        self.limit = limit
        self._heap: list[_Kept] = []

    def could_keep(self, best_possible_key: SortKey) -> bool:
        """False only if even the best possible key loses to the weakest kept one."""
        return len(self._heap) < self.limit or not best_possible_key > self._heap[0].key

    def would_keep(self, key: SortKey) -> bool:
        return len(self._heap) < self.limit or key < self._heap[0].key

    def add(self, key: SortKey, candidate: Candidate, ranking: Ranking) -> None:
        kept = _Kept(key, candidate, ranking)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, kept)
        else:
            heapq.heapreplace(self._heap, kept)

    def hits(self) -> list[SearchHit]:
        ordered = sorted(self._heap, key=lambda kept: kept.key)
        return [kept.ranking.hit(kept.candidate.to_item()) for kept in ordered]


class _ExclusionCheck:
    """Exclusions for hits that would be kept; the check may probe the disk.

    In an Explorer search, folders listed directly from the open folder stay
    visible even when a marker would exclude their contents from the catalog.
    """

    def __init__(self, exclusions: ExclusionPolicy, direct_entries: bool):
        self.exclusions = exclusions
        self.direct_entries = direct_entries

    def __call__(self, path_text: str, directory: bool, application: bool) -> bool:
        if self.direct_entries and directory:
            return True
        return self.exclusions.allows(Path(path_text), directory=directory, application=application)

    def allows(self, candidate: Candidate) -> bool:
        directory = candidate.kind == ItemKind.FOLDER
        return self(candidate.path_text, directory, candidate.application_source)


class SearchService:
    def __init__(self, store: CatalogStore):
        self.store = store

    def search(
        self, query: SearchQuery, settings: Settings, *, cancel: threading.Event | None = None
    ) -> list[SearchHit]:
        if query.navigation is not None:
            logger.info("Search started: engine=path_navigation")
            return _hide_fallbacks_unless_needed(
                search_paths(query, settings, cancel, self.store), query
            )
        if query.explorer and query.scope is not None:
            candidates = list(directory_candidates(query.scope, cancel))
            hits = self._rank(query, settings, cancel, candidates)
            logger.info("Explorer direct search: entries=%d hits=%d", len(candidates), len(hits))
            if hits or (cancel is not None and cancel.is_set()):
                return hits
        return self._rank(query, settings, cancel)

    def _rank(
        self,
        query: SearchQuery,
        settings: Settings,
        cancel: threading.Event | None,
        candidates: list[Candidate] | None = None,
    ) -> list[SearchHit]:
        """Rank with the Rust engine; Python only when Rust is unavailable or failed."""
        native = select_native()
        content_search = query.content_text is not None
        if content_search and native is None:
            reason = native_availability().reason or "It is turned off for this run."
            raise ValueError(f"contains: needs the Rust search engine. {reason}")
        logger.info(
            "Search started: engine=%s content_filter=%s",
            "rust" if native is not None else "python",
            content_search,
        )
        direct_entries = query.explorer and candidates is not None
        check = _ExclusionCheck(settings.exclusions(), direct_entries)
        if native is not None:
            try:
                return self._rank_native(native, query, settings, cancel, candidates, check)
            except Exception as exc:
                if content_search:
                    logger.exception("Rust content search failed")
                    raise RuntimeError(f"contains: the content search failed: {exc}") from exc
                if native_required():
                    raise
                log_native_failure(exc)
        return self._rank_python(query, settings, cancel, candidates, check)

    def _rank_native(
        self,
        native: ModuleType,
        query: SearchQuery,
        settings: Settings,
        cancel: threading.Event | None,
        candidates: list[Candidate] | None,
        check: _ExclusionCheck,
    ) -> list[SearchHit]:
        kept = rank_with_native(
            native,
            self.store,
            query,
            settings.priorities(),
            Ranker(query),
            settings.max_results,
            check,
            cancel=cancel,
            content_search_threads=settings.content_search_threads,
            candidates=candidates,
        )
        if cancel is not None and cancel.is_set():
            return []
        hits = [ranking.hit(candidate.to_item()) for candidate, ranking in kept]
        return _hide_fallbacks_unless_needed(hits, query)

    def _rank_python(
        self,
        query: SearchQuery,
        settings: Settings,
        cancel: threading.Event | None,
        candidates: list[Candidate] | None,
        check: _ExclusionCheck,
    ) -> list[SearchHit]:
        priorities = settings.priorities()
        ranker = Ranker(query)
        order = ResultOrder(query, ranker)
        best = _BestHits(settings.max_results)
        stream = candidates if candidates is not None else self._candidates(query, cancel)
        for candidate in stream:
            if cancel is not None and cancel.is_set():
                return []
            priority = priorities.evaluate(candidate)
            if not ranker.eligible(candidate, priority):
                continue
            if not best.could_keep(order.best_possible_key(candidate, priority)):
                continue
            ranking = ranker.rank(candidate, priority)
            if ranking is None:
                continue
            key = order.key(candidate, ranking)
            # Marker probes touch the disk, so only hits that would be kept pay for them.
            if best.would_keep(key) and check.allows(candidate):
                best.add(key, candidate, ranking)
        if cancel is not None and cancel.is_set():
            return []
        return _hide_fallbacks_unless_needed(best.hits(), query)

    def _candidates(
        self, query: SearchQuery, cancel: threading.Event | None
    ) -> Iterator[Candidate]:
        literal = read_candidates(self.store, query, cancel=cancel)
        if not query.uses_fuzzy_pass:
            return literal
        fuzzy = read_candidates(self.store, query, fuzzy=True, cancel=cancel)
        return itertools.chain(literal, fuzzy)


def _hide_fallbacks_unless_needed(hits: list[SearchHit], query: SearchQuery) -> list[SearchHit]:
    """Low-priority hits appear only with `low:true` or when nothing else matched."""
    if query.include_low_priority:
        return hits
    preferred = [hit for hit in hits if not hit.low_priority]
    return preferred or hits
