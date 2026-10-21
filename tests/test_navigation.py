"""Home and environment shortcuts browse directly while preserving exclusions."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from winlauncher.catalog.model import ItemKind
from winlauncher.catalog.priority import NamePriority
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.query_parser import parse_query
from winlauncher.search.service import SearchService
from winlauncher.settings import DEFAULT_EXCLUDED_PATTERNS, Settings


class PathNavigationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-navigation-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.toolbox = self.home / ".toolbox"
        self.toolbox.mkdir()
        (self.home / "My Documents").mkdir()
        (self.home / "notes.txt").write_text("fixture")
        self.settings = Settings(roots=(), index_apps=False)
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

    def test_folder_names_complete_by_prefix_from_variables_and_shortcuts(self):
        downloads = self.home / "Downloads"
        downloads.mkdir()
        (downloads / "setup.exe").write_text("fixture")
        self.service = SearchService(self.service.store, lambda: {"DOWNLOADS": downloads})

        def names(text):
            return [
                hit.reason.split(" · ")[0]
                for hit in self.service.search(parse_query(text), self.settings)
            ]

        # Only names starting with the typed text; TMP reaches TEMP's folder and is left out.
        self.assertEqual(names("%t"), ["Windows folder: %TEMP%"])
        self.assertEqual(names("%dev"), ["Windows folder: %DEVTOOLS%"])
        self.assertEqual(names("%user"), ["Windows folder: %USERPROFILE%"])
        self.assertEqual(names("%DOWN"), ["Windows folder: %DOWNLOADS%"])
        # A variable whose value is no folder, such as a token, is never shown.
        self.assertEqual(names("%api"), [])
        self.assertEqual(self.paths("%Downloads%\\"), [downloads / "setup.exe"])
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.service.search(parse_query("%NOWHERE%\\"), self.settings)

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
