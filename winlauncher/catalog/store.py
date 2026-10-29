"""SQLite catalog persistence with atomic snapshots and persistent usage signals."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from winlauncher.catalog.model import (
    CatalogItem,
    ItemKind,
    normalize,
    normalize_extension,
    path_key,
    path_range,
)

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 6
CONNECTION_TIMEOUT_SECONDS = 15
# Free pages a write leaves behind; beyond this many they go back to the file system.
FREE_PAGES_KEPT = 1024  # 4 MiB at SQLite's default page size.
INCREMENTAL_AUTO_VACUUM = 2  # How `PRAGMA auto_vacuum` reports the incremental mode.
_ENTRY_COLUMNS = """(
    path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    root TEXT NOT NULL,
    name TEXT NOT NULL,
    search_path TEXT NOT NULL,
    extension TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('file', 'folder', 'app')),
    size INTEGER NOT NULL,
    modified REAL NOT NULL,
    attributes INTEGER NOT NULL DEFAULT 0
)"""
# `path_key` is unique, so the `path` it is derived from is unique too.
_ENTRY_INDEXES = (
    "CREATE UNIQUE INDEX entries_scope ON entries(path_key)",
    "CREATE INDEX entries_root ON entries(root)",
    "CREATE INDEX entries_kind ON entries(kind)",
)
SCHEMA = (
    f"CREATE TABLE entries {_ENTRY_COLUMNS}",
    *_ENTRY_INDEXES,
    """CREATE TABLE usage (
           path_key TEXT PRIMARY KEY, launches INTEGER NOT NULL, last_used REAL NOT NULL
       )""",
    """CREATE TABLE query_usage (
           query TEXT NOT NULL, path_key TEXT NOT NULL, launches INTEGER NOT NULL,
           PRIMARY KEY(query, path_key)
       )""",
    "CREATE TABLE favorites (path_key TEXT PRIMARY KEY)",
)
# Version 4: the Rust engine keeps a copy of `entries` in memory and reloads it
# when the catalog version changes. Every inserted, updated or deleted entry
# increments `generation`; the random `id` tells a recreated catalog file apart
# from the one the copy was loaded from. Usage and favorites do not count: the
# engine reads those small tables on every search.
_VERSION_TRACKING = (
    "CREATE TABLE IF NOT EXISTS catalog_version (id TEXT NOT NULL, generation INTEGER NOT NULL)",
    """INSERT INTO catalog_version SELECT lower(hex(randomblob(16))), 0
       WHERE NOT EXISTS (SELECT 1 FROM catalog_version)""",
    *(
        f"""CREATE TRIGGER IF NOT EXISTS entries_version_{name} AFTER {event} ON entries BEGIN
               UPDATE catalog_version SET generation = generation + 1;
           END"""
        for name, event in (("ai", "INSERT"), ("ad", "DELETE"), ("au", "UPDATE"))
    ),
)

# Versions 1 and 2 keyed rows by the literal path spelling. Version 3 merges
# alternate spellings of the same Windows path while keeping usage history.
_IDENTITY_MIGRATION = (
    "UPDATE entries SET path_key = path_identity(path), root = owner_identity(path, root)",
    """DELETE FROM entries
       WHERE rowid NOT IN (SELECT min(rowid) FROM entries GROUP BY path_key)""",
    "DROP INDEX entries_scope",
    "CREATE UNIQUE INDEX entries_scope ON entries(path_key)",
    """CREATE TABLE usage_new (
           path_key TEXT PRIMARY KEY, launches INTEGER NOT NULL, last_used REAL NOT NULL
       )""",
    """INSERT INTO usage_new
       SELECT path_identity(path), sum(launches), max(last_used)
       FROM usage GROUP BY path_identity(path)""",
    """CREATE TABLE query_usage_new (
           query TEXT NOT NULL, path_key TEXT NOT NULL, launches INTEGER NOT NULL,
           PRIMARY KEY(query, path_key)
       )""",
    """INSERT INTO query_usage_new
       SELECT query, path_identity(path), sum(launches)
       FROM query_usage GROUP BY query, path_identity(path)""",
    "CREATE TABLE favorites_new (path_key TEXT PRIMARY KEY)",
    "INSERT OR IGNORE INTO favorites_new SELECT path_identity(path) FROM favorites",
    "DROP TABLE usage",
    "ALTER TABLE usage_new RENAME TO usage",
    "DROP TABLE query_usage",
    "ALTER TABLE query_usage_new RENAME TO query_usage",
    "DROP TABLE favorites",
    "ALTER TABLE favorites_new RENAME TO favorites",
)

# Version 5 drops the FTS5 trigram index, which took two thirds of the file:
# both engines match terms with LIKE, the Rust one in memory. It also drops the
# unique index on `path`, which `entries_scope` makes redundant. The rebuilt table keeps every rowid; dropping the
# old one drops its triggers, so the version tracking is set up again.
_COPIED_COLUMNS = (
    "path, path_key, root, name, search_path, extension, kind, size, modified, attributes"
)
_COMPACT_MIGRATION = (
    "DROP TABLE IF EXISTS entry_fts",
    f"CREATE TABLE entries_new {_ENTRY_COLUMNS}",
    f"""INSERT INTO entries_new(rowid, {_COPIED_COLUMNS})
        SELECT rowid, {_COPIED_COLUMNS} FROM entries""",
    "DROP TABLE entries",
    "ALTER TABLE entries_new RENAME TO entries",
    *_ENTRY_INDEXES,
    *_VERSION_TRACKING,
)

# Version 6 matches without accents (`normalize`), so `lohn` finds `Löhne`: the
# stored search text is computed again from each path.
_SEARCH_TEXT_MIGRATION = """
    UPDATE entries SET name = search_name(path, kind), search_path = search_text(path),
        extension = search_extension(path)
    WHERE name != search_name(path, kind) OR search_path != search_text(path)
