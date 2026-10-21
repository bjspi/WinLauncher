"""The Rust engine's memory index: a copy of the catalog's entries for fast candidate scans.

Candidate SQL scans the whole table with `LIKE` on every keystroke. The Rust
engine therefore evaluates the same candidate conditions
(`candidates.candidate_plan`) against an in-memory copy of the entries table,
which it loads with the statements below. Only launches and favorites are read
from SQLite on each search.

The copy is never stale: every search reads `catalog_version`, which triggers
increment on each change to `entries`, and reloads the copy when it differs.
Loading takes a while on large catalogs, so call `native_engine.warm_search_index` on a worker
thread after the catalog changed; searches then find the copy current. A search
that finds another thread loading does not wait but uses candidate SQL.
"""

from __future__ import annotations

from dataclasses import dataclass

from winlauncher.catalog.model import APPLICATION_SOURCE_PREFIX, REGISTRY_SOURCE
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.candidates import candidate_plan
from winlauncher.search.query import SearchQuery

# Column order must match `CatalogIndex::load` in `native/src/catalog.rs`, and
# each column must equal what `_CANDIDATE_SQL` selects for the entry.
MEMORY_INDEX_SQL = {
    "version": "SELECT id, generation FROM catalog_version",
    "entries": f"""
        SELECT e.rowid, e.path, e.path_key, e.kind, e.name, e.search_path, e.extension,
            e.size, e.modified, e.attributes,
            (e.root = '{REGISTRY_SOURCE}' OR e.root LIKE '{APPLICATION_SOURCE_PREFIX}%')
        FROM entries e ORDER BY e.rowid
    """,
    # The joins of `_CANDIDATE_SQL`, as rowids: launches, last use, favorites.
    "usage": """
        SELECT e.rowid, u.launches, u.last_used
        FROM usage u JOIN entries e ON e.path_key = u.path_key
    """,
    "query_usage": """
        SELECT e.rowid, q.launches
        FROM query_usage q JOIN entries e ON e.path_key = q.path_key
        WHERE q.query = :history_key
    """,
    "favorites": "SELECT e.rowid FROM favorites f JOIN entries e ON e.path_key = f.path_key",
}


@dataclass(frozen=True, slots=True)
class IndexReport:
    """The memory index after `warm_search_index`."""

    entries: int
    bytes: int
    generation: int  # The `catalog_version.generation` the copy was loaded from.
    reloaded: bool  # False if the copy was current already.
    load_ms: float
    total_ms: float


def catalog_spec(store: CatalogStore) -> dict[str, object]:
    return {"database": str(store.path), "statements": MEMORY_INDEX_SQL}


def memory_plan(query: SearchQuery) -> dict[str, object]:
    """Both candidate passes of `query` as conditions for the memory index."""
    return {
        "literal": candidate_plan(query),
        "fuzzy": candidate_plan(query, fuzzy=True) if query.uses_fuzzy_pass else None,
        "history_key": query.history_key,
    }
