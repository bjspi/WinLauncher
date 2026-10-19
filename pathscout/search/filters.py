"""Structured query filters: type, extension, location and modification date.

These decide eligibility only. Text relevance and personal ranking never
override a structured filter.
"""

from __future__ import annotations

from pathscout.catalog.model import CatalogItem, is_within, normalize, normalize_extension
from pathscout.search.query import SearchQuery


def passes_structured_filters(query: SearchQuery, item: CatalogItem) -> bool:
    return (
        _matches_type(query, item) and _matches_location(query, item) and _matches_date(query, item)
    )


def _matches_type(query: SearchQuery, item: CatalogItem) -> bool:
    if query.kind is not None and query.kind != item.kind:
        return False
    if not query.extensions:
        return True
    return normalize_extension(item.path.suffix) in query.extensions


def _matches_location(query: SearchQuery, item: CatalogItem) -> bool:
    """A scope returns its descendants, not the scope folder itself."""
    if query.scope is not None:
        if item.path == query.scope or not is_within(item.path, query.scope):
            return False
    path = normalize(str(item.path))
    return not any(term in path for term in query.excluded_terms)


def _matches_date(query: SearchQuery, item: CatalogItem) -> bool:
    if query.modified_after is not None and item.modified < query.modified_after:
        return False
    if query.modified_before is not None and item.modified >= query.modified_before:
        return False
    return True