"""

_MERGE_STAGED = """
    INSERT INTO entries SELECT * FROM staged WHERE 1  -- WHERE avoids an upsert parse ambiguity.
    ON CONFLICT(path_key) DO UPDATE SET
        path = excluded.path, root = excluded.root, name = excluded.name,
        search_path = excluded.search_path, extension = excluded.extension,
        kind = excluded.kind, size = excluded.size, modified = excluded.modified,
        attributes = excluded.attributes
    WHERE entries.path != excluded.path OR entries.name != excluded.name
        OR entries.modified != excluded.modified OR entries.root != excluded.root
        OR entries.kind != excluded.kind OR entries.size != excluded.size
        OR entries.attributes != excluded.attributes
"""


def _legacy_owner_identity(path: str, owner: str) -> str:
    """Re-key a version 1/2 owner id (`files:<path>`) using the entry's own spelling."""
    category, _, previous = owner.partition(":")
    if category not in ("files", "apps"):
        return owner
    depth = len(Path(previous).parts)
    return category + ":" + path_key(Path(*Path(path).parts[:depth]))


def _search_name(path: str, kind: str) -> str:
    return normalize(CatalogItem(Path(path), ItemKind(kind)).name)


def _search_extension(path: str) -> str:
    return normalize_extension(Path(path).suffix)


def _release_free_pages(connection: sqlite3.Connection) -> None:
    """Shrink the file after a write that freed many pages, as replacing a source does."""
    if connection.execute("PRAGMA freelist_count").fetchone()[0] > FREE_PAGES_KEPT:
        # Frees one page per step; Python 3.11's `execute` steps only once, while
        # `executescript` always runs it to the end.
        connection.executescript("PRAGMA incremental_vacuum")


def _placeholders(values: tuple) -> str:
    return ",".join("?" for _ in values)


def _staged_row(item: CatalogItem, source_id: str) -> tuple:
    return (
        str(item.path),
        path_key(item.path),
        source_id,
        normalize(item.name),
        normalize(str(item.path)),
        normalize_extension(item.path.suffix),
        str(item.kind),
        item.size,
        item.modified,
        item.attributes,
    )


