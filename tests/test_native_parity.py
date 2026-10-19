"""The optional native loop must agree with the Python behavior reference."""

from __future__ import annotations

import importlib
import itertools
import os
import tempfile
import threading
import time
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from pathscout.catalog.exclusions import ExclusionPolicy
from pathscout.catalog.model import CatalogItem, CatalogSource, ItemKind
from pathscout.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    PriorityPolicy,
)
from pathscout.catalog.store import CatalogStore
from pathscout.search import native_engine
from pathscout.search.candidates import Candidate, _candidate_from_row, candidate_sql
from pathscout.search.native_engine import priority_spec, query_spec
from pathscout.search.query import MatchMode, ScoreMode, SearchQuery, SortOrder
from pathscout.search.query_parser import parse_query
from pathscout.search.ranking import Ranker
from pathscout.search.service import ResultOrder, SearchService, _BestHits
from pathscout.settings import Settings, settings_from_json

try:
    NATIVE = importlib.import_module("pathscout_native")
except ImportError:
    NATIVE = None

NOW = 1_735_689_600.0
ASCII_NAMES = (
    "report.txt",
    "Report-final.txt",
    "Annual Report.txt",
    "rare-export.txt",
    "Project Report.txt",
    "ProjectPlan.md",
    "VisualStudioCode.exe",
    "Visual Studio Code.lnk",
    "archive.tar.gz",
    "file.",
    ".hidden",
    "Folder.With.Dots",
    "100%_complete.txt",
    "mom's report.txt",
    "Report-REPORT.txt",
    "BudgetReview3.csv",
    "a" + "x" * 180 + "b.txt",
)
UNICODE_NAMES = (
    "Straße.txt",
    "Strasse.txt",
    "Café.txt",
    "Cafe\u0301.txt",
    "ǅuraNotes.txt",
    "ΔοκιμήReport.txt",
    "ПримерReport.txt",
    "İıſK.txt",
    "平方²Ⅷ.txt",
    "😀Report.txt",
    "ᲐReport.txt",
    "ﬃNotes.txt",
    "a\u0308CamelCase.txt",
)
QUERY_TEXTS = (
    "",
    "type:file",
    "type:folder",
    "type:app",
    "ext:txt,md",
    "report",
    "r",
    "rpt",
    "vsc",
    "project rep",
    '"annual report"',
    "name:report",
    "name:txt",
    "name:hidden",
    "name:file",
    "path:work/deep report",
    "path:deep/work report",
    "report !archive",
    "report after:2024-06-01 before:2024-12-15",
    "report low:true",
    '"100%_"',
    "mom's",
    "strasse",
    "café",
    "ǆ",
    "δοκ",
    "пример",
    "ffn",
    "äcc",
    "sra",
    "²ⅷ",
    "squared missing",
    "ab",
    "aab",
)


def corpus(root: Path, *, unicode: bool) -> list[CatalogItem]:
    names = (*ASCII_NAMES, *(UNICODE_NAMES if unicode else ()))
    folders = ("", "work", "archive", "work/deep", "work/deep/more", "ignored")
    items = []
    for index, name in enumerate(names):
        kind = (
            ItemKind.FOLDER
            if name == "Folder.With.Dots"
            else ItemKind.APP
            if name.endswith((".exe", ".lnk"))
            else ItemKind.FILE
        )
        items.append(CatalogItem(root / folders[index % len(folders)] / name, kind))
    items.extend(
        CatalogItem(root / folders[index % len(folders)] / f"{index:03d}-Report.txt", ItemKind.FILE)
        for index in range(230)
    )
    items.extend(
        CatalogItem(root / relative, ItemKind.FOLDER)
        for relative in ("work", "work/deep", "archive", "ignored")
    )
    items.extend(
        (
            CatalogItem(Path(r"\\server\share\Work\Report.txt"), ItemKind.FILE),
            CatalogItem(Path(r"C:\Parity\Work\Deep\Report.lnk"), ItemKind.APP),
        )
    )
    return [
        replace(
            item,
            size=index * 17,
            modified=NOW - index * 86400,
            attributes=(0, 2, 4, 6, 4096)[index % 5],
            pinned=index % 13 == 0,
            launches=index % 80 if index % 3 == 0 else 0,
            query_launches=index % 40 if index % 7 == 0 else 0,
            last_used=NOW + (3 - index % 20) * 86400 if index % 11 == 0 else 0,
        )
        for index, item in enumerate(items)
    ]


