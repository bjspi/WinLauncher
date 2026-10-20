"""The Rust memory index must select what candidate SQL selects and never be stale."""

from __future__ import annotations

import importlib
import os
import random
import tempfile
import threading
import time
import unicodedata
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pathscout.catalog.model import CatalogItem, CatalogSource, ItemKind, normalize
from pathscout.catalog.priority import PriorityPolicy
from pathscout.catalog.store import CatalogStore
from pathscout.search import native_engine
from pathscout.search.memory_index import catalog_spec, memory_plan
from pathscout.search.query_parser import parse_query
from pathscout.search.ranking import Ranker
from pathscout.search.service import SearchService
from pathscout.settings import Settings

try:
    NATIVE = importlib.import_module("pathscout_native")
except ImportError:
    NATIVE = None

# The randomized LIKE differential test: deterministic, and quick.
LIKE_SEED = 20261020
LIKE_PATTERNS = 400
LIKE_NAMES = 60
LIKE_MAX_LENGTH = 6
# Pattern syntax, an escape, upper case (SQLite folds ASCII) and multibyte text.
LIKE_PATTERN_ALPHABET = "ab%_\\.Aé🙂"
LIKE_NAME_ALPHABET = "ab%_\\.é🙂"
CONCURRENT_SEARCHERS = 4
CONCURRENT_CHANGES = 15
CANCEL_LIMIT_S = 0.5  # Loose enough for busy CI runners, far below a full scan.


@contextmanager
def engine(name: str, phase: str = "memory"):
    with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE=name, PATHSCOUT_NATIVE_PHASE=phase):
        yield


class CatalogCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pathscout-memory-index-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "library"
        self.store = CatalogStore(self.base / "catalog.sqlite3")
        self.service = SearchService(self.store)
        self.settings = Settings(
            roots=(str(self.root),), index_apps=False, marker_files=(), excluded_patterns=()
        )

    def publish(self, *relative_paths: str) -> None:
        """Replace the catalog with files at `relative_paths` below the root."""
        source = CatalogSource(self.root).id
        items = [CatalogItem(self.root / path, ItemKind.FILE) for path in relative_paths]
        with self.store.snapshot() as connection:
            self.store.stage(connection, ((item, source) for item in items))
            self.store.publish(connection, active_roots=(source,), preserved_roots=())

    def names(self, text: str, engine_name: str = "native") -> list[str]:
        with engine(engine_name):
            hits = self.service.search(parse_query(text), self.settings)
        return [hit.item.path.relative_to(self.root).as_posix() for hit in hits]

    def assert_engines_agree(self, text: str) -> list[str]:
        expected = self.names(text, "python")
        self.assertEqual(self.names(text), expected, text)
        return expected


class ShortTermTest(CatalogCase):
    """Terms shorter than MIN_PATH_TERM_LENGTH match names, never parent folders."""

    def test_short_terms_ignore_folders_but_explicit_paths_still_match(self):
        self.publish("do-folder/report.txt", "dossier.txt", "x/d.txt", "dok/rechnung.pdf")
        for engine_name in ("python", "native") if NATIVE is not None else ("python",):
            with self.subTest(engine=engine_name):
                self.assertEqual(self.names("do", engine_name), ["dossier.txt"])
                self.assertEqual(
                    self.names("d", engine_name), ["x/d.txt", "dossier.txt", "dok/rechnung.pdf"]
                )
                self.assertEqual(
                    self.names("path:do report", engine_name), ["do-folder/report.txt"]
                )
                self.assertEqual(self.names("do/ report", engine_name), ["do-folder/report.txt"])
                # Three letters and more still find items through their folders.
                self.assertIn("dok/rechnung.pdf", self.names("dok", engine_name))
        if NATIVE is None:
            return
        for text in ("do", "d", "o r", "do rech", "d*", "*.pdf", '"do"'):
            with self.subTest(text=text):
                self.assert_engines_agree(text)


