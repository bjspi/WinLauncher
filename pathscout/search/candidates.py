"""Translate a query into SQL that streams candidate rows from the catalog.

SQL only narrows the candidates; ranking makes the final decision. Fuzzy search
runs two passes: the literal pass first, then a subsequence pass that skips
everything the literal pass already returned.

Candidates carry the catalog's stored, already-normalized text and no `Path`
object: most candidates never reach the result list, so building paths and
normalizing text for each of them would be wasted work.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pathscout.catalog.model import (
    CatalogItem,
    ItemKind,
    normalize,
    normalize_extension,
    path_key,
    path_range,
)
from pathscout.catalog.store import CatalogStore
from pathscout.search.query import SearchQuery

MIN_TRIGRAM_TERM_LENGTH = 3  # FTS5 trigram indexes cannot match shorter terms.
CANCEL_CHECK_INSTRUCTIONS = 1000

# Column order must match `_candidate_from_row`.
_CANDIDATE_SQL = """
    SELECT e.path, e.path_key, e.kind, e.name, e.search_path, e.extension,
        e.size, e.modified, e.attributes,
        coalesce(u.launches, 0) AS launches,
        coalesce(u.last_used, 0) AS last_used,
        coalesce(q.launches, 0) AS query_launches,
        f.path_key IS NOT NULL AS pinned
    FROM {entries}
    LEFT JOIN usage u ON u.path_key = e.path_key
    LEFT JOIN query_usage q ON q.path_key = e.path_key AND q.query = :history_key
    LEFT JOIN favorites f ON f.path_key = e.path_key
    WHERE {where}
