"""How the catalog file is stored: the version 5 migration and returning free pages."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pathscout.catalog import store
from pathscout.catalog.indexer import CatalogIndexer
from pathscout.catalog.model import path_key
from pathscout.catalog.store import CatalogStore
from pathscout.search.query_parser import parse_query
from pathscout.search.service import SearchService
from pathscout.settings import Settings

SCANNED_FILES = 400
LAUNCHES = 3

# The schema version 4 created, frozen here so the migration is tested against it.
VERSION_4_SCHEMA = """
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
CREATE TABLE catalog_version (id TEXT NOT NULL, generation INTEGER NOT NULL);
INSERT INTO catalog_version VALUES('version-4-catalog', 7);
CREATE TRIGGER entries_version_ai AFTER INSERT ON entries BEGIN
    UPDATE catalog_version SET generation = generation + 1;
END;
PRAGMA user_version=4;
"""


class CatalogStorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pathscout-storage-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "library"
        self.root.mkdir()
        self.settings = Settings(roots=(str(self.root),), index_apps=False)

    def test_version_4_catalog_drops_full_text_and_keeps_entries_and_usage(self):
        report = self.root / "Report Straße.txt"
        report.write_text("test", encoding="utf-8")
        database = self.base / "catalog.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(VERSION_4_SCHEMA)
            connection.execute(
                "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(report),
                    path_key(report),
                    "files:" + path_key(self.root),
                    "report strasse.txt",
                    store.normalize(str(report)),
                    "txt",
                    "file",
                    4,
                    0.0,
                    0,
                ),
            )
            connection.execute("INSERT INTO usage VALUES(?,?,1)", (path_key(report), LAUNCHES))
            rowid = connection.execute("SELECT rowid FROM entries").fetchone()[0]

        catalog = CatalogStore(database)

        with catalog.connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            self.assertFalse({name for name in tables if name.startswith("entry_fts")})
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                connection.execute("PRAGMA auto_vacuum").fetchone()[0],
                store.INCREMENTAL_AUTO_VACUUM,
            )
            self.assertEqual(connection.execute("SELECT rowid FROM entries").fetchone()[0], rowid)
            identity, generation = connection.execute(
                "SELECT id, generation FROM catalog_version"
            ).fetchone()
            self.assertNotEqual(identity, "version-4-catalog")  # A new table, a new copy.
        hit = SearchService(catalog).search(parse_query("strasse"), self.settings)[0]
        self.assertEqual((hit.item.path, hit.item.launches), (report, LAUNCHES))

        # The version tracking triggers came back with the rebuilt table.
        (self.root / "second.txt").write_text("test", encoding="utf-8")
        CatalogIndexer(catalog).scan(self.settings)
        with catalog.connect() as connection:
            self.assertGreater(
                connection.execute("SELECT generation FROM catalog_version").fetchone()[0],
                generation,
            )

    def test_new_catalog_returns_pages_a_large_removal_freed(self):
        catalog = CatalogStore(self.base / "catalog.sqlite3")
        indexer = CatalogIndexer(catalog)
        files = [self.root / f"{index:04} {'long name ' * 8}.txt" for index in range(SCANNED_FILES)]
        for path in files:
            path.write_text("test", encoding="utf-8")
        indexer.scan(self.settings)
        for path in files:
            path.unlink()

        with patch.object(store, "FREE_PAGES_KEPT", 0):
            indexer.scan(self.settings)

        with catalog.connect() as connection:
            self.assertEqual(
                connection.execute("PRAGMA auto_vacuum").fetchone()[0],
                store.INCREMENTAL_AUTO_VACUUM,
            )
            self.assertEqual(connection.execute("PRAGMA freelist_count").fetchone()[0], 0)

    def test_small_changes_keep_their_free_pages_for_reuse(self):
        catalog = CatalogStore(self.base / "catalog.sqlite3")
        indexer = CatalogIndexer(catalog)
        files = [self.root / f"{index:04} {'long name ' * 8}.txt" for index in range(SCANNED_FILES)]
        for path in files:
            path.write_text("test", encoding="utf-8")
        indexer.scan(self.settings)
        for path in files:
            path.unlink()

        indexer.scan(self.settings)

        with catalog.connect() as connection:
            freed = connection.execute("PRAGMA freelist_count").fetchone()[0]
        self.assertGreater(freed, 0)
        self.assertLessEqual(freed, store.FREE_PAGES_KEPT)


if __name__ == "__main__":
    unittest.main()
