"""Behavior checks for search quality, index consistency and keyboard gestures."""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from winlauncher.actions import ExplorerTarget, ItemActions
from winlauncher.catalog import long_paths
from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.indexer import CatalogIndexer
from winlauncher.catalog.model import (
    CatalogItem,
    CatalogSource,
    ItemKind,
    is_within,
    path_key,
)
from winlauncher.catalog.priority import (
    AttributePriority,
    ExtensionPriority,
    FolderPriority,
    NamePriority,
    PriorityPolicy,
    SearchPriority,
)
from winlauncher.catalog.store import CatalogStore
from winlauncher.platform.gesture import DoubleCtrlGesture
from winlauncher.search.candidates import Candidate
from winlauncher.search.matching import fuzzy_score
from winlauncher.search.query import MatchMode
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import rank
from winlauncher.search.service import SearchService
from winlauncher.settings import Settings, SettingsFile


class BusinessRulesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "library"
        self.root.mkdir()
        self.store = CatalogStore(self.base / "state/catalog.sqlite3")
        self.indexer = CatalogIndexer(self.store)
        self.settings = Settings(
            roots=(str(self.root),), index_apps=False, index_windows_tools=False
        )
        self.search = SearchService(self.store)

    def file(self, relative: str, text: str = "test") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self, **kwargs):
        report = self.indexer.scan(self.settings, **kwargs)
        self.assertFalse(report.cancelled)
        self.assertFalse(report.errors, report.errors)
        return report

    def hits(self, text: str, settings: Settings | None = None):
        return self.search.search(parse_query(text), settings or self.settings)

    def test_marker_prunes_directory_and_subtree_from_storage(self):
        visible = self.file("visible.txt")
        self.file("private/.nobackup", "")
        self.file("private/nested/secret.txt")
        self.file("other/.no-backup", "")
        self.file("other/secret.txt")
        self.scan()
        with self.store.connect() as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM entries")]
        # The configured folder itself is an entry, too.
        self.assertEqual(paths, [str(self.root), str(visible)])

    def test_new_marker_hides_old_results_and_subtree_refresh_removes_them(self):
        self.file("private/secret.txt")
        self.file("public.txt")
        self.scan()
        self.assertTrue(self.hits("secret"))
        self.file("private/.nobackup", "")
        self.assertFalse(self.hits("secret"))
        self.scan(scope=self.root / "private")
        with self.store.connect() as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM entries")]
        self.assertFalse(any("private" in path for path in paths))
        self.assertTrue(self.hits("public"))

    def test_default_exclusions_skip_caches_builds_and_environments(self):
        for generated in (
            ".ruff_cache/report.txt",
            ".mypy_cache/report.txt",
            "app/__pycache__/report.pyc",
            "app/build/report.txt",
            "app/dist/report.txt",
            "android/.gradle/report.txt",
            "pkg/winlauncher.egg-info/report.txt",
            "env/Lib/report.txt",
            "conda-env/Lib/report.txt",
        ):
            self.file(generated)
        self.file("env/pyvenv.cfg")
        (self.root / "conda-env/conda-meta").mkdir()
        kept = {self.file("buildings/report.txt"), self.file("Build Notes/report.txt")}
        self.scan()
        self.assertEqual({hit.item.path for hit in self.hits("report ext:txt")}, kept)

    def test_exclusion_uses_path_boundaries_and_markers_above_scan_root(self):
        policy = ExclusionPolicy(paths=(str(self.root / "cache"),))
        self.assertFalse(policy.allows(self.root / "cache/file.txt"))
        self.assertTrue(policy.allows(self.root / "cache-copy/file.txt"))
        self.assertFalse(is_within(self.root / "cache-copy", self.root / "cache"))
        (self.base / ".nobackup").write_text("")
        self.assertFalse(ExclusionPolicy().allows(self.root / "anything.txt"))

    def test_path_patterns_match_folder_levels_with_either_separator(self):
        def allows(pattern: str, path: str) -> bool:
            return ExclusionPolicy(patterns=(pattern,), markers=()).allows(Path(path))

        one_level = r"Projects\Android\*\build"
        self.assertFalse(allows(one_level, r"D:\Projects\Android\App\build\out\a.txt"))
        self.assertFalse(allows(one_level, r"E:\Work\projects\android\App\BUILD\a.txt"))
        self.assertTrue(allows(one_level, r"D:\Projects\Android\App\sub\build\a.txt"))
        self.assertTrue(allows(one_level, r"D:\Projects\Android\build\a.txt"))
        self.assertTrue(allows(one_level, r"D:\MyProjects\Android\App\build\a.txt"))
        any_depth = "Android/**/build"
        self.assertFalse(allows(any_depth, r"E:\Android\build\a.txt"))
        self.assertFalse(allows(any_depth, r"E:\Android\a\b\c\build\a.txt"))
        self.assertTrue(allows(any_depth, r"E:\Android\a\build2\a.txt"))
        anchored = "D:/Tools/x?z"
        self.assertFalse(allows(anchored, r"D:\Tools\xyz\a.txt"))
        self.assertTrue(allows(anchored, r"E:\Tools\xyz\a.txt"))
        self.assertTrue(allows(anchored, r"D:\Other\Tools\xyz\a.txt"))
        self.assertFalse(allows(r"\\server\share\[!a]*", r"\\server\share\bcd\a.txt"))
        self.assertTrue(allows(r"\\server\share\[!a]*", r"\\server\share\abc\a.txt"))
        # Like fnmatch, a reversed range matches nothing instead of breaking the scan.
        self.assertTrue(allows("Work/[z-a]", r"D:\Work\m\a.txt"))
        self.assertFalse(allows("Work/[!z-a]", r"D:\Work\m\a.txt"))
        self.assertFalse(allows("Work/[]x]", r"D:\Work\]\a.txt"))

    def test_cancelled_scan_preserves_entire_previous_snapshot(self):
        self.file("original.txt")
        self.scan()
        previous = self.store.count()
        for index in range(510):
            self.file(f"new-{index}.txt")
        cancel = threading.Event()

        def stop_after_batch(count):
            self.assertEqual(self.store.count(), previous)
            cancel.set()

        report = self.indexer.scan(self.settings, cancel=cancel, progress=stop_after_batch)
        self.assertTrue(report.cancelled)
        self.assertEqual(self.store.count(), previous)
        self.assertFalse(self.hits("new-"))
        self.assertTrue(self.hits("original"))

    def test_pre_cancelled_empty_scan_cannot_erase_catalog(self):
        self.file("original.txt")
        self.scan()
        cancel = threading.Event()
        cancel.set()
        report = self.indexer.scan(replace(self.settings, roots=()), cancel=cancel)
        self.assertTrue(report.cancelled)
        self.assertEqual(self.store.count(), 2)  # The configured folder and its file.

    def test_unavailable_root_preserves_previous_snapshot(self):
        self.file("original.txt")
        self.scan()
        with patch(
            "winlauncher.catalog.indexer.os.scandir", side_effect=PermissionError("unavailable")
        ):
            report = self.indexer.scan(self.settings)
        self.assertTrue(report.errors)
        self.assertEqual(self.store.count(), 2)  # The configured folder and its file.

    def test_renames_and_deletions_reconcile_without_losing_usage(self):
        path = self.file("project/report.txt")
        keep = self.file("other/keep.txt")
        self.scan()
        self.store.record_launch(keep, "keep")
        path.rename(path.with_name("renamed.txt"))
        self.scan(scope=path.parent)
        self.assertFalse(self.hits("name:report type:file"))
        self.assertTrue(self.hits("name:renamed"))
        self.assertEqual(self.hits("keep")[0].item.launches, 1)

    def test_exact_prefix_word_and_fuzzy_match_quality(self):
        query = parse_query("report")
        items = [
            CatalogItem(self.root / name, ItemKind.FILE)
            for name in ("report.txt", "report-final.txt", "annual report.txt", "rare-export.txt")
        ]
        scores = [rank(item, query).score for item in items]
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])
        self.assertGreater(fuzzy_score("abc", "abc.txt"), fuzzy_score("abc", "a___b___c.txt"))
        self.assertIsNone(fuzzy_score("zz", "report.txt"))

    def test_abbreviations_camel_case_and_keyword_mode(self):
        self.file("VisualStudioCode.exe")
        self.file("Visual Studio Code.url")
        self.scan()
        self.assertEqual(len(self.hits("vsc")), 2)
        self.assertFalse(self.hits("vsc match:keyword"))
        self.assertEqual(parse_query("match:keyword").match_mode, MatchMode.KEYWORD)

    def test_default_link_and_office_exclusions_hide_old_rows_and_prune_on_refresh(self):
        links = {
            self.file("Report Tool.lnk"),
            self.file("work/Report.LNK"),
            self.file("~$Report.docx"),
        }
        kept = self.file("Report.txt")
        self.settings = replace(
            self.settings,
            excluded_patterns=tuple(
                p for p in self.settings.excluded_patterns if p not in ("*.lnk", "~$*")
            ),
        )
        self.scan()
        self.assertEqual({hit.item.path for hit in self.hits("report")}, links | {kept})
        self.settings = replace(
            self.settings, excluded_patterns=(*self.settings.excluded_patterns, "*.lnk", "~$*")
        )
        self.assertEqual([hit.item.path for hit in self.hits("report")], [kept])
        self.scan()
        with self.store.connect() as connection:
            paths = {Path(row[0]) for row in connection.execute("SELECT path FROM entries")}
        self.assertTrue(links.isdisjoint(paths))
        self.assertIn(kept, paths)

    def test_learning_favorites_and_folder_boost_change_order(self):
        first = self.file("a/Report.txt")
        second = self.file("b/Report.txt")
        self.scan()
        self.assertEqual(self.hits("report")[0].item.path, first)
        self.store.record_launch(second, "report")
        self.assertEqual(self.hits("report")[0].item.path, second)
        self.store.set_favorite(first, True)
        self.assertEqual(self.hits("report")[0].item.path, first)
        settings = replace(
            self.settings, folder_priorities=(FolderPriority(str(second.parent), 100),)
        )
        self.assertEqual(self.hits("report", settings)[0].item.path, second)

    def test_search_class_is_separate_from_numeric_sort_boost(self):
        self.file("Report.txt")
        self.file("Report.bak")
        self.settings = replace(
            self.settings, extension_priorities=(ExtensionPriority("bak", 100, "low"),)
        )
        self.scan()
        hits = self.hits("report")
        self.assertEqual([hit.item.path.suffix for hit in hits], [".txt"])
        hits = self.hits("report low:true")
        self.assertEqual([hit.item.path.suffix for hit in hits], [".txt", ".bak"])
        (self.root / "Report.txt").unlink()
        self.scan()
        self.assertTrue(self.hits("report")[0].low_priority)

    def test_ignored_rules_win_over_favorites_and_high_priority(self):
        path = self.file("secret.txt")
        self.scan()
        self.store.set_favorite(path, True)
        self.settings = replace(
            self.settings,
            name_priorities=(NamePriority("secret", 100, "ignored"),),
            folder_priorities=(FolderPriority(str(self.root), 100, "high"),),
        )
        self.assertFalse(self.hits("secret"))
        self.scan()
        with self.store.connect() as connection:
            paths = [row[0] for row in connection.execute("SELECT path FROM entries")]
        self.assertEqual(paths, [str(self.root)])  # Only the configured folder itself.

    def test_ignored_start_menu_folder_prunes_application_descendants(self):
        self.file("Hidden Apps/Secret.exe")
        visible = self.file("Visible.exe")
        settings = replace(
            self.settings,
            roots=(),
            index_apps=True,
            name_priorities=(NamePriority("^Hidden Apps$", 0, "ignored", files=False),),
        )
        report = self.indexer.scan(settings, application_roots=(self.root,))
        self.assertFalse(report.errors)
        hits = self.search.search(parse_query("ext:exe"), settings)
        self.assertEqual([hit.item.path for hit in hits], [visible])

    def test_start_menu_shortcuts_survive_the_default_lnk_exclusion(self):
        start_menu = self.base / "Start Menu"
        shortcut = start_menu / "Programs" / "Editor.lnk"
        shortcut.parent.mkdir(parents=True)
        shortcut.write_text("shortcut", encoding="utf-8")
        self.file("Editor notes.lnk")
        settings = replace(self.settings, index_apps=True)
        self.assertIn("*.lnk", settings.excluded_patterns)
        report = self.indexer.scan(settings, application_roots=(start_menu,))
        self.assertFalse(report.errors)
        for engine in ("python", "auto"):
            with (
                self.subTest(engine=engine),
                patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE=engine),
            ):
                hits = self.search.search(parse_query("editor"), settings)
                self.assertEqual([hit.item.path for hit in hits], [shortcut])

    def test_attribute_priority_and_most_specific_folder_rule(self):
        item = CatalogItem(self.root / "nested/file.txt", ItemKind.FILE, attributes=2)
        policy = PriorityPolicy(
            folders=(FolderPriority(str(self.root), 30), FolderPriority(str(item.path.parent), 5)),
            attributes=(AttributePriority(2, -10, "low"),),
        )
        decision = policy.evaluate(item)
        self.assertEqual(decision.boost, -5)
        self.assertEqual(decision.search, SearchPriority.LOW)

    def test_low_rule_is_not_undone_by_a_high_rule(self):
        item = CatalogItem(self.root / "report.bak", ItemKind.FILE)
        policy = PriorityPolicy(
            folders=(FolderPriority(str(self.root), 50, "high"),),
            extensions=(ExtensionPriority("bak", 100, "low"),),
        )
        self.assertEqual(policy.evaluate(item).search, SearchPriority.LOW)

    def test_combined_and_text_only_scoring_have_distinct_order(self):
        exact = self.file("report.txt")
        boosted = self.file("preferred/Annual Report.txt")
        self.settings = replace(
            self.settings, folder_priorities=(FolderPriority(str(boosted.parent), 0, "high"),)
        )
        self.scan()
        self.assertEqual(self.hits("report")[0].item.path, boosted)
        self.assertEqual(self.hits("report score:match")[0].item.path, exact)

    def test_filename_only_search_ignores_parent_and_extension(self):
        self.file("report/document.txt")
        self.scan()
        self.assertFalse(self.hits("name:report type:file"))
        self.assertFalse(self.hits("name:txt match:keyword"))
        self.assertTrue(self.hits("name:document"))

    def test_search_cancellation_does_not_corrupt_store(self):
        self.file("report.txt")
        self.scan()
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(
            self.search.search(parse_query("report"), self.settings, cancel=cancel), []
        )
        self.assertTrue(self.hits("report"))

    def test_fnmatch_patterns_use_filenames_and_combine_with_other_filters(self):
        names = (
            "report1.json",
            "reportA.json",
            "report[1].json",
            "report[.json",
            "100%_complete.json",
            "100xxcomplete.json",
            "report2.txt",
            "röport.json",
            "directory.json/other.txt",
            "deep/report3.json",
            "VisualStudioCode.exe",
        )
        paths = {name: self.file(name) for name in names}
        self.scan()
        for text, expected in (
            ('"*.json" type:file', {name for name in names if name.endswith(".json")}),
            ("report?.json", {"report1.json", "reportA.json", "report[.json", "deep/report3.json"}),
            ("report[0-9].json", {"report1.json", "deep/report3.json"}),
            ("report[!0-9].json", {"reportA.json", "report[.json"}),
            ("report[[]1].json", {"report[1].json"}),
            ("report[.json", {"report[.json"}),
            ("100%_*.json", {"100%_complete.json"}),
            ("r?port.json", {"röport.json"}),
            ("*.exe", {"VisualStudioCode.exe"}),
            (
                "*.json rpt size:>0b",
                {
                    "report1.json",
                    "reportA.json",
                    "report[1].json",
                    "report[.json",
                    "deep/report3.json",
                    "röport.json",
                },
            ),
            ("name:report[0-9]", {"report1.json", "report2.txt", "deep/report3.json"}),
            ("path:d* report[0-9].json", {"deep/report3.json"}),
            (
                "report* ![1]",
                {
                    "report1.json",
                    "reportA.json",
                    "report[.json",
                    "report2.txt",
                    "deep/report3.json",
                },
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    {h.item.path for h in self.hits(text)}, {paths[name] for name in expected}
                )

    def test_extension_patterns_and_lists_match_whole_extensions(self):
        names = (
            "report.doc",
            "report.docx",
            "report.docm",
            "report.docxx",
            "report.txt",
            "image.JPG",
            "image.png",
            "image.xjpg",
            "sample.döc",
            "literal.100%_ext",
            "other.100xxext",
        )
        paths = {name: self.file(name) for name in names}
        self.scan()
        for text, expected in (
            # Extensions match with and without accents.
            ("ext:doc,docx", {"report.doc", "report.docx", "sample.döc"}),
            ("ext:(doc,docx)", {"report.doc", "report.docx", "sample.döc"}),
            ("ext:döc", {"report.doc", "sample.döc"}),
            (
                "ext:doc*",
                {"report.doc", "report.docx", "report.docm", "report.docxx", "sample.döc"},
            ),
            ("ext:doc?", {"report.docx", "report.docm"}),
            ("ext:doc[!x]", {"report.docm"}),
            ("ext:d?c", {"report.doc", "sample.döc"}),
            ("ext:(jpg, png)", {"image.JPG", "image.png"}),
            ("ext:(doc?,p*)", {"report.docx", "report.docm", "image.png"}),
            ("ext:doc ext:txt", {"report.doc", "report.txt", "sample.döc"}),
            ("ext:100%_*", {"literal.100%_ext"}),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    {hit.item.path for hit in self.hits(text)}, {paths[name] for name in expected}
                )

    def test_size_and_partial_date_filters_include_only_matching_files(self):
        paths = {}
        for name, size, modified in (
            ("empty.txt", 0, datetime(2026, 2, 1)),
            ("exact.txt", 1024, datetime(2026, 2, 1)),
            ("small.txt", 1025, datetime(2026, 2, 1)),
            ("last.exe", 2048, datetime(2026, 2, 28, 23, 59, 59)),
            ("large.txt", 2049, datetime(2026, 2, 15)),
            ("old.txt", 1500, datetime(2026, 1, 31, 23, 59, 59)),
            ("next.txt", 1500, datetime(2026, 3, 1)),
        ):
            path = self.file(name, "x" * size)
            os.utime(path, (modified.timestamp(),) * 2)
            paths[name] = path
        (self.root / "folder").mkdir()
        self.scan()
        self.assertEqual(
            {
                h.item.path
                for h in self.hits("size:>1kb size:<=2kbyte after:2026-02* before:2026-02*")
            },
            {paths["small.txt"], paths["last.exe"]},
        )
        self.assertEqual([h.item.path for h in self.hits("size:<1byte")], [paths["empty.txt"]])
        self.assertFalse(self.hits("type:folder size:<1gb"))

    def test_listary_style_syntax_filters_files_in_both_engines(self):
        paths = {}
        for relative, size, modified in (
            ("Rechnungen/2024 Strom.pdf", 2048, datetime(2024, 3, 5)),
            ("Rechnungen/2024 Strom.bak", 2048, datetime(2024, 3, 5)),
            ("Archiv/2024 Strom.pdf", 4096, datetime(2024, 9, 1)),
            ("Work/Client/Project Plan.docx", 10, datetime(2026, 1, 15)),
            ("Work/Client/Plan of Project.docx", 10, datetime(2026, 6, 30)),
            ("Work/Other/Project Plan old version.docx", 10, datetime(2026, 7, 1)),
            ("Work/Client/proposal.tmp", 10, datetime(2026, 2, 1)),
        ):
            path = self.file(relative, "x" * size)
            os.utime(path, (modified.timestamp(),) * 2)
            paths[relative] = path
        self.scan()

        def found(*relatives):
            return {paths[relative] for relative in relatives}

        work = self.root / "Work"
        for text, expected in (
            (r"rechnungen\ 2024", found("Rechnungen/2024 Strom.pdf", "Rechnungen/2024 Strom.bak")),
            (r"rechnungen\ 2024 !*.bak", found("Rechnungen/2024 Strom.pdf")),
            ("2024 -ext:bak;pdf", set()),
            ("strom -archiv", found("Rechnungen/2024 Strom.pdf", "Rechnungen/2024 Strom.bak")),
            (r"work\client\ pro", found(*(r for r in paths if r.startswith("Work/Client")))),
            (
                f'"{work}{os.sep}" plan date:2026-01..2026-06',
                found("Work/Client/Project Plan.docx", "Work/Client/Plan of Project.docx"),
            ),
            (
                '"project plan"',
                found("Work/Client/Project Plan.docx", "Work/Other/Project Plan old version.docx"),
            ),
            ('"project plan" -"old version"', found("Work/Client/Project Plan.docx")),
            ("strom size:3kb..1mb", found("Archiv/2024 Strom.pdf")),
            (
                "strom date:<=2024-03",
                found("Rechnungen/2024 Strom.pdf", "Rechnungen/2024 Strom.bak"),
            ),
        ):
            for engine in ("python", "auto"):
                with (
                    self.subTest(text=text, engine=engine),
                    patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE=engine),
                ):
                    self.assertEqual({hit.item.path for hit in self.hits(text)}, expected)
        self.assertGreater(len(self.hits("project plan")), 2)  # Fuzzy without quotes.

    def test_query_filters_paths_dates_and_literal_sql_wildcards(self):
        path = self.file("Projects/Reports/100%_complete.txt")
        self.file("Projects/Other/100xxcomplete.txt")
        os.utime(path, (datetime(2025, 6, 15).timestamp(),) * 2)
        self.scan()
        query = parse_query(
            f'"100%_" ext:txt in:"{self.root}" after:2025-06-01 before:2025-06-30 path:projects/reports'
        )
        self.assertEqual([h.item.path for h in self.search.search(query, self.settings)], [path])
        self.assertFalse(self.hits("100%_ !reports"))
        self.assertFalse(self.hits("path:reports/projects"))
        with self.assertRaises(ValueError):
            parse_query("type:unknown")

    def test_streaming_rank_does_not_truncate_strong_late_candidate(self):
        root_id = CatalogSource(self.root).id
        special = self.root / "zzzz/Code.txt"
        settings = replace(
            self.settings, folder_priorities=(FolderPriority(str(special.parent), 100),)
        )
        items = [
            (CatalogItem(self.root / f"a/{i:04d}-Code.txt", ItemKind.FILE), root_id)
            for i in range(1800)
        ]
        items.append((CatalogItem(special, ItemKind.FILE), root_id))
        with self.store.snapshot() as connection:
            self.store.stage(connection, items)
            self.store.publish(connection, active_roots=(root_id,), preserved_roots=())
        self.assertEqual(self.hits("code", settings)[0].item.path, special)

    def test_scoped_refresh_retains_owner_when_full_root_later_fails(self):
        self.file("nested/report.txt")
        self.scan()
        self.scan(scope=self.root / "nested")
        with self.store.connect() as connection:
            owners = {row[0] for row in connection.execute("SELECT root FROM entries")}
        self.assertEqual(owners, {CatalogSource(self.root).id})
        previous = self.store.count()
        with patch(
            "winlauncher.catalog.indexer.os.scandir", side_effect=PermissionError("offline")
        ):
            report = self.indexer.scan(self.settings)
        self.assertTrue(report.errors)
        self.assertEqual(self.store.count(), previous)
        self.assertTrue(self.hits("report"))

    def test_unreadable_subtree_preserves_only_failed_paths(self):
        locked = self.file("locked/keep.txt")
        removed = self.file("remove.txt")
        self.scan()
        removed.unlink()
        scan = os.scandir

        def readable_only(path):
            if Path(path) == locked.parent:
                raise PermissionError("locked fixture")
            return scan(path)

        with patch("winlauncher.catalog.indexer.os.scandir", side_effect=readable_only):
            report = self.indexer.scan(self.settings)
        self.assertTrue(report.errors)
        self.assertTrue(self.hits("keep"))
        self.assertFalse(self.hits("remove"))

    def test_marker_probe_failure_preserves_catalog_but_search_fails_closed(self):
        self.file("report.txt")
        self.scan()
        previous = self.store.count()
        stat = long_paths.stat

        def available_only(path):
            if Path(path) == self.root / ".nobackup":
                raise PermissionError("marker unavailable")
            return stat(path)

        with patch.object(long_paths, "stat", available_only):
            report = self.indexer.scan(self.settings)
            self.assertTrue(report.errors)
            self.assertEqual(self.store.count(), previous)
            self.assertFalse(self.hits("report"))
        self.assertTrue(self.hits("report"))

    def test_scoped_scan_cannot_resurrect_ignored_ancestor(self):
        self.file("build/obj/output.txt")
        self.settings = replace(
            self.settings, name_priorities=(NamePriority("^build$", 0, "ignored", files=False),)
        )
        self.scan()
        self.scan(scope=self.root / "build/obj")
        self.assertFalse(self.hits("output"))

    def test_scoped_application_repair_keeps_apps_only_mode(self):
        app = self.file("Programs/Nested/Editor.exe")
        self.file("Programs/Nested/desktop.ini")
        settings = replace(self.settings, roots=(), index_apps=True)
        self.indexer.scan(settings, application_roots=(self.root / "Programs",))
        self.indexer.scan(settings, scope=app.parent, application_roots=(self.root / "Programs",))
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.hits("editor", settings)[0].item.path, app)

    def test_parent_scope_preserves_ownership_and_mode_of_nested_sources(self):
        path = self.file("docs/report.txt")
        app = self.file("Programs/Editor.exe")
        self.file("Programs/desktop.ini")
        settings = replace(self.settings, roots=(str(path.parent),), index_apps=True)
        apps = (app.parent,)
        self.indexer.scan(settings, application_roots=apps)
        with self.store.connect() as connection:
            previous = {
                row["path"]: row["root"]
                for row in connection.execute("SELECT path,root FROM entries")
            }
        self.indexer.scan(settings, scope=self.root, application_roots=apps)
        with self.store.connect() as connection:
            current = {
                row["path"]: row["root"]
                for row in connection.execute("SELECT path,root FROM entries")
            }
        self.assertEqual(current, previous)

    @unittest.skipUnless(os.name == "nt", "Windows directory junctions")
    def test_explicit_junction_root_is_followed_but_nested_junctions_are_not(self):
        import _winapi

        self.file("sub/report.txt")
        linked = self.base / "linked"
        _winapi.CreateJunction(str(self.root), str(linked))
        self.addCleanup(linked.rmdir)
        self.settings = replace(self.settings, roots=(str(linked),))
        self.scan()
        self.assertEqual(self.hits("report")[0].item.path, linked / "sub/report.txt")
        # The configured folder itself is an entry, besides sub and report.txt.
        self.assertEqual(self.hits("linked")[0].item.path, linked)
        self.scan(scope=linked / "sub")
        self.assertEqual(self.store.count(), 3)
        nested = self.root / "sub/loop"
        _winapi.CreateJunction(str(self.root), str(nested))
        self.addCleanup(nested.rmdir)
        self.scan()
        self.assertEqual(self.store.count(), 3)
        self.scan(scope=linked)  # A repair of the root itself keeps exactly one row for it.
        self.assertEqual(self.store.count(), 3)

    def test_empty_quotes_repeated_filters_and_validation_messages(self):
        self.assertEqual(parse_query('""').terms, ())
        self.assertTrue(parse_query('"" type:file').uses_learned_items_only)
        query = parse_query("ext:txt ext:.MD,txt path:a/b path:c")
        self.assertEqual(query.extensions, ("txt", "md"))
        self.assertEqual(query.folder_parts, ("a", "b", "c"))
        self.assertEqual(parse_query(r"C:\Work report").terms, (r"c:\work", "report"))
        for text, expected in (
            ("match:exact", "match: expects one of keyword, fuzzy"),
            ("sort:size", "sort: expects one of score, name, modified"),
            ("score:magic", "score: expects one of match, combined"),
            ("low:maybe", "low: expects true or false"),
            ("after:yesterday", "after: expects a date"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, expected):
                parse_query(text)

    def test_literal_and_fuzzy_passes_never_return_the_same_item_twice(self):
        for name in ("Project Report.txt", "Prj Rpt.txt", "report-project.md", "pr.txt"):
            self.file(f"docs/{name}")
        self.scan()
        for text in ("report", "pr", "proj rep", "path:docs rpt", "name:pr", "rpt"):
            with self.subTest(text=text):
                paths = [hit.item.path for hit in self.hits(text)]
                self.assertEqual(len(paths), len(set(paths)))

    def test_apostrophes_are_literal_and_unsupported_dates_are_validation_errors(self):
        self.assertEqual(parse_query('"annual report" mom\'s').terms, ("annual report", "mom's"))
        path = self.file("mom's report.txt")
        self.scan()
        self.assertEqual(self.hits("mom's")[0].item.path, path)
        with self.assertRaises(ValueError):
            parse_query("before:9999-12-31")
        if os.name == "nt":
            with self.assertRaises(ValueError):
                parse_query("after:0001-01-01")

    def test_empty_query_uses_history_and_favorites_only(self):
        first = self.file("first.txt")
        second = self.file("second.txt")
        self.scan()
        self.assertFalse(self.hits(""))
        self.assertFalse(self.hits("type:file"))
        self.store.record_launch(first, "first")
        self.store.set_favorite(second, True)
        self.assertEqual({hit.item.path for hit in self.hits("")}, {first, second})
        self.assertEqual(len(self.hits("type:file")), 2)

    def test_scoped_search_lists_direct_entries_before_deeper_matches(self):
        deep = self.file("project/archive/2019/Report.txt")
        direct = self.file("project/Old report notes.txt")
        self.scan()
        query = parse_query("report", scope=self.root / "project")
        paths = [hit.item.path for hit in self.search.search(query, self.settings)]
        self.assertEqual(paths[0], direct)  # Despite the stronger exact-name match below.
        self.assertIn(deep, paths)
        unscoped = [hit.item.path for hit in self.hits("report")]
        self.assertEqual(unscoped[0], deep)

    def test_explorer_live_folder_names_override_markers_without_indexing_contents(self):
        self.file("toolbox-cli/.nobackup", "")
        self.file("toolbox-cli/secret-document.txt")
        self.file("toolbox-second/.no-backup", "")
        direct_file = self.file("toolbox.txt")
        deep = self.file("archive/toolbox.txt")
        self.scan()
        count = self.store.count()
        for sort in ("score", "name", "modified"):
            with self.subTest(sort=sort):
                query = replace(parse_query(f"toolbox sort:{sort}", scope=self.root), explorer=True)
                hits = self.search.search(query, self.settings)
                self.assertEqual(
                    {hit.item.path for hit in hits[:2]},
                    {self.root / "toolbox-cli", self.root / "toolbox-second"},
                )
                self.assertEqual(hits[2].item.path, direct_file)
                # Subfolder matches follow every direct entry, whatever the sort order.
                self.assertEqual([hit.item.path for hit in hits[3:]], [deep])
        # A directory created after indexing also participates immediately.
        fresh = self.root / "new-folder"
        fresh.mkdir()
        query = replace(parse_query("new-folder", scope=self.root), explorer=True)
        self.assertEqual(self.search.search(query, self.settings)[0].item.path, fresh)
        self.assertEqual(self.store.count(), count)
        self.assertFalse(self.hits("toolbox-cli"))
        query = replace(parse_query("secret-document", scope=self.root), explorer=True)
        self.assertFalse(self.search.search(query, self.settings))

    def test_explorer_lists_subfolder_matches_when_no_direct_name_matches(self):
        nested = self.file("archive/unique-target.txt")
        self.file("other.txt")
        self.scan()
        query = replace(parse_query("unique-target", scope=self.root), explorer=True)
        self.assertEqual(
            [hit.item.path for hit in self.search.search(query, self.settings)], [nested]
        )
        # The common scope path must not make every direct item a name match.
        query = replace(parse_query("library", scope=self.root), explorer=True)
        self.assertFalse(self.search.search(query, self.settings))
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(self.search.search(query, self.settings, cancel=cancel))

    @unittest.skipUnless(os.name == "nt", "Windows Explorer adapter")
    def test_explorer_navigation_uses_captured_tab_and_refuses_stale_targets(self):
        from winlauncher.platform.explorer import navigate_to

        first, selected = Mock(), Mock()
        destination = self.root / "folder, with spaces"
        with (
            patch("win32com.client.Dispatch") as dispatch,
            patch("winlauncher.platform.explorer._window_folder") as folder,
            patch("winlauncher.platform.explorer.active_tab", return_value=22) as active_tab,
            patch("winlauncher.platform.explorer.activate_window") as activate,
        ):
            dispatch.return_value.Windows.return_value = [first, selected]
            folder.side_effect = [(self.root, False), (self.root, True)]
            navigate_to(destination, 11, 22, self.root)
            selected.Navigate2.assert_called_once_with(str(destination), 0)
            first.Navigate2.assert_not_called()
            activate.assert_called_once_with(11)
            selected.reset_mock()
            for matches in (
                [(self.root / "changed", True), (None, False)],
                [(self.root, False), (self.root, False)],
            ):
                folder.side_effect = matches
                with self.assertRaises(OSError):
                    navigate_to(destination, 11, 22, self.root)
                selected.Navigate2.assert_not_called()
            active_tab.return_value = 33
            with self.assertRaises(OSError):
                navigate_to(destination, 11, 22, self.root)

    def test_explorer_fallback_matches_paths_below_the_folder_and_program_names(self):
        receipt = self.file("invoices 2024/receipt.pdf")
        setup = self.file("tools/setup.exe")
        self.file("tools/readme.txt")
        self.scan()
        for engine in ("python", "auto"):
            for text, expected in (
                ("invoices receipt", [receipt]),
                ("2024 receipt", [receipt]),
                ("setup.exe", [setup]),
                ("*.exe", [setup]),
            ):
                with (
                    self.subTest(engine=engine, text=text),
                    patch.dict(os.environ, WINLAUNCHER_SEARCH_ENGINE=engine),
                ):
                    query = replace(parse_query(text, scope=self.root), explorer=True)
                    hits = self.search.search(query, self.settings)
                    self.assertEqual([hit.item.path for hit in hits], expected)

    @unittest.skipUnless(os.name == "nt", "Windows Explorer adapter")
    def test_explorer_folder_action_records_only_successful_navigation(self):
        destination = self.root / "target"
        destination.mkdir()
        action = ItemActions(self.store)
        with (
            patch("winlauncher.platform.explorer.navigate_to") as navigate,
            patch("winlauncher.platform.shell.open_path") as launch,
            patch.object(self.store, "record_launch") as record,
        ):
            action.open(destination, "target", explorer=ExplorerTarget(11, 22, self.root))
            navigate.assert_called_once_with(destination, 11, 22, self.root)
            launch.assert_not_called()
            record.assert_called_once_with(destination, "target")
            record.reset_mock()
            navigate.side_effect = OSError("Original tab closed")
            with self.assertRaises(OSError):
                action.open(destination, "target", explorer=ExplorerTarget(11, 22, self.root))
            record.assert_not_called()
            launch.assert_not_called()

    def test_skipping_hopeless_candidates_never_changes_results(self):
        names = [
            "Report",
            "report-draft",
            "Quarterly Report",
            "rep notes",
            "Prepare",
            "Super Report Archive",
            "RePort.bak",
            "reporting",
            "trip report",
            "rpt",
        ]
        for index in range(60):
            folder = ("archive", "work", "deep/er/path")[index % 3]
            self.file(f"{folder}/{names[index % len(names)]} {index:02d}.txt")
        self.file("work/Report Tool.lnk")
        self.scan()
        favorite = self.root / "archive/Prepare 04.txt"
        self.store.set_favorite(favorite, True)
        self.store.record_launch(self.root / "deep/er/path/rep notes 03.txt", "rep")
        settings = replace(
            self.settings,
            folder_priorities=(FolderPriority(str(self.root / "work"), 15),),
            extension_priorities=(ExtensionPriority("bak", -5, "low"),),
            name_priorities=(NamePriority("draft", 10, "high"),),
        )
        texts = ("rep", "report", "r", "rpt sort:name", "report sort:modified", "p score:match")
        scopes = (None, self.root / "work")
        for text in texts:
            for scope in scopes:
                with self.subTest(text=text, scope=scope):
                    query = parse_query(text, scope=scope)
                    limited = self.search.search(query, replace(settings, max_results=10))
                    complete = self.search.search(query, replace(settings, max_results=200))
                    self.assertEqual(
                        [(h.item.path, round(h.score, 6)) for h in limited],
                        [(h.item.path, round(h.score, 6)) for h in complete][: len(limited)],
                    )

    def test_candidate_rows_name_items_like_catalog_items(self):
        for relative, kind, stem in (
            ("archive.tar.gz", ItemKind.FILE, "archive.tar"),
            ("file.", ItemKind.FILE, "file."),
            (".hidden", ItemKind.FILE, ".hidden"),
            ("Tool.lnk", ItemKind.APP, "Tool"),
            ("Folder.With.Dots", ItemKind.FOLDER, "Folder.With"),
        ):
            item = CatalogItem(self.root / relative, kind)
            candidate = Candidate.from_item(item)
            with self.subTest(relative=relative):
                self.assertEqual(candidate.name, item.name)
                self.assertEqual(candidate.stem, stem)
                self.assertEqual(candidate.to_item(), item)

    def test_losing_candidates_do_not_probe_every_directory(self):
        items = []
        for index in range(150):
            path = self.file(f"folder-{index:04d}/{index:04d}-Report.txt")
            items.append(CatalogItem(path, ItemKind.FILE))
        source = CatalogSource(self.root).id
        with self.store.snapshot() as connection:
            self.store.stage(connection, ((item, source) for item in items))
            self.store.publish(connection, active_roots=(source,), preserved_roots=())
        settings = replace(self.settings, max_results=10)
        stat = long_paths.stat
        calls = []

        def counted(path):
            calls.append(Path(path))
            return stat(path)

        with patch.object(long_paths, "stat", counted):
            self.assertEqual(len(self.hits("report match:keyword", settings)), 10)
        self.assertTrue(calls)  # Kept hits were probed for markers.
        candidate_folders = {path.parent for path in calls if path.parent.parent == self.root}
        self.assertLess(len(candidate_folders), 30)  # Not all 150 candidate folders.

    def test_windows_path_spelling_preserves_usage_without_unicode_aliasing(self):
        path = self.file("Mixed/Report.txt")
        self.settings = replace(self.settings, roots=(str(self.root), str(path.parent).upper()))
        self.scan()
        self.store.set_favorite(Path(str(path).upper()), True)
        self.store.record_launch(Path(str(path).upper()), "report")
        self.scan(scope=Path(str(path.parent).upper()))
        hits = self.hits("report")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].item.pinned)
        self.assertEqual(hits[0].item.query_launches, 1)
        self.assertNotEqual(path_key(self.root / "Straße"), path_key(self.root / "Strasse"))
        self.assertNotEqual(path_key(self.root / "Café"), path_key(self.root / "Cafe\u0301"))

    def test_scoped_sql_uses_the_path_index(self):
        from winlauncher.catalog.model import path_range

        with self.store.connect() as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM entries WHERE path_key>=? AND path_key<?",
                path_range(self.root),
            ).fetchall()
        self.assertTrue(any("entries_scope" in row[3] for row in plan))

    def test_legacy_identity_migration_keeps_usage_favorites_and_search_index(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self._assert_legacy_migration(version)

    def _assert_legacy_migration(self, version):
        self.store = CatalogStore(self.base / f"legacy-{version}.sqlite3")
        self.indexer = CatalogIndexer(self.store)
        path = self.file("Report.txt")
        self.scan()
        with self.store.connect() as connection:
            for table in ("usage", "query_usage", "favorites"):
                connection.execute(f"ALTER TABLE {table} RENAME COLUMN path_key TO path")
            connection.execute("INSERT INTO usage VALUES(?,2,100)", (str(path),))
            connection.execute("INSERT INTO usage VALUES(?,3,200)", (str(path).upper(),))
            connection.execute("INSERT INTO query_usage VALUES('report',?,2)", (str(path),))
            connection.execute("INSERT INTO favorites VALUES(?)", (str(path).upper(),))
            connection.execute("DROP INDEX entries_scope")
            connection.execute("CREATE INDEX entries_scope ON entries(path_key)")
            connection.execute(
                "UPDATE entries SET root=?,path_key=?",
                ("files:" + str(self.root).lower().replace("\\", "/") + "/", str(path).lower()),
            )
            if version == 1:
                connection.execute("ALTER TABLE entries DROP COLUMN attributes")
            connection.execute(f"PRAGMA user_version={version}")
        self.store = CatalogStore(self.store.path)
        self.search = SearchService(self.store)
        hit = self.hits("report")[0]
        self.assertEqual(hit.item.launches, 5)
        self.assertEqual(hit.item.query_launches, 2)
        self.assertTrue(hit.item.pinned)
        self.scan()
        self.assertEqual(self.store.count(), 2)  # The configured folder and the report.

    def test_only_successful_launches_are_learned(self):
        path = self.file("App.exe")
        self.scan()
        with patch("winlauncher.platform.shell.open_path", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                ItemActions(self.store).open(path, "app")
        self.assertEqual(self.hits("app")[0].item.launches, 0)

    def test_file_refresh_updates_metadata_without_scanning_neighbors(self):
        changed = self.file("nested/report.txt", "old")
        untouched = self.file("other/keep.txt")
        self.scan()
        changed.write_text("updated content", encoding="utf8")
        with patch(
            "winlauncher.catalog.indexer.os.scandir", side_effect=AssertionError("Recursive scan")
        ):
            report = self.indexer.refresh_files(self.settings, (changed,))
        self.assertFalse(report.errors)
        self.assertEqual(self.hits("report")[0].item.size, len("updated content"))
        self.assertEqual(self.hits("keep")[0].item.path, untouched)
        self.store.set_favorite(changed, True)
        changed.unlink()
        self.indexer.refresh_files(self.settings, (changed,))
        self.assertFalse(self.hits("report"))
        self.assertTrue(self.hits("keep"))

    def test_file_refresh_preserves_failed_path_and_honors_ancestor_rules(self):
        path = self.file("private/report.txt")
        self.scan()
        lstat = long_paths.lstat

        def unavailable(candidate):
            if Path(candidate) == path:
                raise PermissionError("fixture")
            return lstat(candidate)

        with patch.object(long_paths, "lstat", unavailable):
            report = self.indexer.refresh_files(self.settings, (path,))
        self.assertTrue(report.errors)
        self.assertTrue(self.hits("report"))
        settings = replace(
            self.settings,
            name_priorities=(
                NamePriority("private", search_priority="ignored", files=False, folders=True),
            ),
        )
        report = self.indexer.refresh_files(settings, (path,))
        self.assertFalse(report.errors)
        self.assertFalse(self.hits("report"))

    @unittest.skipUnless(os.name == "nt", "Windows shell actions")
    def test_shell_actions_set_working_directory_and_quote_comma_paths(self):
        from winlauncher.platform.shell import open_path, show_in_folder

        path = self.file("a,b.txt")
        with patch("os.startfile") as launch, patch("subprocess.Popen") as reveal:
            open_path(path)
            show_in_folder(path)
        launch.assert_called_once_with(str(path), cwd=str(path.parent))
        explorer = Path(os.environ["WINDIR"]) / "explorer.exe"
        reveal.assert_called_once_with(f'"{explorer}" /select,"{path}"')

    def test_settings_round_trip_and_validation(self):
        settings = replace(
            self.settings,
            name_priorities=(NamePriority(".*backup.*", -15, "low"),),
            plugin_time_limit_ms=0,
        )
        file = SettingsFile(self.base / "settings.json")
        file.save(settings)
        self.assertEqual(file.load(), settings)
        with self.assertRaises(ValueError):
            replace(settings, name_priorities=(NamePriority("["),))
        with self.assertRaises(ValueError):
            replace(settings, marker_files=("../marker",))
        with self.assertRaises(ValueError):
            replace(settings, name_priorities=(NamePriority("secret", files="false"),))
        with self.assertRaises(ValueError):
            replace(settings, plugin_time_limit_ms=-1)

    def test_double_ctrl_rejects_shortcuts_repeats_and_long_holds(self):
        gesture = DoubleCtrlGesture(350)
        self.assertFalse(gesture.feed(0xA2, True, 0.00))
        self.assertFalse(gesture.feed(0xA2, True, 0.02))
        self.assertFalse(gesture.feed(0xA2, False, 0.05))
        self.assertFalse(gesture.feed(0xA2, True, 0.15))
        self.assertTrue(gesture.feed(0xA2, False, 0.19))
        gesture.feed(0xA2, True, 1)
        gesture.feed(ord("C"), True, 1.01)
        self.assertFalse(gesture.feed(0xA2, False, 1.05))
        gesture.feed(0xA2, True, 1.10)
        self.assertFalse(gesture.feed(0xA2, False, 1.15))
        gesture.feed(0xA2, True, 2)
        self.assertFalse(gesture.feed(0xA2, False, 3))
        gesture.feed(0xA2, True, 3.05)
        self.assertFalse(gesture.feed(0xA2, False, 3.10))

    @unittest.skipUnless(os.name == "nt", "Windows directory notifications")
    def test_native_watcher_reports_created_file_and_stops(self):
        from winlauncher.platform.changes import FileChange
        from winlauncher.platform.watcher import DirectoryWatcher

        events = queue.SimpleQueue()
        watcher = DirectoryWatcher(self.root, events)
        watcher.start()
        try:
            self.assertTrue(watcher.ready.wait(4), "Directory watch did not become ready")
            self.file("new.txt")
            self.assertEqual(events.get(timeout=4), FileChange(self.root / "new.txt"))
        finally:
            watcher.stop()
            watcher.join(timeout=2)
        self.assertFalse(watcher.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows Explorer adapter")
    def test_explorer_cache_survives_resolution_only_for_unchanged_view(self):
        from winlauncher.platform.explorer import ExplorerContext, ExplorerTracker

        for view, retained in (((1, 2), True), ((1, 3), False)):
            tracker = ExplorerTracker(on_failure=lambda message: None)
            original = ExplorerContext(1, 2, self.root)
            tracker.context = original
            observed = []

            def resolve(*args, tracker=tracker, original=original, retained=retained):
                self.assertEqual(tracker.context, original if retained else None)
                tracker.stop()
                return self.root

            with (
                patch("win32com.client.Dispatch"),
                patch("winlauncher.platform.explorer.explorer_file_view", return_value=view),
                patch("winlauncher.platform.explorer.resolve_folder", side_effect=resolve),
                patch(
                    "pythoncom.PumpWaitingMessages",
                    side_effect=lambda observed=observed, tracker=tracker: observed.append(
                        tracker.context
                    ),
                ),
            ):
                tracker.run()
            self.assertEqual(observed, [ExplorerContext(*view, self.root)])

    @unittest.skipUnless(os.name == "nt", "Windows Explorer adapter")
    def test_explorer_rejects_menu_and_drag_input(self):
        import ctypes

        from winlauncher.platform.windows import GuiThreadInfo, explorer_file_view, user32

        for field in ("flags", "hwndMenuOwner", "hwndCapture", "hwndMoveSize"):

            def info(thread, pointer, field=field):
                state = ctypes.cast(pointer, ctypes.POINTER(GuiThreadInfo)).contents
                state.hwndFocus = 2
                setattr(state, field, 2)
                return True

            with (
                patch.object(user32, "GetForegroundWindow", return_value=1),
                patch.object(user32, "GetGUIThreadInfo", side_effect=info),
                patch("winlauncher.platform.windows.class_name", return_value="CabinetWClass"),
            ):
                self.assertIsNone(explorer_file_view())

    @unittest.skipUnless(os.name == "nt", "Windows directory notifications")
    def test_watcher_filters_excluded_activity_but_keeps_marker_changes(self):
        from winlauncher.platform.changes import FolderChange, WatchFailed
        from winlauncher.platform.watcher import DirectoryWatcher

        self.file(".git/initial.txt")
        self.file("private/.nobackup", "")
        events = queue.SimpleQueue()
        watcher = DirectoryWatcher(self.root, events, exclusions=self.settings.exclusions())
        watcher.start()
        try:
            self.assertTrue(watcher.ready.wait(4))
            self.file(".git/ignored.txt")
            self.file("private/ignored.txt")
            self.file("private/.no-backup", "")
            folders = set()
            while self.root / "private" not in folders:
                event = events.get(timeout=4)
                self.assertNotIsInstance(event, WatchFailed)
                if isinstance(event, FolderChange):
                    folders.add(event.folder)
            self.assertNotIn(self.root / ".git", folders)
        finally:
            watcher.stop()
            watcher.join(timeout=2)
        self.assertFalse(watcher.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows directory notifications")
    def test_nested_writes_and_git_locks_do_not_queue_recursive_parent_repairs(self):
        from winlauncher.platform.changes import FileChange, FolderChange
        from winlauncher.platform.watcher import DirectoryWatcher

        self.file("a/b/initial.txt")
        self.file("project/.git/initial.txt")
        events = queue.SimpleQueue()
        watcher = DirectoryWatcher(self.root, events, exclusions=self.settings.exclusions())
        watcher.start()
        try:
            self.assertTrue(watcher.ready.wait(4))
            changed = self.file("a/b/new.txt")
            lock = self.file("project/.git/index.lock")
            lock.unlink()
            received = []
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    received.append(events.get(timeout=deadline - time.monotonic()))
                except queue.Empty:
                    break
            self.assertIn(FileChange(changed), received)
            self.assertFalse(any(isinstance(event, FolderChange) for event in received), received)
        finally:
            watcher.stop()
            watcher.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
