"""The optional native loop must agree with the Python behavior reference."""

from __future__ import annotations

import importlib
import itertools
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
import unittest
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from ci_timing import PROMPT_LIMIT_S

from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.model import CatalogItem, CatalogSource, ItemKind, normalize
from winlauncher.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    PriorityPolicy,
)
from winlauncher.catalog.store import CatalogStore
from winlauncher.search import native_engine
from winlauncher.search.candidates import Candidate, candidate_from_row, candidate_sql
from winlauncher.search.matching import is_wildcard
from winlauncher.search.memory_index import catalog_spec, memory_plan
from winlauncher.search.native_engine import NativeAvailability, priority_spec, query_spec
from winlauncher.search.query import MatchMode, ScoreMode, SearchQuery, SortOrder
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import Ranker
from winlauncher.search.service import ResultOrder, SearchService, _BestHits
from winlauncher.settings import Settings

try:
    NATIVE = importlib.import_module("winlauncher_native")
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
    "report1.json",
    "reportA.json",
    "report[1].json",
    "report[.json",
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
    "ext:t*",
    "ext:(txt,md)",
    "ext:[!m]*",
    "ext:(jso?,t?t)",
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
    "report after:2024- before:2024-06*",
    "size:>1kb",
    "size:<1kbyte",
    "size:>=1kb size:<=2kb",
    "report size:>1kb size:<4kb",
    "*.json",
    '"*.json"',
    "report?.json",
    "report[0-9].json",
    "report[!0-9].json",
    "report[[]1].json",
    "report[.json",
    "**.json",
    "*.exe",
    "*.txt rpt",
    "*.txt ![archive]",
    "[!0-9]* size:>1kb",
    "name:report[0-9]",
    "path:[aw]* report*",
    "100%_*.txt",
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
    # Exact phrases, excluded extensions, `-` exclusions, ranges and folder shorthand.
    '"rpt"',
    '"vsc"',
    '"project rep"',
    '"report" rpt',
    '"port" "work"',
    "report !ext:md",
    "report -ext:(json;exe)",
    "-ext:t* rpt",
    "report -*.json",
    "!*.lnk !ext:j?on report",
    "report -archive",
    "size:1kb..2kb",
    "report size:100..1kb size:<512",
    "report date:2024",
    "date:2024-06..2024-12",
    "rpt date:>=2024-11 date:<2024-12-20",
    "report date:<=2024-03",
    "report after:2024-02 date:2024",
    r"work\deep\ report",
    "deep/ rpt",
    r"work\ !ext:txt",
    # Terms shorter than MIN_PATH_TERM_LENGTH match names only, alone and combined.
    "w",
    "wo",
    "de",
    "a b",
    "wo rpt",
    '"wo" report',
    "re *.txt",
    "path:wo re",
)
# fnmatch sets that Rust's regex syntax reads differently unless every member is
# escaped: `[` inside a set, `]` starting a range, reversed and edge ranges,
# set operators, unclosed or empty sets and multibyte members.
WILDCARD_SET_PATTERNS = (
    "[a[]",
    "[!![]",
    "x[ab[]",
    "[]-a]",
    "[!]-a]",
    "[z-a]",
    "[!z-a]",
    "[az-a]",
    "[-a]",
    "[a-]",
    "[--a]",
    "[a-b-c]",
    "[!]",
    "[]",
    "[ab",
    "[^a]",
    "[&&a]",
    "[a~~b]",
    "[a||b]",
    "[é]*",
    "[!é]*",
    "[é-ë]*",
    "*[🙂]",
    "[!🙂]?",
    "[🙂-🙃]*",
)
WILDCARD_SET_NAMES = (
    "[",
    "a",
    "b",
    "c",
    "x",
    "z",
    "!",
    "-",
    "^",
    "_",
    "]",
    "&",
    "~",
    "|",
    "x[",
    "xa",
    "[!]",
    "[]",
    "[ab",
    "é",
    "ê.txt",
    "ë",
    "🙂",
    "🙃x",
    "a🙂",
)
# The randomized differential test: deterministic, and small enough to run quickly.
WILDCARD_FUZZ_SEED = 20261020
WILDCARD_FUZZ_PATTERNS = 300
WILDCARD_FUZZ_NAMES = 40
WILDCARD_FUZZ_MAX_LENGTH = 6
WILDCARD_PATTERN_ALPHABET = "ab-]![*?^&é🙂"
WILDCARD_NAME_ALPHABET = "ab-]![^&é🙂"
WILDCARD_LIMIT = 200


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
        int(item.path.suffix == ".lnk"),  # Start-menu shortcuts come from app sources.
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
                # An absolute folder with a trailing separator scopes like `in:`.
                yield parse_query(f'"{scope}{os.sep}" {text}'), limit


