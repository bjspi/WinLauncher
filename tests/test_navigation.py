"""Absolute paths and home/environment shortcuts browse while preserving exclusions."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from winlauncher.catalog import storage
from winlauncher.catalog.model import ItemKind
from winlauncher.catalog.priority import NamePriority
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.navigation import FolderNames
from winlauncher.search.query_parser import parse_query
from winlauncher.search.service import SearchService
from winlauncher.settings import DEFAULT_EXCLUDED_PATTERNS, RESULT_LIMIT_RANGE, Settings


class PathNavigationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-navigation-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.toolbox = self.home / ".toolbox"
        self.toolbox.mkdir()
        (self.home / "My Documents").mkdir()
        (self.home / "notes.txt").write_text("fixture")
        self.settings = Settings(roots=(), index_apps=False, index_windows_tools=False)
        self.service = SearchService(CatalogStore(self.home / "state" / "catalog.sqlite3"))
        # Only these variables, so suggestions do not depend on the machine's own.
        system = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR") if name in os.environ}
        self.environment = patch.dict(
            os.environ,
            {
                **system,
                "USERPROFILE": str(self.home),
                "LOCALAPPDATA": str(self.home / "Local Data"),
                "TEMP": str(self.home / "Temporary"),
                "TMP": str(self.home / "Temporary"),
                "API_TOKEN": "secret-value",
                "DEVTOOLS": str(self.toolbox),
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.home / "Local Data").mkdir()
        (self.home / "Temporary").mkdir()
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def paths(self, text, *, settings=None, cancel=None):
        return [
            hit.item.path
            for hit in self.service.search(
                parse_query(text), settings or self.settings, cancel=cancel
            )
        ]

    def test_home_and_environment_paths_work_without_indexing_or_native_module(self):
        for text, expected in (
            ("~", self.home),
            ("~/.toolbox", self.toolbox),
            ("~\\.toolbox", self.toolbox),
            ("%USERPROFILE%/.toolbox", self.toolbox),
            ("%TEMP%", self.home / "Temporary"),
            ("%LOCALAPPDATA%", self.home / "Local Data"),
            ("~/My Documents", self.home / "My Documents"),
        ):
            with (
                self.subTest(text=text),
                patch(
                    "winlauncher.search.service.select_native",
                    side_effect=AssertionError("Not a catalog search"),
                ),
            ):
                self.assertEqual(self.paths(text), [expected])
        self.assertEqual(self.service.store.count(), 0)
        self.assertEqual(parse_query("~/.toolbox").history_key, "navigate:~/.toolbox")

    def test_alias_suggestions_and_direct_children_use_prefix_completion(self):
        self.assertEqual(self.paths("%LO"), [self.home / "Local Data"])
        self.assertEqual(self.paths("%T"), [self.home / "Temporary"])
        self.assertIn(self.toolbox, self.paths("~/"))
        self.assertIn(self.home / "notes.txt", self.paths("~\\"))
        self.assertEqual(self.paths("~/.to"), [self.toolbox])
        self.assertEqual(self.paths("%USERPROFILE%/My Doc"), [self.home / "My Documents"])
        (self.toolbox / "nested").mkdir()
        self.assertNotIn(self.toolbox / "nested", self.paths("~/"))
        self.assertIn(self.toolbox / "nested", self.paths("~/.toolbox/"))
        self.assertEqual(self.paths("%USERPROFILE%/missing/"), [])

    def test_absolute_targets_bypass_catalog_search_and_directory_enumeration(self) -> None:
        report = self.home / "My Documents" / "Shared report.txt"
        report.write_text("fixture", encoding="utf-8")
        for target in (self.home, self.toolbox, report):
            for text in (str(target), str(target).swapcase(), target.as_posix()):
                with (
                    self.subTest(text=text),
                    patch(
                        "winlauncher.search.service.select_native",
                        side_effect=AssertionError("Navigation must bypass catalog search"),
                    ),
                    patch(
                        "winlauncher.search.navigation.long_paths.scandir",
                        side_effect=AssertionError("Exact targets need no directory enumeration"),
                    ),
                ):
                    hits = self.service.search(parse_query(text), self.settings)
                    self.assertEqual([hit.item.path for hit in hits], [target])
                    self.assertIsNone(hits[0].label)
        self.assertEqual(self.service.store.count(), 0)

    def test_absolute_paths_browse_and_complete_only_direct_children(self) -> None:
        nested = self.toolbox / "nested"
        nested.mkdir()
        for separator in ("/", "\\"):
            with self.subTest(separator=separator):
                root = str(self.home) + separator
                self.assertIn(self.toolbox, self.paths(root))
                self.assertIn(self.home / "notes.txt", self.paths(root))
                self.assertNotIn(nested, self.paths(root))
                self.assertEqual(self.paths(root + ".to"), [self.toolbox])
                self.assertEqual(self.paths(root + "My Doc"), [self.home / "My Documents"])
                self.assertEqual(self.paths(root + "missing"), [])
                self.assertEqual(self.paths(root + "missing" + separator), [])
                self.assertIn(nested, self.paths(str(self.toolbox) + separator))
        self.assertEqual(self.service.store.count(), 0)

    def test_alias_metadata_identifies_variables_without_changing_targets(self) -> None:
        for text, alias, target in (
            ("%dev", "%DEVTOOLS%", self.toolbox),
            ("%devtools%", "%DEVTOOLS%", self.toolbox),
            ("%TEMP%", "%TEMP%", self.home / "Temporary"),
            ("%tmp%", "%TMP%", self.home / "Temporary"),
        ):
            with self.subTest(text=text):
                hit = self.service.search(parse_query(text), self.settings)[0]
                self.assertEqual(hit.label, alias)
                self.assertEqual(hit.item.path, target)
                self.assertEqual(hit.item.name, target.name)
        for text in ("~/", "%USERPROFILE%/", "%USERPROFILE%/.toolbox"):
            with self.subTest(text=text):
                hits = self.service.search(parse_query(text), self.settings)
                self.assertTrue(hits)
                self.assertTrue(all(hit.label is None for hit in hits))

    def test_aliases_sort_by_name_and_preserve_environment_precedence(self) -> None:
        # Folder names sort opposite to their variables, so the order proves label sorting.
        alpha_target = self.home / "Zulu folder"
        zulu_target = self.home / "Alpha folder"
        alpha_target.mkdir()
        zulu_target.mkdir()
        variables = {"NAV_ALPHA": zulu_target, "NAV_EXTRA": self.toolbox}
        self.service = SearchService(self.service.store, lambda: FolderNames(variables))
        with patch.dict(
            os.environ,
            NAV_ALPHA=str(alpha_target),
            NAV_ZULU=str(zulu_target),
            NAV_DUPLICATE=str(alpha_target),
            NAV_SECRET="not-a-path",
        ):
            hits = self.service.search(parse_query("%nav_"), self.settings)
            self.assertEqual(
                [hit.label for hit in hits],
                ["%NAV_ALPHA%", "%NAV_EXTRA%", "%NAV_ZULU%"],
            )
            self.assertEqual(
                [hit.item.path for hit in hits], [alpha_target, self.toolbox, zulu_target]
            )
            hits = self.service.search(parse_query("%nav_alpha%"), self.settings)
            self.assertEqual(hits[0].item.path, alpha_target)

    def test_folder_names_complete_by_prefix_from_variables_and_shortcuts(self):
        downloads = self.home / "Downloads"
        downloads.mkdir()
        (downloads / "setup.exe").write_text("fixture")
        folders = FolderNames({"DOWNLOADS": downloads})
        self.service = SearchService(self.service.store, lambda: folders)

        def names(text):
            return [hit.label for hit in self.service.search(parse_query(text), self.settings)]

        # Only names starting with the typed text; TMP reaches TEMP's folder and is left out.
        self.assertEqual(names("%t"), ["%TEMP%"])
        self.assertEqual(names("%dev"), ["%DEVTOOLS%"])
        self.assertEqual(names("%user"), ["%USERPROFILE%"])
        self.assertEqual(names("%DOWN"), ["%DOWNLOADS%"])
        hit = self.service.search(parse_query("%t"), self.settings)[0]
        self.assertTrue(hit.reason.startswith("Windows folder variable"))
        # A variable whose value is no folder, such as a token, is never shown.
        self.assertEqual(names("%api"), [])
        self.assertEqual(self.paths("%Downloads%\\"), [downloads / "setup.exe"])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.service.search(parse_query("%NOWHERE%\\"), self.settings)

    def test_folder_names_keep_their_order_so_familiar_names_win(self):
        documents = self.home / "Documents"
        documents.mkdir()
        # The registry calls Documents `Personal`; the curated name comes first.
        names = FolderNames({"DOCUMENTS": documents, "PERSONAL": documents})
        self.service = SearchService(self.service.store, lambda: names)
        labels = [hit.label for hit in self.service.search(parse_query("%"), self.settings)]
        self.assertIn("%DOCUMENTS%", labels)
        self.assertNotIn("%PERSONAL%", labels)
        self.assertEqual(self.paths("%personal%"), [documents])

    def test_shell_folders_complete_by_name_and_browse_like_paths(self):
        startup = self.home / "Startup"
        startup.mkdir()
        (startup / "tool.txt").write_text("fixture")
        recycle_bin = self.home / "Shell folders" / "Recycle Bin (RecycleBinFolder).lnk"
        recycle_bin.parent.mkdir()
        recycle_bin.write_text("fixture")
        shell = {
            "Startup": startup,
            "Start Menu": self.toolbox,
            "RecycleBinFolder": recycle_bin,
            "OneDrive": self.home / "missing",
        }
        self.service = SearchService(self.service.store, lambda: FolderNames(shell=shell))

        def labels(text):
            return [hit.label for hit in self.service.search(parse_query(text), self.settings)]

        # Like Explorer's address bar: any case, spaces and filter-like text included.
        self.assertEqual(
            parse_query(" Shell:Common Startup t:app ").navigation, "Shell:Common Startup t:app"
        )
        self.assertIsNone(parse_query("shellfish").navigation)
        self.assertEqual(labels("shell:sta"), ["shell:Start Menu", "shell:Startup"])
        self.assertEqual(labels("SHELL:Startup"), ["shell:Startup"])
        # A virtual folder opens through its shortcut; a missing folder is left out.
        self.assertEqual(
            labels("shell:"), ["shell:RecycleBinFolder", "shell:Start Menu", "shell:Startup"]
        )
        hit = self.service.search(parse_query("shell:recycle"), self.settings)[0]
        self.assertEqual(hit.item.path, recycle_bin)
        self.assertTrue(hit.reason.startswith("Windows shell folder"))
        self.assertEqual(self.paths("shell:startup\\"), [startup / "tool.txt"])
        self.assertEqual(self.paths("shell:Startup/to"), [startup / "tool.txt"])
        self.assertEqual(self.paths("shell:Start Menu\\"), [])
        with self.assertRaisesRegex(ValueError, "Unknown shell folder: shell:Nowhere"):
            self.paths("shell:Nowhere\\")
        # Like other results, what was opened recently comes first; favorites lead.
        self.service.store.record_launch(startup, "")
        self.assertEqual(labels("shell:sta"), ["shell:Startup", "shell:Start Menu"])
        self.service.store.set_favorite(self.toolbox, True)
        self.assertEqual(labels("shell:sta"), ["shell:Start Menu", "shell:Startup"])
        # Without names from the platform, a shell folder is simply not suggested.
        self.service = SearchService(self.service.store)
        self.assertEqual(self.paths("shell:sta"), [])

    def test_navigation_honors_exclusions_markers_priorities_and_type(self):
        (self.home / "Report.LNK").touch()
        for pattern in DEFAULT_EXCLUDED_PATTERNS:
            if not any(c in pattern for c in "*?["):
                folder = self.home / pattern
                folder.mkdir(exist_ok=True)
        paths = self.paths("~/")
        self.assertIn(self.toolbox, paths)
        self.assertNotIn(self.home / "Report.LNK", paths)
        self.assertEqual(self.paths("~/Report.LNK"), [])
        for name in (".mypy_cache", ".ruff_cache", "__pycache__", ".pylint.d", ".pytype"):
            self.assertNotIn(self.home / name, paths)
            self.assertEqual(self.paths(f"~/{name}"), [])
            with patch(
                "winlauncher.search.navigation.os.scandir",
                side_effect=AssertionError("Excluded subtree must not be read"),
            ):
                self.assertEqual(self.paths(f"~/{name}/"), [])
        (self.toolbox / ".nobackup").touch()
        self.assertEqual(self.paths("~/.toolbox"), [])
        settings = replace(
            self.settings, name_priorities=(NamePriority("notes", search_priority="ignored"),)
        )
        self.assertNotIn(self.home / "notes.txt", self.paths("~/", settings=settings))
        query = replace(parse_query("~/"), kind=ItemKind.FOLDER)
        self.assertTrue(
            all(
                hit.item.kind == ItemKind.FOLDER
                for hit in self.service.search(query, self.settings)
            )
        )

    def test_unknown_relative_and_cancelled_shortcuts(self):
        with patch.dict(os.environ, {"WINLAUNCHER_TEST_RELATIVE": "relative/path"}):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                self.paths("%WINLAUNCHER_TEST_RELATIVE%")
        with self.assertRaisesRegex(ValueError, "Unknown environment variable"):
            self.paths("%WINLAUNCHER_NONEXISTENT_VARIABLE%")
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.paths("~/", cancel=cancel), [])
        self.assertEqual(
            parse_query('in:"%USERPROFILE%/My Documents"').scope, self.home / "My Documents"
        )

    def test_shortcut_favorite_state_can_be_toggled(self):
        query = parse_query("~/.toolbox")
        self.service.store.set_favorite(self.toolbox, True)
        self.assertTrue(self.service.search(query, self.settings)[0].item.pinned)
        self.service.store.set_favorite(self.toolbox, False)
        self.assertFalse(self.service.search(query, self.settings)[0].item.pinned)

    def test_absolute_navigation_honors_exclusions_markers_and_priorities(self) -> None:
        excluded = self.home / "__pycache__"
        excluded.mkdir()
        (excluded / "module.pyc").touch()
        with patch(
            "winlauncher.search.navigation.long_paths.scandir",
            side_effect=AssertionError("Excluded subtree must not be read"),
        ):
            self.assertEqual(self.paths(str(excluded) + os.sep), [])
        with patch(
            "winlauncher.search.navigation._item",
            side_effect=AssertionError("An excluded target must not be read"),
        ):
            self.assertEqual(self.paths(str(excluded / "module.pyc")), [])
        (self.toolbox / ".nobackup").touch()
        self.assertEqual(self.paths(str(self.toolbox)), [])
        self.assertNotIn(self.toolbox, self.paths(str(self.home) + os.sep))
        self.assertEqual(self.paths("%DEVTOOLS%\\"), [])
        # Asked for by name, the excluded folder itself still shows; its contents do not.
        self.assertEqual(self.paths("%DEVTOOLS%"), [self.toolbox])
        self.assertEqual(self.paths("%devtool"), [self.toolbox])
        ignored = replace(
            self.settings, name_priorities=(NamePriority("notes", search_priority="ignored"),)
        )
        self.assertEqual(self.paths(str(self.home / "notes.txt"), settings=ignored), [])

    def test_absolute_navigation_keeps_limits_type_cancellation_and_favorites(self) -> None:
        limit = RESULT_LIMIT_RANGE[0]
        for index in range(limit + 1):
            (self.home / f"result-{index}").mkdir()
        limited = replace(self.settings, max_results=limit)
        self.assertEqual(len(self.paths(str(self.home) + os.sep, settings=limited)), limit)
        query = replace(parse_query(str(self.home) + os.sep), kind=ItemKind.FILE)
        hits = self.service.search(query, self.settings)
        self.assertEqual([hit.item.path for hit in hits], [self.home / "notes.txt"])
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.paths(str(self.home) + os.sep, cancel=cancel), [])
        self.service.store.set_favorite(self.home / "notes.txt", True)
        hits = self.service.search(parse_query(str(self.home / "notes.txt")), self.settings)
        self.assertTrue(hits[0].item.pinned)

    def test_programs_reached_by_path_are_applications_like_in_the_catalog(self) -> None:
        program = self.home / "Program.exe"
        program.write_bytes(b"")  # Never run.
        for text in (str(program), str(self.home / "Prog")):
            with self.subTest(text=text):
                query = replace(parse_query(text), kind=ItemKind.APP)
                hits = self.service.search(query, self.settings)
                self.assertEqual([hit.item.path for hit in hits], [program])

    def test_quoted_pasted_paths_navigate_without_their_quotes(self) -> None:
        report = self.home / "My Documents" / "Shared report.txt"
        report.write_text("fixture", encoding="utf-8")
        self.assertEqual(self.paths(f'  "{report}"  '), [report])
        self.assertEqual(self.paths(f'"{self.home}{os.sep}"'), self.paths(f"{self.home}{os.sep}"))

    def test_unreadable_folders_report_a_message_and_dangling_targets_are_skipped(self) -> None:
        with (
            patch(
                "winlauncher.search.navigation.long_paths.scandir",
                side_effect=PermissionError(13, "Access is denied"),
            ),
            self.assertRaisesRegex(ValueError, "Cannot read .*Access is denied"),
        ):
            self.paths(str(self.toolbox) + os.sep)
        with patch(
            "winlauncher.search.navigation._item",
            side_effect=FileNotFoundError(2, "The system cannot find the file"),
        ):
            self.assertEqual(self.paths(str(self.home / "notes.txt")), [])

    def test_unresponsive_roots_cannot_hold_a_search_worker(self) -> None:
        released = threading.Event()
        self.addCleanup(released.set)

        def hanging_share(_root: str) -> bool:
            released.wait()
            return True

        share = r"\\unreachable-server\share"
        with (
            patch.object(storage, "_DRIVES", storage.DriveProbe(0.05, hanging_share)),
            patch.dict(os.environ, NAV_SHARE=share),
        ):
            for text in (share + "\\", share + "\\Reports\\q3"):
                with (
                    self.subTest(text=text),
                    self.assertRaisesRegex(ValueError, "does not respond"),
                ):
                    self.paths(text)
            self.assertEqual(self.paths("%nav_"), [])
