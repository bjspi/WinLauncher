"""On-demand content filtering must precede the result limit and stay cancellable."""

from __future__ import annotations

import importlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pathscout.catalog.model import CatalogItem, CatalogSource, ItemKind
from pathscout.catalog.priority import PriorityPolicy
from pathscout.catalog.store import CatalogStore
from pathscout.search.native_engine import API_VERSION, priority_spec, query_spec
from pathscout.search.query_parser import parse_query, without_type_filter
from pathscout.search.ranking import Ranker
from pathscout.search.service import SearchService
from pathscout.settings import Settings

try:
    NATIVE = importlib.import_module("pathscout_native")
except ImportError:
    NATIVE = None


class ContentQueryTest(unittest.TestCase):
    def test_content_query_validation_history_and_type_replacement(self):
        query = parse_query('*.json contains:"API_KEY=[ok]*" type:file')
        self.assertEqual(query.content_text, "API_KEY=[ok]*")
        self.assertEqual(query.terms, ("*.json",))
        self.assertIn("contains:api_key=[ok]*", query.history_key)
        self.assertFalse(parse_query("contains:hello").uses_learned_items_only)
        self.assertEqual(parse_query("contains:first contains:second").content_text, "second")
        self.assertEqual(
            parse_query(without_type_filter('*.json type:app contains:"API_KEY=[ok]*"')),
            parse_query('*.json contains:"API_KEY=[ok]*"'),
        )
        for text in ("contains:", 'contains:" "', "contains:" + "a" * 129):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "contains"):
                parse_query(text)

    def test_unavailable_native_backend_never_drops_the_content_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CatalogStore(Path(temporary) / "catalog.sqlite3")
            service = SearchService(store)
            query = parse_query("report contains:needle")
            with patch("pathscout.search.service.select_native", return_value=None):
                with self.assertRaisesRegex(ValueError, "requires the Rust backend"):
                    service.search(query, Settings(roots=()))
            with (
                patch("pathscout.search.service.select_native", return_value=object()),
                patch(
                    "pathscout.search.service.rank_with_native", side_effect=ValueError("fixture")
                ),
                self.assertLogs("pathscout.search.service", level="ERROR"),
                self.assertRaisesRegex(RuntimeError, "could not complete"),
            ):
                service.search(query, Settings(roots=()))