def row_for(item: CatalogItem) -> tuple:
    candidate = Candidate.from_item(item)
    return (
        candidate.path_text,
        candidate.identity,
        str(candidate.kind),
        candidate.search_name,
        candidate.search_path,
        candidate.extension,
        candidate.size,
        candidate.modified,
        candidate.attributes,
        candidate.launches,
        candidate.last_used,
        candidate.query_launches,
        int(candidate.pinned),
    )


def policy_for(root: Path, *, names: bool) -> PriorityPolicy:
    return PriorityPolicy(
        folders=(
            FolderPriority(str(root), 11, "high"),
            FolderPriority(str(root / "work"), 7),
            FolderPriority(str(root / "archive"), -100, "low"),
            FolderPriority(str(root / "ignored"), 0, "ignored"),
            FolderPriority(str(root / "work/deep"), 3, "high"),
            FolderPriority(str(root / "work/deep"), 100, "ignored"),
        ),
        names=(
            NamePriority(r"(?<=Annual )Report(?=\.txt$)", 4, "high"),
            NamePriority(r"^Report", -3),
            NamePriority(r"([A-Za-z]+)-\1", 5, "high"),
            NamePriority(r"^Folder", 0, "ignored", files=False),
        )
        if names
        else (),
        extensions=(
            ExtensionPriority("txt", 2),
            ExtensionPriority(".TXT", 1, "high"),
            ExtensionPriority("md", -4, "low"),
            ExtensionPriority("lnk", -2),
        ),
        attributes=(
            AttributePriority(2, -7, "low"),
            AttributePriority(4, 10, "high"),
            AttributePriority(4096, -20, "ignored"),
            AttributePriority(6, -13, "ignored"),
        ),
    )


def matrix(root: Path):
    for text in QUERY_TEXTS:
        base = parse_query(text)
        for sort, match, score, limit in itertools.product(
            SortOrder, MatchMode, ScoreMode, (10, 200)
        ):
            yield replace(base, sort=sort, match_mode=match, score_mode=score), limit
    for scope in (root, root / "work", root / "work/deep"):
        for text in ("", "report", "rpt"):
            for limit in (10, 200):
                yield parse_query(text, scope=scope), limit


def python_rows(rows, query, policy, limit, allows):
    """Run the actual Python ranking/heap objects over extension-format rows."""
    ranker = Ranker(query, now=NOW)
    order = ResultOrder(query, ranker)
    best = _BestHits(limit)
    indices = {}
    for index, row in enumerate(rows):
        candidate = _candidate_from_row(row)
        priority = policy.evaluate(candidate)
        if not ranker.eligible(candidate, priority):
            continue
        if not best.could_keep(order.best_possible_key(candidate, priority)):
            continue
        ranking = ranker.rank(candidate, priority)
        if ranking is None:
            continue
        key = order.key(candidate, ranking)
        if best.would_keep(key) and allows(candidate.path_text, candidate.kind == ItemKind.FOLDER):
            best.add(key, candidate, ranking)
            indices[id(candidate)] = index
    return [
        (
            indices[id(kept.candidate)],
            kept.ranking.score,
            int(kept.ranking.band),
            list(kept.ranking.reasons),
        )
        for kept in sorted(best._heap, key=lambda kept: kept.key)
    ]