@unittest.skipUnless(NATIVE is not None, "Optional pathscout_native module is not installed")
class FreshnessTest(CatalogCase):
    def setUp(self):
        super().setUp()
        NATIVE.use_unicode(unicodedata.unidata_version)

    def warm(self) -> dict:
        return NATIVE.warm_catalog(catalog_spec(self.store))

    def test_every_catalog_change_reloads_and_searches_never_see_stale_entries(self):
        self.publish("alpha report.txt", "beta report.txt")
        self.assertTrue(self.warm()["reloaded"])
        self.assertFalse(self.warm()["reloaded"])
        reports = self.assert_engines_agree("rprt")
        self.assertEqual(sorted(reports), ["alpha report.txt", "beta report.txt"])
        self.publish("alpha report.txt", "gamma report.txt")  # One insert, one delete.
        reports = self.assert_engines_agree("rprt")
        self.assertEqual(sorted(reports), ["alpha report.txt", "gamma report.txt"])
        self.assertFalse(self.warm()["reloaded"], "the search reloaded the index itself")
        path = self.root / "gamma report.txt"
        with self.store.snapshot() as connection:
            item = CatalogItem(path, ItemKind.FILE, size=4096, modified=1.0)
            self.store.stage(connection, ((item, CatalogSource(self.root).id),))
            self.store.publish_files(connection, (path,))
        self.assertTrue(self.warm()["reloaded"])
        self.assert_engines_agree("rprt size:>1kb")

    def test_usage_and_favorites_apply_without_a_reload(self):
        self.publish("alpha report.txt", "beta report.txt")
        self.warm()
        self.store.record_launch(self.root / "beta report.txt", parse_query("rprt").history_key)
        self.store.set_favorite(self.root / "alpha report.txt", True)
        self.assertEqual(self.assert_engines_agree("rprt"), ["alpha report.txt", "beta report.txt"])
        self.assertEqual(self.assert_engines_agree(""), ["alpha report.txt", "beta report.txt"])
        self.assertFalse(self.warm()["reloaded"])

    def test_a_recreated_catalog_at_the_same_path_is_never_mistaken_for_the_old_one(self):
        self.publish("old report.txt")
        self.warm()
        self.store.path.unlink()
        self.store = CatalogStore(self.store.path)
        self.service = SearchService(self.store)
        self.publish("new report.txt")  # The same generation as the old catalog.
        self.assertEqual(self.assert_engines_agree("rprt"), ["new report.txt"])

    def test_warm_report_and_python_wrapper(self):
        self.publish("alpha report.txt")
        with engine("native"):
            report = native_engine.warm_search_index(self.store)
        self.assertEqual(report.entries, 1)
        self.assertGreater(report.bytes, 0)
        with engine("native", "sqlite"):
            self.assertIsNone(native_engine.warm_search_index(self.store))
        with engine("python"):
            self.assertIsNone(native_engine.warm_search_index(self.store))

    def test_a_search_that_finds_the_index_loading_uses_sql_instead(self):
        self.publish("alpha report.txt")
        query = parse_query("rprt")
        with (
            engine("native"),
            patch.object(NATIVE, "rank_catalog", return_value=None),
            patch.object(NATIVE, "rank_sqlite", wraps=NATIVE.rank_sqlite) as sqlite,
        ):
            hits = self.service.search(query, self.settings)
        sqlite.assert_called_once()
        self.assertEqual([hit.item.path.name for hit in hits], ["alpha report.txt"])

    def test_concurrent_searches_and_catalog_changes_stay_consistent(self):
        self.publish(*(f"report {index}.txt" for index in range(50)))
        errors: list[BaseException] = []
        stop = threading.Event()

        def search():
            try:
                with engine("native"):
                    while not stop.is_set():
                        hits = self.service.search(parse_query("rprt"), self.settings)
                        # Every snapshot holds 50 or 51 reports, never a mix of two.
                        self.assertIn(len(hits), (50, 51))
            except BaseException as exc:
                errors.append(exc)

        searchers = [threading.Thread(target=search) for _ in range(CONCURRENT_SEARCHERS)]
        for searcher in searchers:
            searcher.start()
        try:
            for change in range(CONCURRENT_CHANGES):
                extra = (f"extra {change}.txt",) if change % 2 else ()
                self.publish(*(f"report {index}.txt" for index in range(50)), *extra)
                with engine("native"):
                    native_engine.warm_search_index(self.store)
        finally:
            stop.set()
            for searcher in searchers:
                searcher.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.assert_engines_agree("rprt")), 50)

    def test_text_that_is_not_case_folded_falls_back_to_a_correct_search(self):
        self.publish("report.txt")
        with self.store.connect() as connection:
            connection.execute("UPDATE entries SET name = 'Report.txt'")
        with engine("native"):
            self.assertIsNone(native_engine.warm_search_index(self.store))
        with engine("auto"), patch.object(native_engine, "_LOGGED", set()):
            self.assertEqual(
                [
                    hit.item.path.name
                    for hit in self.service.search(parse_query("report"), self.settings)
                ],
                ["report.txt"],
            )

    def test_cancelling_a_memory_search_returns_empty_promptly(self):
        # Expensive fuzzy alignments keep the scan busy long enough to cancel it.
        # Their initials and text do not contain the term, so each needs a fuzzy alignment.
        self.publish(*(f"{index}x{'a_b_' * 50}.txt" for index in range(3000)))
        query = parse_query("ab" * 20)
        spec = native_engine.query_spec(query, Ranker(query), 10)
        rules = native_engine.priority_spec(PriorityPolicy())
        self.warm()
        flag = NATIVE.CancelFlag()
        entered = threading.Event()
        cancelled_at = []

        def allows(*_):
            entered.set()
            return True

        def cancel():
            if entered.wait(5):
                cancelled_at.append(time.perf_counter())
                flag.set()

        canceller = threading.Thread(target=cancel, daemon=True)
        canceller.start()
        result = NATIVE.rank_catalog(
            catalog_spec(self.store), memory_plan(query), spec, rules, allows, flag
        )
        returned_at = time.perf_counter()
        canceller.join(6)
        self.assertTrue(cancelled_at, "the canceller did not run during the scan")
        self.assertEqual(result, [])
        self.assertLess(returned_at - cancelled_at[0], CANCEL_LIMIT_S)