class CatalogStore:
    """Each operation owns its connection; Qt and worker threads share no handles."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            # A new file takes this only before it switches to WAL; an existing one
            # with its next VACUUM (see `_apply_auto_vacuum`).
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            self._create_or_migrate(connection)
            self._apply_auto_vacuum(connection)

    @staticmethod
    def _create_or_migrate(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            script = "".join(statement + ";\n" for statement in (*SCHEMA, *_VERSION_TRACKING))
            connection.executescript(
                f"BEGIN;\n{script}PRAGMA user_version={SCHEMA_VERSION}; COMMIT;"
            )
            return
        if not 1 <= version < SCHEMA_VERSION:
            raise ValueError(f"Unsupported catalog schema: {version}; expected {SCHEMA_VERSION}.")
        for name, arguments, function in (
            ("path_identity", 1, path_key),
            ("owner_identity", 2, _legacy_owner_identity),
            ("search_name", 2, _search_name),
            ("search_text", 1, normalize),
            ("search_extension", 1, _search_extension),
        ):
            connection.create_function(name, arguments, function, deterministic=True)
        connection.execute("BEGIN IMMEDIATE")
        if version == 1:
            connection.execute(
                "ALTER TABLE entries ADD COLUMN attributes INTEGER NOT NULL DEFAULT 0"
            )
        if version in (1, 2):
            for statement in _IDENTITY_MIGRATION:
                connection.execute(statement)
        if version <= 4:
            for statement in _COMPACT_MIGRATION:
                connection.execute(statement)
        connection.execute(_SEARCH_TEXT_MIGRATION)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()

    @staticmethod
    def _apply_auto_vacuum(connection: sqlite3.Connection) -> None:
        """Once per file from before version 5: VACUUM applies `auto_vacuum` and
        returns all free pages, such as those of the dropped full-text index.

        Unlike the incremental vacuum after publications, VACUUM may renumber
        rowids, so the catalog gets a new id and no memory index keeps its copy.
        A file another process holds is left as it is until the next start.
        """
        if connection.execute("PRAGMA auto_vacuum").fetchone()[0] == INCREMENTAL_AUTO_VACUUM:
            return
        try:
            connection.execute("VACUUM")
        except sqlite3.OperationalError:
            logger.warning("Catalog not compacted; retrying at the next start", exc_info=True)
            return
        connection.execute("UPDATE catalog_version SET id = lower(hex(randomblob(16)))")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=CONNECTION_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={CONNECTION_TIMEOUT_SECONDS * 1000}")
        connection.execute("PRAGMA mmap_size=268435456")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def count(self) -> int:
        with self.connect() as connection:
            return connection.execute("SELECT count(*) FROM entries").fetchone()[0]

    def count_within(self, folder: Path) -> int:
        """Entries inside `folder`, using the path identity index."""
        with self.connect() as connection:
            query = "SELECT count(*) FROM entries WHERE path_key > ? AND path_key < ?"
            return connection.execute(query, path_range(folder)).fetchone()[0]

    # Staged publication -------------------------------------------------------

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """Stage a scan in a private temporary table without locking the live catalog."""
        with self.connect() as connection:
            connection.execute("CREATE TEMP TABLE staged AS SELECT * FROM entries WHERE 0")
            connection.execute("CREATE UNIQUE INDEX temp.staged_path ON staged(path_key)")
            yield connection

    @staticmethod
    def stage(connection: sqlite3.Connection, items: Iterable[tuple[CatalogItem, str]]) -> None:
        """Stage `(item, source id)` pairs; the first staging of a path wins."""
        rows = [_staged_row(item, source_id) for item, source_id in items]
        connection.executemany("INSERT OR IGNORE INTO staged VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        connection.commit()

    @staticmethod
    def preserve_subtree(connection: sqlite3.Connection, folder: Path) -> None:
        """Retain unreadable paths without suppressing cleanup in readable siblings."""
        connection.execute(
            "INSERT OR IGNORE INTO staged SELECT * FROM entries WHERE path_key >= ? AND path_key < ?",
            path_range(folder),
        )
        connection.commit()

    @staticmethod
    def publish(
        connection: sqlite3.Connection,
        *,
        active_roots: Iterable[str],
        preserved_roots: Iterable[str],
    ) -> None:
        """Publish a full scan: drop removed sources, replace active ones, keep preserved ones."""
        active = tuple(active_roots)
        keep = (*active, *preserved_roots)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"DELETE FROM entries WHERE root NOT IN ({_placeholders(keep)})", keep)
        connection.execute(
            f"""DELETE FROM entries WHERE root IN ({_placeholders(active)})
                AND path_key NOT IN (SELECT path_key FROM staged)""",
            active,
        )
        connection.execute(_MERGE_STAGED)
        connection.commit()
        _release_free_pages(connection)

    @staticmethod
    def publish_subtree(connection: sqlite3.Connection, folder: Path) -> None:
        """Reconcile a changed subtree without taking ownership of unrelated roots."""
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """DELETE FROM entries WHERE path_key >= ? AND path_key < ?
               AND path_key NOT IN (SELECT path_key FROM staged)""",
            path_range(folder),
        )
        connection.execute(_MERGE_STAGED)
        connection.commit()
        _release_free_pages(connection)

    @staticmethod
    def publish_files(connection: sqlite3.Connection, paths: tuple[Path, ...]) -> None:
        """Reconcile exact file identities; descendants and siblings are untouched."""
        if not paths:
            return
        keys = tuple(path_key(path) for path in paths)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""DELETE FROM entries WHERE path_key IN ({_placeholders(keys)})
                AND path_key NOT IN (SELECT path_key FROM staged)""",
            keys,
        )
        connection.execute(_MERGE_STAGED)
        connection.commit()
        _release_free_pages(connection)

    # Personal signals ---------------------------------------------------------

    def record_launch(self, path: Path, query_key: str) -> None:
        identity = path_key(path)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO usage VALUES(?, 1, ?) ON CONFLICT(path_key) DO UPDATE SET
                   launches = launches + 1, last_used = excluded.last_used""",
                (identity, time.time()),
            )
            if not query_key:
                return
            connection.execute(
                """INSERT INTO query_usage VALUES(?, ?, 1) ON CONFLICT(query, path_key)
                   DO UPDATE SET launches = launches + 1""",
                (query_key, identity),
            )

    def set_favorite(self, path: Path, favorite: bool) -> None:
        with self.connect() as connection:
            if favorite:
                connection.execute("INSERT OR IGNORE INTO favorites VALUES(?)", (path_key(path),))
            else:
                connection.execute("DELETE FROM favorites WHERE path_key = ?", (path_key(path),))

    def favorite_keys(self, keys: Iterable[str]) -> set[str]:
        """Which of these `path_key` identities are favorites."""
        keys = tuple(keys)
        if not keys:
            return set()
        placeholders = ",".join("?" for _ in keys)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT path_key FROM favorites WHERE path_key IN ({placeholders})", keys
            )
            return {row[0] for row in rows}

    def clear_history(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM usage")
            connection.execute("DELETE FROM query_usage")