@unittest.skipUnless(
    NATIVE is not None and NATIVE.API_VERSION == API_VERSION, "Current native extension required"
)
class NativeContentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pathscout-content-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "library"
        self.root.mkdir()
        self.store = CatalogStore(self.base / "catalog.sqlite3")
        self.service = SearchService(self.store)
        self.settings = Settings(roots=(str(self.root),), max_results=10, index_apps=False)

    def publish(self, items):
        source = CatalogSource(self.root).id
        with self.store.snapshot() as connection:
            self.store.stage(connection, ((item, source) for item in items))
            self.store.publish(connection, active_roots=(source,), preserved_roots=())

    def search(self, text, phase):
        with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="native", PATHSCOUT_NATIVE_PHASE=phase):
            return self.service.search(parse_query(text), self.settings)

    def test_streams_supported_text_types_after_name_filters_before_the_limit(self):
        phrase = "API_KEY=(ok)+.{}"
        items, expected = [], set()
        for index in range(20):
            path = self.root / f"report{index:02}.json"
            path.write_text("unrelated" if index < 15 else phrase.lower(), encoding="utf-8")
            items.append(CatalogItem(path, ItemKind.FILE))
            if index >= 15:
                expected.add(path)
        for name, data in (
            ("report.md", ("# Heading\n" + phrase).encode()),
            ("report.txt", ("München\n" + phrase).encode("utf-16")),
            ("report.log", ("x" * 100000 + phrase).encode()),
            ("report-binary.txt", b"\0" + phrase.encode()),
            ("report.exe", phrase.encode()),
            ("report.mp4", phrase.encode()),
            ("report.docx", phrase.encode()),
            ("report.pdf", phrase.encode()),
            ("report-regex.txt", b"API_KEY=okkkkk"),
            ("other.txt", phrase.encode()),
            ("~$report.txt", phrase.encode()),
        ):
            path = self.root / name
            path.write_bytes(data)
            items.append(CatalogItem(path, ItemKind.FILE))
            if name in ("report.md", "report.txt", "report.log"):
                expected.add(path)
        huge = self.root / "report-large.txt"
        with huge.open("wb") as stream:
            stream.write(phrase.encode())
            stream.truncate(64 * 1024 * 1024 + 1)
        items.append(
            CatalogItem(huge, ItemKind.FILE, size=1)
        )  # Actual size overrides stale metadata.
        missing = self.root / "report-missing.txt"
        items.extend(
            (
                CatalogItem(missing, ItemKind.FILE),
                CatalogItem(self.root / "report-folder", ItemKind.FOLDER),
            )
        )
        self.publish(items)
        for phase in ("rows", "sqlite"):
            with self.subTest(phase=phase):
                hits = self.search(f'report contains:"{phrase}" sort:name', phase)
                self.assertEqual({hit.item.path for hit in hits}, expected)
                self.assertFalse(self.search('report??.json contains:"API_KEY=ok"', phase))
                self.assertFalse(self.search(f'type:folder contains:"{phrase}"', phase))
        changed = self.root / "report19.json"
        changed.write_text("changed after cataloging", encoding="utf-8")
        self.assertNotIn(
            changed, {hit.item.path for hit in self.search(f'report contains:"{phrase}"', "sqlite")}
        )

    def test_fnmatch_content_patterns_filter_both_native_phases(self):
        items = []
        for name, text in (
            ("a.txt", "prefix GSK_a7 suffix"),
            ("b.txt", "prefix gsk_bx suffix"),
            ("c.txt", "prefix gsk_ä5 suffix"),
            ("d.txt", "prefix gsk_[1]* suffix"),
            ("lines.txt", "gsk_\nend"),
            ("other.txt", "other value"),
        ):
            path = self.root / name
            path.write_text(text, encoding="utf-8")
            items.append(CatalogItem(path, ItemKind.FILE))
        self.publish(items)
        for phase in ("rows", "sqlite"):
            for pattern, expected in (
                ("gsk_*", {"a.txt", "b.txt", "c.txt", "d.txt", "lines.txt"}),
                ("gsk_?", {"a.txt", "b.txt", "c.txt", "d.txt"}),
                ("gsk_[a-z][0-9]", {"a.txt"}),
                ("gsk_[!0-9][0-9]", {"a.txt", "c.txt", "d.txt"}),
                ("gsk_[[]1][*]", {"d.txt"}),
                ("gsk_*end", set()),
            ):
                with self.subTest(phase=phase, pattern=pattern):
                    hits = self.search(f"contains:{pattern}", phase)
                    self.assertEqual({hit.item.path.name for hit in hits}, expected)

    def test_content_scan_releases_gil_and_cancels_between_read_chunks(self):
        path = self.root / "report.txt"
        path.write_bytes(b"x" * (8 * 1024 * 1024))
        self.publish([CatalogItem(path, ItemKind.FILE)])
        query = parse_query("report contains:missing")
        spec = query_spec(query, Ranker(query), 10)
        flag = NATIVE.CancelFlag()
        entered = threading.Event()
        cancelled_at = []

        def allows(*_):
            entered.set()
            return True

        def cancel_scan():
            if entered.wait(5):
                time.sleep(0.01)
                cancelled_at.append(time.perf_counter())
                flag.set()

        canceller = threading.Thread(target=cancel_scan, daemon=True)
        canceller.start()
        from pathscout.search.native_engine import _search_sql

        sql, parameters = _search_sql(query)
        # Repeat candidate reads so cancellation is exercised even on fast disks.
        sql = (
            "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<999) SELECT hits.* FROM ("
            + sql
            + ") hits CROSS JOIN n"
        )
        try:
            result = NATIVE.rank_sqlite(
                str(self.store.path),
                sql,
                parameters,
                spec,
                priority_spec(PriorityPolicy()),
                allows,
                flag,
            )
            returned_at = time.perf_counter()
        finally:
            flag.set()
            canceller.join(6)
        self.assertFalse(canceller.is_alive())
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled_at, "Python cancellation did not run during the content scan")
        self.assertGreaterEqual(returned_at, cancelled_at[0])
        self.assertLess(returned_at - cancelled_at[0], 0.5)
        self.assertEqual(result, [])