@unittest.skipUnless(NATIVE is not None, "Optional pathscout_native module is not installed")
class LikeParityTest(CatalogCase):
    def test_random_like_patterns_match_sqlite(self):
        generator = random.Random(LIKE_SEED)

        def text(alphabet):
            return "".join(generator.choices(alphabet, k=generator.randint(0, LIKE_MAX_LENGTH)))

        names = sorted({normalize(text(LIKE_NAME_ALPHABET)) + ".x" for _ in range(LIKE_NAMES)})
        self.publish(*(f"{index}/{name}" for index, name in enumerate(names)))
        with self.store.connect() as connection:
            for _ in range(LIKE_PATTERNS):
                pattern = text(LIKE_PATTERN_ALPHABET) + generator.choice(("", "%", "_"))
                expected = [
                    rowid
                    for (rowid,) in connection.execute(
                        "SELECT rowid FROM entries WHERE name LIKE ? ESCAPE '\\' ORDER BY rowid",
                        (pattern,),
                    )
                ]
                plan = {"literal": [("like", "name", pattern)], "fuzzy": None, "history_key": ""}
                with self.subTest(pattern=pattern):
                    selected = NATIVE.select_catalog(
                        catalog_spec(self.store), plan, NATIVE.CancelFlag()
                    )
                    self.assertEqual(selected, (expected, []))


if __name__ == "__main__":
    unittest.main()