def recorded_allows(calls):
    def allows(path, directory):
        calls.append((path, directory))
        return "007-Report" not in path and "035-Report" not in path

    return allows


class PythonParityHarnessTest(unittest.TestCase):
    def test_reference_harness_checks_scores_heap_and_callback_order(self):
        root = Path(tempfile.gettempdir()) / "native-reference"
        items = [
            CatalogItem(root / f"{index:02d}-Report.txt", ItemKind.FILE) for index in range(12)
        ]
        items.append(CatalogItem(root / "zz/Report.txt", ItemKind.FILE, pinned=True))
        rows = [row_for(item) for item in items]
        calls = []
        result = python_rows(
            rows, parse_query("report"), PriorityPolicy(), 10, recorded_allows(calls)
        )
        self.assertEqual(result[0], (12, 230.0, 1, ["Exact name", "Favorite"]))
        self.assertEqual(len(result), 10)
        self.assertEqual(
            [path for path, _ in calls],
            [
                item.path.as_posix() if os.name != "nt" else str(item.path)
                for item in (*items[:10], items[-1])
            ],
        )


@unittest.skipUnless(NATIVE is not None, "Optional pathscout_native module is not installed")
class NativeParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="pathscout-native-parity-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name) / "library"
        cls.root.mkdir()
        cls.items = corpus(cls.root, unicode=True)
        cls.store = CatalogStore(Path(cls.temporary.name) / "catalog.sqlite3")
        source = CatalogSource(cls.root).id
        with cls.store.snapshot() as connection:
            cls.store.stage(connection, ((item, source) for item in cls.items))
            cls.store.publish(connection, active_roots=(source,), preserved_roots=())
        with cls.store.connect() as connection:
            for item in cls.items:
                candidate = Candidate.from_item(item)
                if item.launches or item.last_used:
                    connection.execute(
                        "INSERT INTO usage VALUES(?,?,?)",
                        (candidate.identity, item.launches, item.last_used),
                    )
                if item.pinned:
                    connection.execute("INSERT INTO favorites VALUES(?)", (candidate.identity,))
                if item.query_launches:
                    for key in {parse_query(text).history_key for text in QUERY_TEXTS}:
                        connection.execute(
                            "INSERT INTO query_usage VALUES(?,?,?)",
                            (key, candidate.identity, item.query_launches),
                        )

    def assert_rankings_equal(self, expected, actual):
        self.assertEqual(len(expected), len(actual))
        for reference, native in zip(expected, actual, strict=True):
            self.assertEqual(reference[0], native[0])
            self.assertAlmostEqual(reference[1], native[1], delta=1e-9)
            self.assertEqual(reference[2:], native[2:])

    def test_native_api_and_unicode_versions_match(self):
        self.assertEqual(NATIVE.API_VERSION, native_engine.API_VERSION)
        self.assertEqual(NATIVE.UNICODE_VERSION, unicodedata.unidata_version)

    def test_raw_rows_match_python_with_unicode_and_every_priority_kind(self):
        for use_unicode, use_names in ((True, False), (False, True)):
            rows = [row_for(item) for item in corpus(self.root, unicode=use_unicode)]
            policy = policy_for(self.root, names=use_names)
            rules = priority_spec(policy)
            for query, limit in matrix(self.root):
                with self.subTest(unicode=use_unicode, query=query, limit=limit):
                    expected_calls, actual_calls = [], []
                    expected = python_rows(
                        rows, query, policy, limit, recorded_allows(expected_calls)
                    )
                    actual = NATIVE.rank_rows(
                        query_spec(query, Ranker(query, now=NOW), limit),
                        rules,
                        rows,
                        recorded_allows(actual_calls),
                        NATIVE.CancelFlag(),
                    )
                    self.assert_rankings_equal(expected, actual)
                    self.assertEqual(expected_calls, actual_calls)

    def test_sqlite_matches_python_and_preserves_literal_then_fuzzy_callbacks(self):
        policy = policy_for(self.root, names=False)
        rules = priority_spec(policy)
        for query, limit in matrix(self.root):
            with self.subTest(query=query, limit=limit):
                sql, parameters = native_engine._search_sql(query)
                with self.store.connect() as connection:
                    connection.row_factory = None
                    rows = list(connection.execute(*candidate_sql(query)))
                    if query.uses_fuzzy_pass:
                        rows.extend(connection.execute(*candidate_sql(query, fuzzy=True)))
                    self.assertEqual(rows, list(connection.execute(sql, parameters)))
                expected_calls, actual_calls = [], []
                expected = python_rows(rows, query, policy, limit, recorded_allows(expected_calls))
                expected = [
                    (rows[index], score, band, reasons) for index, score, band, reasons in expected
                ]
                actual = NATIVE.rank_sqlite(
                    str(self.store.path),
                    sql,
                    parameters,
                    query_spec(query, Ranker(query, now=NOW), limit),
                    rules,
                    recorded_allows(actual_calls),
                    NATIVE.CancelFlag(),
                )
                self.assert_rankings_equal(expected, actual)
                self.assertEqual(expected_calls, actual_calls)

    def test_service_both_phases_preserve_hits_explanations_and_regex_fallback(self):
        settings = Settings(
            roots=(str(self.root),), index_apps=False, marker_files=(), excluded_patterns=()
        )
        service = SearchService(self.store)
        scenarios = (
            ("report", settings),
            ("rpt low:true", settings),
            ("strasse", settings),
            (
                "report",
                replace(settings, extension_priorities=(ExtensionPriority("txt", 0, "low"),)),
            ),
            (
                "report low:true",
                replace(settings, extension_priorities=(ExtensionPriority("txt", 0, "low"),)),
            ),
            (
                "report score:match",
                replace(settings, name_priorities=(NamePriority("report", 7, "high"),)),
            ),
            (
                "report",
                replace(settings, name_priorities=(NamePriority(r"(?i:report)", 13, "low"),)),
            ),
            ("café", replace(settings, name_priorities=(NamePriority("café", 13, "high"),))),
        )
        for phase, (text, configured) in itertools.product(("rows", "sqlite"), scenarios):
            with self.subTest(phase=phase, text=text, names=configured.name_priorities):
                query = parse_query(text)
                with patch("pathscout.search.ranking.time.time", return_value=NOW):
                    with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="python"):
                        expected = service.search(query, configured)
                    with patch.dict(
                        os.environ, PATHSCOUT_SEARCH_ENGINE="native", PATHSCOUT_NATIVE_PHASE=phase
                    ):
                        actual = service.search(query, configured)
                self.assertEqual(expected, actual)

    def test_unicode_regex_fallback_never_repeats_earlier_ascii_exclusion_callbacks(self):
        settings = Settings(
            roots=(str(self.root),),
            index_apps=False,
            marker_files=(),
            excluded_patterns=(),
            name_priorities=(NamePriority("report", 3),),
        )
        service = SearchService(self.store)
        query = parse_query("report")
        for phase in ("rows", "sqlite"):
            calls = []

            def allows(_policy, path, *, directory=False, calls=calls):
                calls.append((str(path), directory))
                return True

            with (
                self.subTest(phase=phase),
                patch.object(ExclusionPolicy, "allows", allows),
                patch("pathscout.search.ranking.time.time", return_value=NOW),
            ):
                with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="python"):
                    expected = service.search(query, settings)
                expected_calls = calls[:]
                self.assertTrue(expected_calls)
                self.assertTrue(expected_calls[0][0].isascii())
                calls.clear()
                with patch.dict(
                    os.environ, PATHSCOUT_SEARCH_ENGINE="native", PATHSCOUT_NATIVE_PHASE=phase
                ):
                    actual = service.search(query, settings)
                self.assertEqual(expected, actual)
                self.assertEqual(expected_calls, calls)

    def test_unsupported_and_unicode_regex_cases_request_python(self):
        query = parse_query("report")
        spec = query_spec(query, Ranker(query, now=NOW), 10)
        for pattern, name in (
            (r"(?i:report)", "Report.txt"),
            (r"\u0130", "I.txt"),
            ("report", "StraßeReport.txt"),
        ):
            policy = PriorityPolicy(names=(NamePriority(pattern, 7),))
            rows = [row_for(CatalogItem(self.root / name, ItemKind.FILE))]
            with self.subTest(pattern=pattern, name=name), self.assertRaises(ValueError):
                NATIVE.rank_rows(
                    spec, priority_spec(policy), rows, lambda *_: True, NATIVE.CancelFlag()
                )

    def test_cancel_flag_returns_empty_promptly_and_releases_the_gil(self):
        query = SearchQuery(terms=("ab" * 20,))
        spec = query_spec(query, Ranker(query, now=NOW), 10)
        rules = priority_spec(PriorityPolicy())
        first = row_for(CatalogItem(self.root / ("a_b_" * 20 + ".txt"), ItemKind.FILE))
        expensive = row_for(
            CatalogItem(self.root / ("a" * 100 + "b" * 100 + ".txt"), ItemKind.FILE)
        )
        rows = [first, *([expensive] * 20_000)]
        for phase in ("rows", "sqlite"):
            with self.subTest(phase=phase):
                entered = threading.Event()
                flag = NATIVE.CancelFlag()
                cancelled_at = []

                def allows(*_, entered=entered):
                    entered.set()
                    return True

                def cancel_during_rank(entered=entered, cancelled_at=cancelled_at, flag=flag):
                    if entered.wait(5):
                        time.sleep(0.01)
                        cancelled_at.append(time.perf_counter())
                        flag.set()

                canceller = threading.Thread(target=cancel_during_rank, daemon=True)
                canceller.start()
                try:
                    if phase == "rows":
                        result = NATIVE.rank_rows(spec, rules, rows, allows, flag)
                    else:
                        columns = ",".join(f":r{index}" for index in range(13))
                        sql = f"WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<999999) SELECT {columns} FROM n"
                        parameters = {f"r{index}": value for index, value in enumerate(first)}
                        result = NATIVE.rank_sqlite(
                            str(self.store.path), sql, parameters, spec, rules, allows, flag
                        )
                    returned_at = time.perf_counter()
                finally:
                    flag.set()
                    canceller.join(6)
                self.assertFalse(canceller.is_alive())
                self.assertTrue(cancelled_at, "Python canceller did not run during native ranking")
                self.assertEqual(result, [])
                self.assertLess(returned_at - cancelled_at[0], 0.1)

    def test_already_cancelled_native_calls_return_empty(self):
        query = parse_query("report")
        spec = query_spec(query, Ranker(query, now=NOW), 10)
        rules = priority_spec(PriorityPolicy())
        flag = NATIVE.CancelFlag()
        flag.set()
        self.assertEqual(
            NATIVE.rank_rows(spec, rules, [row_for(self.items[0])], lambda *_: True, flag), []
        )
        sql, parameters = native_engine._search_sql(query)
        self.assertEqual(
            NATIVE.rank_sqlite(
                str(self.store.path), sql, parameters, spec, rules, lambda *_: True, flag
            ),
            [],
        )


