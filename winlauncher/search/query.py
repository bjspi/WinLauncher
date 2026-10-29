"""The parsed search request. Parsing lives in `query_parser`, filtering in `filters`."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from winlauncher.catalog.model import ItemKind, normalize
from winlauncher.search.matching import MIN_FUZZY_TERM_LENGTH, is_wildcard


class MatchMode(StrEnum):
    KEYWORD = "keyword"
    FUZZY = "fuzzy"


class SortOrder(StrEnum):
    SCORE = "score"
    NAME = "name"
    MODIFIED = "modified"


class ScoreMode(StrEnum):
    MATCH = "match"
    COMBINED = "combined"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """All text values are search-normalized (`catalog.model.normalize`)."""

    terms: tuple[str, ...] = ()
    exact_terms: tuple[str, ...] = ()  # Quoted `terms`: contiguous text only, never fuzzy.
    kind: ItemKind | None = None
    extensions: tuple[str, ...] = ()
    scope: Path | None = None
    excluded_terms: tuple[str, ...] = ()
    excluded_extensions: tuple[str, ...] = ()  # Extensions/fnmatch patterns to reject.
    folder_parts: tuple[str, ...] = ()
    basename_only: bool = False
    match_mode: MatchMode = MatchMode.FUZZY
    sort: SortOrder = SortOrder.SCORE
    include_low_priority: bool = False
    modified_after: float | None = None
    modified_before: float | None = None
    score_mode: ScoreMode = ScoreMode.COMBINED
    navigation: str | None = None  # Typed path for direct navigation (`search/navigation.py`).
    min_size: int | None = None  # Inclusive byte bounds; folders have no searchable size.
    max_size: int | None = None
    content_text: str | None = None  # Case-insensitive fnmatch text; native backend only.
    # `terms` as typed, with their accents (`catalog.model.fold_case`); empty means
    # `terms`. A match spelled otherwise ranks a little lower: `lohn` finds Lohn
    # before Löhne, `löhn` finds Löhne before Lohn.
    spellings: tuple[str, ...] = ()
    explorer: bool = False  # Live direct entries first, then catalog matches in subfolders.

    @property
    def uses_learned_items_only(self) -> bool:
        """An empty default query (optionally with a type filter) shows recent and
        favorite items instead of browsing the catalog. Any other filter, including
        an explicit scope, browses."""
        return replace(self, kind=None) == _DEFAULT_QUERY

    @property
    def uses_fuzzy_pass(self) -> bool:
        """Fuzzy and abbreviation matches need two characters; shorter text skips that pass."""
        if self.match_mode != MatchMode.FUZZY:
            return False
        fuzzy_terms = (term for term in self.terms if term not in self.exact_terms)
        texts = (*fuzzy_terms, *self.folder_parts)
        return any(not is_wildcard(text) and len(text) >= MIN_FUZZY_TERM_LENGTH for text in texts)

    @property
    def history_key(self) -> str:
        """Launches are learned per query text and type/extension/path filters.

        Exclusions, scope, size and dates are left out: related searches share
        history, and a relative `date:today` keeps its key from day to day.
        """
        if self.navigation is not None:
            return "navigate:" + normalize(self.navigation)
        parts = list(self.terms)
        if self.kind:
            parts.append("type:" + self.kind)
        if self.extensions:
            parts.append("ext:" + ",".join(sorted(self.extensions)))
        if self.folder_parts:
            parts.append("path:" + "/".join(self.folder_parts))
        if self.content_text is not None:
            parts.append("contains:" + normalize(self.content_text))
        return " ".join(parts)


_DEFAULT_QUERY = SearchQuery()
