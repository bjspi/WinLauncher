"""Search use case: stream candidates, rank them, keep the best, apply exclusions."""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from pathscout.catalog.model import CatalogItem, ItemKind
from pathscout.catalog.store import CatalogStore
from pathscout.search.candidates import read_candidates
from pathscout.search.query import SearchQuery, SortOrder
from pathscout.search.ranking import SearchHit, rank
from pathscout.settings import Settings

SortKey = tuple  # Compared lexicographically; built only by `sort_key_for`.


def sort_key_for(order: SortOrder, scope: Path | None = None) -> Callable[[SearchHit], SortKey]:
    """Smaller keys rank first. Low-priority hits always follow the others.

    In a folder scope (an Explorer search), the folder's direct entries come
    before deeper matches; relevance orders hits within each group.
    """
    if order == SortOrder.NAME:
        return lambda hit: (hit.low_priority, hit.item.name.casefold(), str(hit.item.path))
    if order == SortOrder.MODIFIED:
        return lambda hit: (hit.low_priority, -hit.item.modified, str(hit.item.path))
    return lambda hit: (
        hit.band,
        _depth_group(hit, scope),
        -hit.score,
        hit.item.name.casefold(),
        str(hit.item.path),
    )


def _depth_group(hit: SearchHit, scope: Path | None) -> int:
    if scope is None or hit.item.path.parent == scope:
        return 0
    return 1


@dataclass(slots=True)
class _Ranked:
    key: SortKey
    hit: SearchHit

    def __lt__(self, other: _Ranked) -> bool:
        # Reversed, so the heap root is the weakest retained hit.
        return self.key > other.key


class _BestHits:
    """Keeps the best `limit` hits in O(limit) memory while candidates stream in."""

    def __init__(self, limit: int):
        self.limit = limit
        self._heap: list[_Ranked] = []

    def would_keep(self, key: SortKey) -> bool:
        return len(self._heap) < self.limit or key < self._heap[0].key

    def add(self, key: SortKey, hit: SearchHit) -> None:
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, _Ranked(key, hit))
        else:
            heapq.heapreplace(self._heap, _Ranked(key, hit))

    def ordered(self) -> list[SearchHit]:
        return [ranked.hit for ranked in sorted(self._heap, key=lambda ranked: ranked.key)]


class SearchService:
    def __init__(self, store: CatalogStore):
        self.store = store

    def search(
        self, query: SearchQuery, settings: Settings, *, cancel: threading.Event | None = None
    ) -> list[SearchHit]:
        exclusions = settings.exclusions()
        priorities = settings.priorities()
        sort_key = sort_key_for(query.sort, query.scope)
        best = _BestHits(settings.max_results)
        for item in self._candidates(query, cancel):
            hit = rank(item, query, priorities.evaluate(item))
            if hit is None:
                continue
            key = sort_key(hit)
            # Marker probes touch the disk, so only hits that would be kept pay for them.
            if best.would_keep(key) and exclusions.allows(item.path, directory=_is_folder(item)):
                best.add(key, hit)
        if cancel is not None and cancel.is_set():
            return []
        return _hide_fallbacks_unless_needed(best.ordered(), query)

    def _candidates(
        self, query: SearchQuery, cancel: threading.Event | None
    ) -> Iterator[CatalogItem]:
        literal = read_candidates(self.store, query, cancel=cancel)
        if not query.uses_fuzzy_pass:
            return literal
        fuzzy = read_candidates(self.store, query, fuzzy=True, cancel=cancel)
        return itertools.chain(literal, fuzzy)


def _is_folder(item: CatalogItem) -> bool:
    return item.kind == ItemKind.FOLDER


def _hide_fallbacks_unless_needed(hits: list[SearchHit], query: SearchQuery) -> list[SearchHit]:
    """Low-priority hits appear only with `low:true` or when nothing else matched."""
    if query.include_low_priority:
        return hits
    preferred = [hit for hit in hits if not hit.low_priority]
    return preferred or hits