def python_rows(rows, query, policy, limit, allows):
    """Run the actual Python ranking/heap objects over extension-format rows."""
    ranker = Ranker(query, now=NOW)
    order = ResultOrder(query, ranker)
    best = _BestHits(limit)
    indices = {}
    for index, row in enumerate(rows):
        candidate = candidate_from_row(row)
        priority = policy.evaluate(candidate)
        if not ranker.eligible(candidate, priority):
            continue
        if not best.could_keep(order.best_possible_key(candidate, priority)):
            continue
        ranking = ranker.rank(candidate, priority)
        if ranking is None:
            continue
        key = order.key(candidate, ranking)
        directory = candidate.kind == ItemKind.FOLDER
        if best.would_keep(key) and allows(
            candidate.path_text, directory, candidate.application_source
        ):
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
    def allows(path, directory, application):
        calls.append((path, directory, application))
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
            [call[0] for call in calls],
            [
                item.path.as_posix() if os.name != "nt" else str(item.path)
                for item in (*items[:10], items[-1])
            ],
        )


@unittest.skipUnless(NATIVE is not None, "Optional winlauncher_native module is not installed")
class NativeParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A prefix without the letters of the fuzzy queries below, which match whole paths.
        cls.temporary = tempfile.TemporaryDirectory(prefix="parity-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name) / "library"
        cls.root.mkdir()
        cls.items = corpus(cls.root, unicode=True)
        cls.store = CatalogStore(Path(cls.temporary.name) / "catalog.sqlite3")
        source = CatalogSource(cls.root).id
        with cls.store.snapshot() as connection:
            cls.store.stage(connection, ((item, source) for item in cls.items))
            cls.store.publish(connection, active_roots=(source,), preserved_roots=())
        NATIVE.use_unicode(unicodedata.unidata_version)
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
            self.assertEqual(reference[1], native[1])
            self.assertEqual(reference[2:], native[2:])

    def test_native_api_and_unicode_versions_match(self):
        self.assertEqual(NATIVE.API_VERSION, native_engine.API_VERSION)
        self.assertIn(unicodedata.unidata_version, NATIVE.UNICODE_VERSIONS)
        NATIVE.use_unicode(unicodedata.unidata_version)
        for version in (
            "0.0.0",
            *(v for v in NATIVE.UNICODE_VERSIONS if v != unicodedata.unidata_version),
        ):
            with self.subTest(version=version), self.assertRaises(ValueError):
                NATIVE.use_unicode(version)

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

    def sql_rowids(self, query, fuzzy):
        with self.store.connect() as connection:
            rowids = dict(connection.execute("SELECT path, rowid FROM entries"))
            connection.row_factory = None
            rows = connection.execute(*candidate_sql(query, fuzzy=fuzzy))
            return sorted(rowids[row[0]] for row in rows)

    def test_memory_index_selects_exactly_the_sql_candidates_of_each_pass(self):
        queries = dict.fromkeys(query for query, _ in matrix(self.root))
        for query in queries:
            with self.subTest(query=query):
                literal, fuzzy = native_engine.select_memory_candidates(NATIVE, self.store, query)
                self.assertEqual(literal, sorted(literal), "scans run in rowid order")
                self.assertEqual(fuzzy, sorted(fuzzy))
                self.assertEqual(literal, self.sql_rowids(query, fuzzy=False))
                expected_fuzzy = self.sql_rowids(query, fuzzy=True) if query.uses_fuzzy_pass else []
                self.assertEqual(fuzzy, expected_fuzzy)

    def test_memory_index_ranks_exactly_like_python_over_sql_rows(self):
        policy = policy_for(self.root, names=True)
        rules = priority_spec(policy)
        for query, limit in matrix(self.root):
            with self.subTest(query=query, limit=limit):
                with self.store.connect() as connection:
                    connection.row_factory = None
                    rows = list(connection.execute(*candidate_sql(query)))
                    if query.uses_fuzzy_pass:
                        rows.extend(connection.execute(*candidate_sql(query, fuzzy=True)))
                expected = python_rows(rows, query, policy, limit, recorded_allows([]))
                expected = [
                    (rows[index], score, band, reasons) for index, score, band, reasons in expected
                ]
                actual = NATIVE.rank_catalog(
                    catalog_spec(self.store),
                    memory_plan(query),
                    query_spec(query, Ranker(query, now=NOW), limit),
                    rules,
                    recorded_allows([]),
                    NATIVE.CancelFlag(),
                )
                self.assert_rankings_equal(expected, actual)

    def test_service_both_phases_preserve_hits_explanations_and_regex_fallback(self):
        settings = Settings(
            roots=(str(self.root),),
            index_apps=False,
            index_windows_tools=False,
            marker_files=(),
            excluded_patterns=(),
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
        phases = ("rows", "sqlite", "memory")
        for phase, (text, configured) in itertools.product(phases, scenarios):
            with self.subTest(phase=phase, text=text, names=configured.name_priorities):
                query = parse_query(text)
                with patch("winlauncher.search.ranking.time.time", return_value=NOW):
                    with patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="python"):
                        expected = service.search(query, configured)
                    with patch.dict(
                        os.environ,
                        WINLAUNCHER_SEARCH_ENGINE="native",
                        WINLAUNCHER_NATIVE_PHASE=phase,
                    ):
                        actual = service.search(query, configured)
                self.assertEqual(expected, actual)

    def test_unicode_regex_fallback_never_repeats_earlier_ascii_exclusion_callbacks(self):
        settings = Settings(
            roots=(str(self.root),),
            index_apps=False,
            index_windows_tools=False,
            marker_files=(),
            excluded_patterns=(),
            name_priorities=(NamePriority("report", 3),),
        )
        service = SearchService(self.store)
        query = parse_query("report")
        for phase in ("rows", "sqlite"):
            calls = []

            def allows(_policy, path, *, directory=False, application=False, calls=calls):
                calls.append((str(path), directory, application))
                return True

            with (
                self.subTest(phase=phase),
                patch.object(ExclusionPolicy, "allows", allows),
                patch("winlauncher.search.ranking.time.time", return_value=NOW),
            ):
                with patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="python"):
                    expected = service.search(query, settings)
                expected_calls = calls[:]
                self.assertTrue(expected_calls)
                self.assertTrue(expected_calls[0][0].isascii())
                calls.clear()
                with patch.dict(
                    os.environ, WINLAUNCHER_SEARCH_ENGINE="native", WINLAUNCHER_NATIVE_PHASE=phase
                ):
                    actual = service.search(query, settings)
                self.assertEqual(expected, actual)
                self.assertEqual(expected_calls, calls)

    def test_rules_rust_cannot_match_exactly_use_the_python_pattern(self):
        query = parse_query("report")
        spec = query_spec(query, Ranker(query, now=NOW), 10)
        for pattern, name in (
            (r"(?i:report)", "Report.txt"),
            (r"İ", "İReport.txt"),
            ("report", "StraßeReport.txt"),
            ("e", "Café Report.txt"),
        ):
            policy = PriorityPolicy(names=(NamePriority(pattern, 7, "high"),))
            rows = [row_for(CatalogItem(self.root / name, ItemKind.FILE))]
            with self.subTest(pattern=pattern, name=name):
                expected = python_rows(rows, query, policy, 10, lambda *_: True)
                actual = NATIVE.rank_rows(
                    spec, priority_spec(policy), rows, lambda *_: True, NATIVE.CancelFlag()
                )
                self.assertEqual(expected, actual)
                self.assertIn("High search priority", expected[0][3])

    def assert_wildcard_parity(self, pattern, names):
        """Native and Python rank `names` alike and keep exactly fnmatch's matches."""
        query = SearchQuery(terms=(normalize(pattern),))
        rows = [row_for(CatalogItem(self.root / name, ItemKind.FILE)) for name in names]
        expected = python_rows(rows, query, PriorityPolicy(), WILDCARD_LIMIT, lambda *_: True)
        actual = NATIVE.rank_rows(
            query_spec(query, Ranker(query, now=NOW), WILDCARD_LIMIT),
            priority_spec(PriorityPolicy()),
            rows,
            lambda *_: True,
            NATIVE.CancelFlag(),
        )
        self.assertEqual(expected, actual)
        matched = [
            index
            for index, name in enumerate(names)
            if fnmatchcase(normalize(name), normalize(pattern))
        ]
        self.assertEqual(sorted(index for index, *_ in actual), matched)

    def test_wildcard_sets_match_python_fnmatch(self):
        for pattern in WILDCARD_SET_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assert_wildcard_parity(pattern, WILDCARD_SET_NAMES)

    def test_random_wildcards_match_python_fnmatch(self):
        generator = random.Random(WILDCARD_FUZZ_SEED)

        def text(alphabet):
            length = generator.randint(1, WILDCARD_FUZZ_MAX_LENGTH)
            return "".join(generator.choices(alphabet, k=length))

        patterns = (text(WILDCARD_PATTERN_ALPHABET) for _ in itertools.count())
        for pattern in itertools.islice(filter(is_wildcard, patterns), WILDCARD_FUZZ_PATTERNS):
            names = sorted({text(WILDCARD_NAME_ALPHABET) for _ in range(WILDCARD_FUZZ_NAMES)})
            with self.subTest(pattern=pattern):
                self.assert_wildcard_parity(pattern, names)

    def test_new_filters_narrow_native_results_exactly_like_python(self):
        """Parity alone would also pass if both engines ignored a filter."""
        rows = [row_for(item) for item in corpus(self.root, unicode=False)]
        policy = PriorityPolicy()
        work = normalize(str(self.root / "work")) + os.sep
        for text, keeps in (
            ('"rpt"', lambda row: False),
            ('"vsc"', lambda row: False),
            ('"project rep"', lambda row: "project rep" in row[3]),
            ("report -ext:(json;lnk)", lambda row: row[5] not in ("json", "lnk")),
            ("report !*.json", lambda row: row[5] != "json"),
            ("report -ext:j?on", lambda row: row[5] != "json"),
            ("size:1kb..2kb", lambda row: 1024 <= row[6] <= 2048 and row[2] != "folder"),
            (
                "date:2024-06..2024-12",
                lambda row: (2024, 6) <= time.localtime(row[7])[:2] <= (2024, 12),
            ),
            # A fuzzy folder part matches its letters in order anywhere in the path.
            ("deep/ rpt", lambda row: re.search("d.*e.*e.*p", row[4]) is not None),
            (f'"{self.root / "work"}{os.sep}" report', lambda row: row[4].startswith(work)),
        ):
            query = parse_query(text)
            with self.subTest(text=text):
                expected = python_rows(rows, query, policy, WILDCARD_LIMIT, lambda *_: True)
                actual = NATIVE.rank_rows(
                    query_spec(query, Ranker(query, now=NOW), WILDCARD_LIMIT),
                    priority_spec(policy),
                    rows,
                    lambda *_: True,
                    NATIVE.CancelFlag(),
                )
                self.assertEqual(expected, actual)
                self.assertTrue(all(keeps(rows[index]) for index, *_ in actual))
                self.assertEqual(bool(actual), text not in ('"rpt"', '"vsc"'))
        for fuzzy_text in ("rpt", "vsc", "project rep"):
            with self.subTest(text=fuzzy_text):
                query = parse_query(fuzzy_text)
                self.assertTrue(python_rows(rows, query, policy, 10, lambda *_: True))

    def test_invalid_native_size_bounds_are_rejected(self):
        spec = query_spec(parse_query("report"), Ranker(parse_query("report")), 10)
        for lower, upper in ((-1, None), (None, -1), (10, 9), (2**63, None)):
            with (
                self.subTest(lower=lower, upper=upper),
                self.assertRaises((ValueError, OverflowError)),
            ):
                NATIVE.rank_rows(
                    {**spec, "min_size": lower, "max_size": upper},
                    priority_spec(PriorityPolicy()),
                    [],
                    lambda *_: True,
                    NATIVE.CancelFlag(),
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
                        columns = ",".join(f":r{index}" for index in range(14))
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
                self.assertLess(returned_at - cancelled_at[0], PROMPT_LIMIT_S)

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
    def setUp(self):
        native_engine.native_availability.cache_clear()
        self.addCleanup(native_engine.native_availability.cache_clear)

    def test_environment_selects_the_engine(self):
        module = ModuleType("winlauncher_native")
        available = NativeAvailability(module)
        missing = NativeAvailability(None, "The Rust search engine is not installed.")
        with patch.object(native_engine, "native_availability", return_value=available) as load:
            with patch.dict(os.environ, {}, clear=True):
                self.assertIs(native_engine.select_native(), module)
            with patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="native"):
                self.assertIs(native_engine.select_native(), module)
            load.reset_mock()
            with patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="python"):
                self.assertIsNone(native_engine.select_native())
            load.assert_not_called()
        with (
            patch.object(native_engine, "native_availability", return_value=missing),
            patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="auto"),
        ):
            self.assertIsNone(native_engine.select_native())
            self.assertFalse(native_engine.native_required())

    def test_python_selection_does_not_import_the_optional_module(self):
        with (
            patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="python"),
            patch.object(
                native_engine.importlib, "import_module", side_effect=AssertionError("Imported")
            ),
        ):
            self.assertIsNone(native_engine.select_native())

    def test_missing_import_version_and_unicode_failures_are_reported_once(self):
        def stale_unicode(version):
            raise ValueError(f"Unicode {version} is not supported")

        module = ModuleType("winlauncher_native")
        module.API_VERSION = native_engine.API_VERSION
        module.use_unicode = lambda version: None
        stale = ModuleType("winlauncher_native")
        stale.API_VERSION = native_engine.API_VERSION
        stale.use_unicode = stale_unicode
        old = ModuleType("winlauncher_native")
        old.API_VERSION = 1
        cases = (
            {"side_effect": ModuleNotFoundError("missing", name="winlauncher_native")},
            {"side_effect": ModuleNotFoundError("missing", name="sqlite3")},
            {"side_effect": ImportError("broken extension")},
            {"return_value": ModuleType("winlauncher_native")},
            {"return_value": old},
            {"return_value": stale},
        )
        for kwargs in cases:
            native_engine.native_availability.cache_clear()
            with (
                self.subTest(case=kwargs),
                patch.object(native_engine.importlib, "import_module", **kwargs),
                patch.object(native_engine._LOGGER, "warning") as warning,
                patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="auto"),
            ):
                self.assertIsNone(native_engine.select_native())
                self.assertIsNone(native_engine.select_native())
                self.assertTrue(native_engine.native_availability().reason)
                warning.assert_called_once()
        native_engine.native_availability.cache_clear()
        with patch.object(native_engine.importlib, "import_module", return_value=module):
            self.assertEqual(native_engine.native_availability(), NativeAvailability(module))

    def test_forced_native_fails_loudly_and_invalid_engine_is_rejected(self):
        missing = NativeAvailability(None, "The Rust search engine is not installed.")
        with patch.object(native_engine, "native_availability", return_value=missing):
            with (
                patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="native"),
                self.assertRaisesRegex(RuntimeError, "not installed"),
            ):
                native_engine.select_native()
        with (
            patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="unknown"),
            self.assertRaises(ValueError),
        ):
            native_engine.select_native()

    def test_forced_native_execution_failure_is_not_hidden(self):
        with tempfile.TemporaryDirectory(prefix="winlauncher-native-forced-") as temporary:
            store = CatalogStore(Path(temporary) / "catalog.sqlite3")
            settings = Settings(
                roots=(),
                index_apps=False,
                index_windows_tools=False,
                marker_files=(),
                excluded_patterns=(),
            )
            with (
                patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="native"),
                patch("winlauncher.search.service.select_native", return_value=object()),
                patch(
                    "winlauncher.search.service.rank_with_native",
                    side_effect=ValueError("unsupported regex"),
                ),
                self.assertRaisesRegex(ValueError, "unsupported regex"),
            ):
                SearchService(store).search(parse_query("report"), settings)

    def test_native_execution_failure_falls_back_to_reference_hits(self):
        with tempfile.TemporaryDirectory(prefix="winlauncher-native-fallback-") as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog.sqlite3")
            source = CatalogSource(root).id
            item = CatalogItem(root / "Report.txt", ItemKind.FILE)
            with store.snapshot() as connection:
                store.stage(connection, ((item, source),))
                store.publish(connection, active_roots=(source,), preserved_roots=())
            settings = Settings(
                roots=(str(root),),
                index_apps=False,
                index_windows_tools=False,
                marker_files=(),
                excluded_patterns=(),
            )
            service = SearchService(store)
            query = parse_query("report")
            with patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="python"):
                expected = service.search(query, settings)
            self.assertTrue(expected)
            with (
                patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE="auto"),
                patch("winlauncher.search.service.select_native", return_value=object()),
                patch(
                    "winlauncher.search.service.rank_with_native",
                    side_effect=ValueError("unsupported regex"),
                ),
                patch.object(native_engine, "_LOGGED", set()),
                patch.object(native_engine._LOGGER, "warning") as warning,
            ):
                self.assertEqual(service.search(query, settings), expected)
                self.assertEqual(service.search(query, settings), expected)
                warning.assert_called_once()

    def test_threading_event_bridge_forwards_and_joins(self):
        fake = ModuleType("winlauncher_native")
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
