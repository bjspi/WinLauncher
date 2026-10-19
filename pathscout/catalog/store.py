"""SQLite catalog persistence with atomic snapshots and persistent usage signals."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pathscout.catalog.model import (
    CatalogItem,
    normalize,
    normalize_extension,
    path_key,
    path_range,
)

SCHEMA_VERSION = 3
CONNECTION_TIMEOUT_SECONDS = 15
SCHEMA = """
CREATE TABLE entries (
    path TEXT PRIMARY KEY,
    path_key TEXT NOT NULL,
    root TEXT NOT NULL,
    name TEXT NOT NULL,
    search_path TEXT NOT NULL,
    extension TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('file', 'folder', 'app')),
    size INTEGER NOT NULL,
    modified REAL NOT NULL,
    attributes INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX entries_scope ON entries(path_key);
CREATE INDEX entries_root ON entries(root);
CREATE INDEX entries_kind ON entries(kind);
CREATE TABLE usage (path_key TEXT PRIMARY KEY, launches INTEGER NOT NULL, last_used REAL NOT NULL);
CREATE TABLE query_usage (
    query TEXT NOT NULL, path_key TEXT NOT NULL, launches INTEGER NOT NULL,
    PRIMARY KEY(query, path_key)
);
CREATE TABLE favorites (path_key TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE entry_fts USING fts5(
    name, search_path, content=entries, content_rowid=rowid, tokenize='trigram'
);
CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entry_fts(rowid, name, search_path) VALUES(new.rowid, new.name, new.search_path);
END;
CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entry_fts(entry_fts, rowid, name, search_path)
    VALUES('delete', old.rowid, old.name, old.search_path);
END;
CREATE TRIGGER entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entry_fts(entry_fts, rowid, name, search_path)
    VALUES('delete', old.rowid, old.name, old.search_path);
    INSERT INTO entry_fts(rowid, name, search_path) VALUES(new.rowid, new.name, new.search_path);
END;
"""

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
            connection.execute("PRAGMA journal_mode=WAL")
            self._create_or_migrate(connection)

    @staticmethod
    def _create_or_migrate(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 0:
            connection.executescript(
                f"BEGIN;\n{SCHEMA}PRAGMA user_version={SCHEMA_VERSION}; COMMIT;"
            )
            return
        if version not in (1, 2):
            raise ValueError(f"Unsupported catalog schema: {version}; expected {SCHEMA_VERSION}.")
        connection.create_function("path_identity", 1, path_key, deterministic=True)
        connection.create_function("owner_identity", 2, _legacy_owner_identity, deterministic=True)
        connection.execute("BEGIN IMMEDIATE")
        if version == 1:
            connection.execute(
                "ALTER TABLE entries ADD COLUMN attributes INTEGER NOT NULL DEFAULT 0"
            )
        for statement in _IDENTITY_MIGRATION:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()

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

    def clear_history(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM usage")
            connection.execute("DELETE FROM query_usage")