class NativeAdapterFallbackTest(unittest.TestCase):
    def test_saved_backend_selection_and_environment_override(self):
        self.assertEqual(settings_from_json({"roots": []}).search_backend, "rust")
        for value in ("auto", "native", "unknown", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_from_json({"roots": [], "search_backend": value})
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(native_engine, "load_native", return_value=object()) as load,
        ):
            self.assertIsNone(native_engine.select_native("python"))
            load.assert_not_called()
            self.assertIsNotNone(native_engine.select_native("rust"))
            with patch.object(native_engine, "load_native", return_value=None):
                self.assertIsNone(native_engine.select_native("rust"))
            with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="python"):
                self.assertIsNone(native_engine.select_native("rust"))
            with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="native"):
                self.assertIsNotNone(native_engine.select_native("python"))

    def test_python_selection_does_not_import_the_optional_module(self):
        with (
            patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="python"),
            patch.object(
                native_engine.importlib, "import_module", side_effect=AssertionError("Imported")
            ),
        ):
            self.assertIsNone(native_engine.select_native())

    def test_missing_import_version_and_unicode_failures_are_logged_once(self):
        module = ModuleType("pathscout_native")
        module.API_VERSION = native_engine.API_VERSION
        module.UNICODE_VERSION = unicodedata.unidata_version
        cases = (
            {"side_effect": ModuleNotFoundError("missing", name="pathscout_native")},
            {"side_effect": ImportError("broken extension")},
            {"return_value": ModuleType("pathscout_native")},
            {
                "return_value": type(
                    "StaleUnicode",
                    (),
                    {"API_VERSION": native_engine.API_VERSION, "UNICODE_VERSION": "0.0.0"},
                )()
            },
        )
        for kwargs in cases:
            with (
                self.subTest(case=kwargs),
                patch.object(native_engine, "_LOGGED", set()),
                patch.object(native_engine.importlib, "import_module", **kwargs),
                patch.object(native_engine._LOGGER, "warning") as warning,
                patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="auto"),
            ):
                self.assertIsNone(native_engine.select_native())
                self.assertIsNone(native_engine.select_native())
                warning.assert_called_once()
        with patch.object(native_engine.importlib, "import_module", return_value=module):
            self.assertIs(native_engine.load_native(), module)

    def test_forced_native_fails_loudly_and_invalid_engine_is_rejected(self):
        with patch.object(native_engine, "load_native", return_value=None):
            with (
                patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="native"),
                self.assertRaises(RuntimeError),
            ):
                native_engine.select_native()
        with (
            patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="unknown"),
            self.assertRaises(ValueError),
        ):
            native_engine.select_native()

    def test_native_execution_failure_falls_back_to_reference_hits(self):
        with tempfile.TemporaryDirectory(prefix="pathscout-native-fallback-") as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog.sqlite3")
            source = CatalogSource(root).id
            item = CatalogItem(root / "Report.txt", ItemKind.FILE)
            with store.snapshot() as connection:
                store.stage(connection, ((item, source),))
                store.publish(connection, active_roots=(source,), preserved_roots=())
            settings = Settings(
                roots=(str(root),), index_apps=False, marker_files=(), excluded_patterns=()
            )
            service = SearchService(store)
            query = parse_query("report")
            with patch.dict(os.environ, PATHSCOUT_SEARCH_ENGINE="python"):
                expected = service.search(query, settings)
            self.assertTrue(expected)
            with (
                patch("pathscout.search.service.select_native", return_value=object()),
                patch(
                    "pathscout.search.service.rank_with_native",
                    side_effect=ValueError("unsupported regex"),
                ),
                patch.object(native_engine, "_LOGGED", set()),
                patch.object(native_engine._LOGGER, "warning") as warning,
            ):
                self.assertEqual(service.search(query, settings), expected)
                self.assertEqual(service.search(query, settings), expected)
                warning.assert_called_once()

    def test_threading_event_bridge_forwards_and_joins(self):
        fake = ModuleType("pathscout_native")
        fake.CancelFlag = threading.Event
        cancel = threading.Event()
        with native_engine._cancel_flag(fake, cancel) as flag:
            cancel.set()
            self.assertTrue(flag.wait(0.1))
        self.assertFalse(
            any(thread.name == "native-search-cancel" for thread in threading.enumerate())
        )


if __name__ == "__main__":
    unittest.main()
