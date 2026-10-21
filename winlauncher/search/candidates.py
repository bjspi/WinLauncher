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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from winlauncher.catalog.model import (
    APPLICATION_SOURCE_PREFIX,
    REGISTRY_SOURCE,
    CatalogItem,
    ItemKind,
    file_stem,
    normalize,
    normalize_extension,
    path_key,
    path_range,
)
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.matching import is_wildcard, matches_path
from winlauncher.search.query import SearchQuery

CANCEL_CHECK_INSTRUCTIONS = 1000

# Column order must match `candidate_from_row` and the native module's `Candidate`.
_CANDIDATE_SQL = """
    SELECT e.path, e.path_key, e.kind, e.name, e.search_path, e.extension,
        e.size, e.modified, e.attributes,
        coalesce(u.launches, 0) AS launches,
        coalesce(u.last_used, 0) AS last_used,
        coalesce(q.launches, 0) AS query_launches,
        f.path_key IS NOT NULL AS pinned,
        (e.root = '{registry}' OR e.root LIKE '{applications}%') AS application_source
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
    # From a Start-menu folder or App Paths: file-name exclusion patterns do not apply.
    application_source: bool = False

    @property
    def stem(self) -> str:
        return file_stem(self.file_name)

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

    def to_row(self) -> tuple:
        """The native adapter uses the same column layout as candidate SQL."""
        return (
            self.path_text,
            self.identity,
            str(self.kind),
            self.search_name,
            self.search_path,
            self.extension,
            self.size,
            self.modified,
            self.attributes,
            self.launches,
            self.last_used,
            self.query_launches,
            self.pinned,
            self.application_source,
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


# A condition as SQL text plus the same condition as a plan node for the Rust
# engine's memory index (`native/src/plan.rs`), which evaluates it like SQLite.
_Condition = tuple[str, tuple]
_COLUMNS = frozenset(("path_key", "kind", "name", "search_path", "extension", "size", "modified"))
_OPERATORS = frozenset(("=", "!=", "<", "<=", ">", ">="))


class _CandidateFilter:
    """WHERE conditions with their parameters and their plan, so the SQL, its values
    and the Rust engine's in-memory evaluation cannot drift apart.

    Placeholders become named parameters with the filter's prefix, so nested
    filters can be combined without renumbering. The plan is a list of nodes that
    must all hold: `("like", column, pattern)`, `("compare", column, operator,
    value)`, `("in", column, values)`, `("any", nodes)`, `("not_all", nodes)` and
    `("learned",)`.
    """

    def __init__(self, prefix: str = "p"):
        self.prefix = prefix
        self.clauses: list[str] = []
        self.parameters: dict[str, object] = {}
        self.plan: list[tuple] = []

    def _bind(self, value: object) -> str:
        name = f"{self.prefix}{len(self.parameters)}"
        self.parameters[name] = value
        return ":" + name

    def like(self, column: str, pattern: str) -> _Condition:
        """`column LIKE pattern` with backslash escapes, as `_contains` builds them."""
        assert column in _COLUMNS, column
        return f"e.{column} LIKE {self._bind(pattern)} ESCAPE '\\'", ("like", column, pattern)

    def compare(self, column: str, operator: str, value: object) -> _Condition:
        assert column in _COLUMNS and operator in _OPERATORS, (column, operator)
        return f"e.{column} {operator} {self._bind(value)}", ("compare", column, operator, value)

    def one_of(self, column: str, values: list[str]) -> _Condition:
        assert column in _COLUMNS and values, column
        placeholders = ",".join(self._bind(value) for value in values)
        return f"e.{column} IN ({placeholders})", ("in", column, tuple(values))

    @staticmethod
    def any_of(*conditions: _Condition) -> _Condition:
        sql = " OR ".join(clause for clause, _ in conditions)
        return f"({sql})", ("any", tuple(node for _, node in conditions))

    @staticmethod
    def learned() -> _Condition:
        """Rows with a launch history or a favorite mark."""
        return _LEARNED_ONLY, ("learned",)

    def require(self, condition: _Condition) -> None:
        clause, node = condition
        self.clauses.append(clause)
        self.plan.append(node)

    def require_none_of(self, other: _CandidateFilter) -> None:
        """Exclude rows matching every condition of `other`."""
        if not other.clauses:
            return
        self.clauses.append(f"NOT ({other.sql()})")
        self.plan.append(("not_all", tuple(other.plan)))
        self.parameters.update(other.parameters)

    def nested(self) -> _CandidateFilter:
        """An empty filter whose parameter names cannot collide with this one's."""
        return _CandidateFilter(prefix=f"{self.prefix}{len(self.parameters)}_")

    def sql(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else "1"


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(text: str) -> str:
    if is_wildcard(text):
        # A LIKE prefix safely narrows character classes; fnmatch decides the full match.
        if "[" in text:
            text = text.partition("[")[0] + "*"
        return (
            "%"
            + "".join(
                "%" if char == "*" else "_" if char in "?/\\" else _escape_like(char)
                for char in text
            )
            + "%"
        )
    return "%" + _escape_like(text) + "%"


def _subsequence(text: str) -> str:
    if is_wildcard(text):
        return _contains(text)
    return "%" + "%".join(_escape_like(char) for char in text) + "%"


def read_candidates(
    store: CatalogStore,
    query: SearchQuery,
    *,
    fuzzy: bool = False,
    cancel: threading.Event | None = None,
) -> Iterator[Candidate]:
    sql, parameters = candidate_sql(query, fuzzy=fuzzy)
    with store.connect() as connection:
        connection.row_factory = None  # Plain tuples; rows are unpacked by position.
        if cancel is not None:
            connection.set_progress_handler(cancel.is_set, CANCEL_CHECK_INSTRUCTIONS)
        yield from _stream_items(connection, sql, parameters, cancel)


def candidate_sql(query: SearchQuery, fuzzy: bool = False) -> tuple[str, dict[str, object]]:
    """Build one candidate pass for either the Python or native SQLite reader."""
    where = _candidate_filter(query, fuzzy)
    entries = _LEARNED_ENTRIES if query.uses_learned_items_only else _ALL_ENTRIES
    sql = _CANDIDATE_SQL.format(
        entries=entries,
        where=where.sql(),
        registry=REGISTRY_SOURCE,
        applications=APPLICATION_SOURCE_PREFIX,
    )
    parameters = {"history_key": query.history_key, **where.parameters}
    return sql, parameters


def candidate_plan(query: SearchQuery, fuzzy: bool = False) -> list[tuple]:
    """The pass `candidate_sql` builds, as conditions for the Rust memory index."""
    return _candidate_filter(query, fuzzy).plan


def _candidate_filter(query: SearchQuery, fuzzy: bool) -> _CandidateFilter:
    where = _CandidateFilter()
    _require_structured_filters(where, query)
    if fuzzy:
        _require_fuzzy_text(where, query)
    else:
        _require_literal_text(where, query)
    return where


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
            yield candidate_from_row(row)
    except sqlite3.OperationalError:
        # The progress handler interrupts a cancelled statement with this error.
        if cancel is None or not cancel.is_set():
            raise


def candidate_from_row(row: tuple) -> Candidate:
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
        application_source,
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
        bool(application_source),
    )