"""
# Empty type filters must probe learned identities rather than walk the kind index.
_LEARNED_ENTRIES = "entries e INDEXED BY entries_scope"
_ALL_ENTRIES = "entries e"
_LEARNED_ONLY = "e.path_key IN (SELECT path_key FROM usage UNION SELECT path_key FROM favorites)"


def _file_name(path_text: str) -> str:
    """The last path component; stored paths are absolute Windows paths."""
    separator = max(path_text.rfind("\\"), path_text.rfind("/"))
    return path_text[separator + 1 :]


def _pathlib_stem(file_name: str) -> str:
    """`PurePath.stem` without building a path: a leading or trailing dot is no suffix."""
    dot = file_name.rfind(".")
    return file_name[:dot] if 0 < dot < len(file_name) - 1 else file_name


@dataclass(frozen=True, slots=True)
class Candidate:
    """One catalog row as stored: search-normalized text, no `Path` object yet."""

    path_text: str
    identity: str  # `path_key` of the path.
    file_name: str
    kind: ItemKind
    search_name: str  # Normalized display name.
    search_path: str  # Normalized full path.
    extension: str  # Normalized, without the dot.
    size: int = 0
    modified: float = 0
    attributes: int = 0
    launches: int = 0
    last_used: float = 0
    query_launches: int = 0
    pinned: bool = False

    @property
    def stem(self) -> str:
        return _pathlib_stem(self.file_name)

    @property
    def name(self) -> str:
        """The display name, as `CatalogItem.name`: applications omit their extension."""
        return self.stem if self.kind == ItemKind.APP else self.file_name

    def to_item(self) -> CatalogItem:
        return CatalogItem(
            Path(self.path_text),
            self.kind,
            self.size,
            self.modified,
            self.launches,
            self.last_used,
            self.query_launches,
            self.pinned,
            self.attributes,
        )

    @classmethod
    def from_item(cls, item: CatalogItem) -> Candidate:
        """Normalize like the catalog does when it stages an item."""
        return cls(
            str(item.path),
            path_key(item.path),
            item.path.name,
            item.kind,
            normalize(item.name),
            normalize(str(item.path)),
            normalize_extension(item.path.suffix),
            item.size,
            item.modified,
            item.attributes,
            item.launches,
            item.last_used,
            item.query_launches,
            item.pinned,
        )


class _SqlFilter:
    """WHERE conditions with their parameters, so clause and values cannot drift apart.

    Placeholders become named parameters with the filter's prefix, so nested
    filters can be combined without renumbering.
    """

    def __init__(self, prefix: str = "p"):
        self.prefix = prefix
        self.clauses: list[str] = []
        self.parameters: dict[str, object] = {}

    def require(self, clause: str, *values: object) -> None:
        """Add `clause`, whose `?` placeholders are bound to `values` in order."""
        assert clause.count("?") == len(values), f"Placeholder mismatch in {clause!r}"
        for value in values:
            name = f"{self.prefix}{len(self.parameters)}"
            clause = clause.replace("?", f":{name}", 1)
            self.parameters[name] = value
        self.clauses.append(clause)

    def require_none_of(self, other: _SqlFilter) -> None:
        """Exclude rows matching every condition of `other`."""
        if not other.clauses:
            return
        self.clauses.append(f"NOT ({other.sql()})")
        self.parameters.update(other.parameters)

    def sql(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else "1"


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(text: str) -> str:
    return "%" + _escape_like(text) + "%"


def _subsequence(text: str) -> str:
    return "%" + "%".join(_escape_like(char) for char in text) + "%"


_NAME_LIKE = "e.name LIKE ? ESCAPE '\\'"
_PATH_LIKE = "e.search_path LIKE ? ESCAPE '\\'"
_NAME_OR_PATH_LIKE = f"({_NAME_LIKE} OR {_PATH_LIKE})"


def read_candidates(
    store: CatalogStore,
    query: SearchQuery,
    *,
    fuzzy: bool = False,
    cancel: threading.Event | None = None,
) -> Iterator[Candidate]:
    where = _SqlFilter()
    _require_structured_filters(where, query)
    if fuzzy:
        _require_fuzzy_text(where, query)
    else:
        _require_literal_text(where, query)
    entries = _LEARNED_ENTRIES if query.uses_learned_items_only else _ALL_ENTRIES
    sql = _CANDIDATE_SQL.format(entries=entries, where=where.sql())
    parameters = {"history_key": query.history_key, **where.parameters}
    with store.connect() as connection:
        connection.row_factory = None  # Plain tuples; rows are unpacked by position.
        if cancel is not None:
            connection.set_progress_handler(cancel.is_set, CANCEL_CHECK_INSTRUCTIONS)
        yield from _stream_items(connection, sql, parameters, cancel)


def _stream_items(
    connection: sqlite3.Connection,
    sql: str,
    parameters: dict[str, object],
    cancel: threading.Event | None,
) -> Iterator[Candidate]:
    try:
        for row in connection.execute(sql, parameters):
            if cancel is not None and cancel.is_set():
                return
            yield _candidate_from_row(row)
    except sqlite3.OperationalError:
        # The progress handler interrupts a cancelled statement with this error.
        if cancel is None or not cancel.is_set():
            raise


def _candidate_from_row(row: tuple) -> Candidate:
    (
        path_text,
        identity,
        kind,
        search_name,
        search_path,
        extension,
        size,
        modified,
        attributes,
        launches,
        last_used,
        query_launches,
        pinned,
    ) = row
    return Candidate(
        path_text,
        identity,
        _file_name(path_text),
        ItemKind(kind),
        search_name,
        search_path,
        extension,
        size,
        modified,
        attributes,
        launches,
        last_used,
        query_launches,
        bool(pinned),
    )


def _require_structured_filters(where: _SqlFilter, query: SearchQuery) -> None:
    if query.scope:
        where.require("e.path_key >= ? AND e.path_key < ?", *path_range(query.scope))
    if query.uses_learned_items_only:
        where.require(_LEARNED_ONLY)
    if query.kind:
        where.require("e.kind = ?", str(query.kind))
    if query.extensions:
        placeholders = ",".join("?" for _ in query.extensions)
        where.require(f"e.extension IN ({placeholders})", *query.extensions)
    if query.modified_after is not None:
        where.require("e.modified >= ?", query.modified_after)
    if query.modified_before is not None:
        where.require("e.modified < ?", query.modified_before)
    for term in query.excluded_terms:
        where.require(f"NOT {_PATH_LIKE}", _contains(term))


def _require_literal_text(where: _SqlFilter, query: SearchQuery) -> None:
    for part in query.folder_parts:
        where.require(_PATH_LIKE, _contains(part))
    long_terms = [t for t in query.terms if len(t) >= MIN_TRIGRAM_TERM_LENGTH]
    short_terms = [t for t in query.terms if len(t) < MIN_TRIGRAM_TERM_LENGTH]
    if long_terms:
        where.require(
            "e.rowid IN (SELECT rowid FROM entry_fts WHERE entry_fts MATCH ?)",
            _fts_expression(long_terms, query.basename_only),
        )
    for term in short_terms:
        _require_term(where, query, name_pattern=_contains(term), path_pattern=_contains(term))


def _require_fuzzy_text(where: _SqlFilter, query: SearchQuery) -> None:
    for part in query.folder_parts:
        where.require(_PATH_LIKE, _subsequence(part))
    for term in query.terms:
        _require_term(where, query, name_pattern=_subsequence(term), path_pattern=_contains(term))
    _exclude_literal_matches(where, query)


def _exclude_literal_matches(where: _SqlFilter, query: SearchQuery) -> None:
    """The literal pass already returned rows that contain every term and folder part.

    This LIKE form must match exactly what the literal pass's FTS trigram query
    matches; trigram MATCH on quoted terms is substring containment, as LIKE is.
    """
    literal = _SqlFilter(prefix="literal")
    for term in query.terms:
        _require_term(literal, query, name_pattern=_contains(term), path_pattern=_contains(term))
    for part in query.folder_parts:
        literal.require(_PATH_LIKE, _contains(part))
    where.require_none_of(literal)


def _require_term(
    where: _SqlFilter, query: SearchQuery, *, name_pattern: str, path_pattern: str
) -> None:
    if query.basename_only:
        where.require(_NAME_LIKE, name_pattern)
    else:
        where.require(_NAME_OR_PATH_LIKE, name_pattern, path_pattern)


def _fts_expression(terms: list[str], basename_only: bool) -> str:
    column = "name: " if basename_only else ""
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " AND ".join(column + term for term in quoted)
