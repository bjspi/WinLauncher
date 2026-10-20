"""Structured query filters: type, extension, location, modification date and size.

These decide eligibility only. Text relevance and personal ranking never
override a structured filter.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from pathscout.catalog.model import ItemKind, path_key
from pathscout.search.candidates import Candidate
from pathscout.search.query import SearchQuery


class StructuredFilters:
    """The query's structured filters, prepared once per search."""

    def __init__(self, query: SearchQuery):
        self.query = query
        self.scope_key = path_key(query.scope) if query.scope is not None else None

    def accepts(self, candidate: Candidate) -> bool:
        return (
            self._matches_type(candidate)
            and self._matches_location(candidate)
            and self._matches_date(candidate)
            and self._matches_size(candidate)
        )

    def _matches_type(self, candidate: Candidate) -> bool:
        if self.query.kind is not None and self.query.kind != candidate.kind:
            return False
        if any(
            fnmatchcase(candidate.extension, pattern) for pattern in self.query.excluded_extensions
        ):
            return False
        return not self.query.extensions or any(
            fnmatchcase(candidate.extension, pattern) for pattern in self.query.extensions
        )

    def _matches_location(self, candidate: Candidate) -> bool:
        """A scope returns its descendants, not the scope folder itself."""
        if self.scope_key is not None:
            identity = candidate.identity
            if identity == self.scope_key or not identity.startswith(self.scope_key):
                return False
        return not any(term in candidate.search_path for term in self.query.excluded_terms)

    def _matches_date(self, candidate: Candidate) -> bool:
        after, before = self.query.modified_after, self.query.modified_before
        if after is not None and candidate.modified < after:
            return False
        if before is not None and candidate.modified >= before:
            return False
        return True

    def _matches_size(self, candidate: Candidate) -> bool:
        lower, upper = self.query.min_size, self.query.max_size
        if lower is None and upper is None:
            return True
        return (
            candidate.kind != ItemKind.FOLDER
            and (lower is None or candidate.size >= lower)
            and (upper is None or candidate.size <= upper)
        )