def _require_structured_filters(where: _CandidateFilter, query: SearchQuery) -> None:
    if query.scope:
        lower, upper = path_range(query.scope)
        where.require(where.compare("path_key", ">=", lower))
        where.require(where.compare("path_key", "<", upper))
    if query.uses_learned_items_only:
        where.require(where.learned())
    if query.kind:
        where.require(where.compare("kind", "=", str(query.kind)))
    if query.content_text is not None:
        where.require(where.compare("kind", "=", str(ItemKind.FILE)))
    if query.extensions:
        if any(is_wildcard(extension) for extension in query.extensions):
            # Anchor the suffix; fnmatch decides character classes after SQL narrows candidates.
            patterns = [_contains(extension)[1:-1] for extension in query.extensions]
            where.require(where.any_of(*(where.like("extension", p) for p in patterns)))
        else:
            where.require(where.one_of("extension", list(query.extensions)))
    # Excluded patterns are left to the engines: a LIKE superset could hide eligible rows.
    excluded = [extension for extension in query.excluded_extensions if not is_wildcard(extension)]
    if excluded:
        _exclude(where, lambda nested: nested.one_of("extension", excluded))
    if query.modified_after is not None:
        where.require(where.compare("modified", ">=", query.modified_after))
    if query.modified_before is not None:
        where.require(where.compare("modified", "<", query.modified_before))
    if query.min_size is not None or query.max_size is not None:
        where.require(where.compare("kind", "!=", str(ItemKind.FOLDER)))
    if query.min_size is not None:
        where.require(where.compare("size", ">=", query.min_size))
    if query.max_size is not None:
        where.require(where.compare("size", "<=", query.max_size))
    for term in query.excluded_terms:
        pattern = "%" + _escape_like(term) + "%"  # Excluded text is never a wildcard.
        _exclude(where, lambda nested, pattern=pattern: nested.like("search_path", pattern))


def _exclude(where: _CandidateFilter, condition: Callable[[_CandidateFilter], _Condition]) -> None:
    """Exclude rows that match the single condition `condition` builds."""
    nested = where.nested()
    nested.require(condition(nested))
    where.require_none_of(nested)


def _require_literal_text(where: _CandidateFilter, query: SearchQuery) -> None:
    for part in query.folder_parts:
        where.require(where.like("search_path", _contains(part)))
    for term in query.terms:
        _require_term(where, query, term, name_pattern=_contains(term))


def _require_fuzzy_text(where: _CandidateFilter, query: SearchQuery) -> None:
    for part in query.folder_parts:
        where.require(where.like("search_path", _subsequence(part)))
    for term in query.terms:
        name_pattern = _contains(term) if term in query.exact_terms else _subsequence(term)
        _require_term(where, query, term, name_pattern=name_pattern)
    _exclude_literal_matches(where, query)


def _exclude_literal_matches(where: _CandidateFilter, query: SearchQuery) -> None:
    """The literal pass already returned rows that contain every term and folder part."""
    literal = _CandidateFilter(prefix="literal")
    for term in query.terms:
        _require_term(literal, query, term, name_pattern=_contains(term))
    for part in query.folder_parts:
        literal.require(literal.like("search_path", _contains(part)))
    where.require_none_of(literal)


def _require_term(
    where: _CandidateFilter, query: SearchQuery, term: str, *, name_pattern: str
) -> None:
    """The name matches `name_pattern`, or the path contains `term` where paths count."""
    name = where.like("name", name_pattern)
    if query.basename_only or not matches_path(term):
        where.require(name)
    else:
        where.require(where.any_of(name, where.like("search_path", _contains(term))))
