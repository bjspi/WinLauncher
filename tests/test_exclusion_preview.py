"""Rule previews share search semantics and leave the catalog and editor unchanged."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTreeWidgetItem

from winlauncher.catalog.exclusion_preview import ExclusionPreview, preview_exclusion
from winlauncher.catalog.model import REGISTRY_SOURCE, CatalogItem, CatalogSource, ItemKind
from winlauncher.catalog.store import CatalogStore
from winlauncher.ui.exclude_dialog import ExcludeFolderDialog, FolderChoice, PatternDialog
from winlauncher.ui.exclusion_preview import RulePreviewDialog
from winlauncher.ui.tasks import BackgroundTasks


class PreviewFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-rule-preview-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = CatalogStore(self.base / "catalog.sqlite3")
        self.root = self.base / "Library"

    def stage(self, entries: list[tuple[str, ItemKind, str]]) -> None:
        with self.store.snapshot() as connection:
            self.store.stage(
                connection,
                ((CatalogItem(self.root / path, kind), source) for path, kind, source in entries),
            )
            self.store.publish(
                connection, active_roots={source for _, _, source in entries}, preserved_roots=()
            )

    def fixture(self) -> None:
        source = CatalogSource(self.root).id
        self.stage(
            [
                ("Dokumente/fddb", ItemKind.FOLDER, source),
                ("Dokumente/fddb/report.txt", ItemKind.FILE, source),
                ("Dokumente/fddb/nested/deep.txt", ItemKind.FILE, source),
                ("Dokumente/fddb/report.pdf", ItemKind.FILE, source),
                ("Dokumente/fddb-copy/keep.txt", ItemKind.FILE, source),
                ("Other/keep.txt", ItemKind.FILE, source),
            ]
        )


class PreviewCatalogTest(PreviewFixture):
    def test_path_wildcards_match_only_the_expected_level(self) -> None:
        self.fixture()
        preview = preview_exclusion(self.store, r"**\Dokumente\fddb\*.txt", Event())
        self.assertIsNotNone(preview)
        self.assertEqual(
            [match.path for match in preview.matches], [self.root / "Dokumente/fddb/report.txt"]
        )
        self.assertEqual((preview.files, preview.folders), (1, 0))
        self.assertFalse(preview.matches[0].inherited)
        recursive = preview_exclusion(self.store, "**/Dokumente/fddb/**/*.txt", Event())
        self.assertEqual(recursive.total, 2)

    def test_folder_rules_include_descendants_and_preserve_path_boundaries(self) -> None:
        self.fixture()
        preview = preview_exclusion(self.store, r"**\Dokumente\fddb", Event())
        self.assertEqual((preview.files, preview.folders), (3, 1))
        matches = {match.path: match for match in preview.matches}
        self.assertFalse(matches[self.root / "Dokumente/fddb"].inherited)
        self.assertTrue(matches[self.root / "Dokumente/fddb/nested/deep.txt"].inherited)
        self.assertNotIn(self.root / "Dokumente/fddb-copy/keep.txt", matches)

    def test_filename_rules_respect_application_source_exemptions(self) -> None:
        source = CatalogSource(self.root).id
        apps = CatalogSource(self.root / "Start menu", apps_only=True).id
        self.stage(
            [
                ("ordinary.lnk", ItemKind.APP, source),
                ("Start menu/program.lnk", ItemKind.APP, apps),
                ("registered.lnk", ItemKind.APP, REGISTRY_SOURCE),
            ]
        )
        preview = preview_exclusion(self.store, "*.lnk", Event())
        self.assertEqual([match.path.name for match in preview.matches], ["ordinary.lnk"])
        folder = preview_exclusion(self.store, "Start menu", Event())
        self.assertEqual([match.path.name for match in folder.matches], ["program.lnk"])
        self.assertTrue(folder.matches[0].inherited)

    def test_preview_does_not_probe_disks_or_write_catalog(self) -> None:
        self.fixture()
        with self.store.connect() as connection:
            before = list(connection.execute("SELECT * FROM entries"))
        with patch("winlauncher.catalog.exclusions.long_paths.stat") as probe:
            preview = preview_exclusion(self.store, "  FDDB  ", Event())
        probe.assert_not_called()
        self.assertEqual(preview.total, 4)
        with self.store.connect() as connection:
            self.assertEqual(before, list(connection.execute("SELECT * FROM entries")))

    def test_counts_remain_exact_when_the_tree_sample_is_limited(self) -> None:
        self.fixture()
        preview = preview_exclusion(self.store, "*.txt", Event(), limit=2)
        self.assertEqual(len(preview.matches), 2)
        self.assertEqual(preview.total, 4)
        empty = preview_exclusion(self.store, "*.missing", Event())
        self.assertEqual(empty, ExclusionPreview((), 0, 0))

    def test_cancelled_preview_discards_partial_results(self) -> None:
        self.fixture()
        cancelled = Event()
        cancelled.set()
        self.assertIsNone(preview_exclusion(self.store, "*.txt", cancelled))
        cancelled.clear()

        def interrupt(_seconds: float) -> None:
            cancelled.set()

        with patch("winlauncher.catalog.exclusion_preview.time.sleep", interrupt):
            self.assertIsNone(preview_exclusion(self.store, "*.txt", cancelled))


class PreviewDialogTest(PreviewFixture):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        super().setUp()
        self.fixture()
        self.tasks = BackgroundTasks()
        self.addCleanup(self.tasks.close)

    def dialog(self, pattern: str) -> RulePreviewDialog:
        dialog = RulePreviewDialog(pattern, self.store, self.tasks)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        dialog.show()
        return dialog

    def await_preview(self, dialog: RulePreviewDialog) -> None:
        deadline = time.monotonic() + 5
        while dialog.summary.text() == "Checking the catalog…" and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertNotEqual(dialog.summary.text(), "Checking the catalog…")

    def test_async_tree_distinguishes_matches_descendants_and_context(self) -> None:
        dialog = self.dialog("fddb")
        self.await_preview(dialog)
        self.assertIn("4 entries", dialog.summary.text())
        self.assertIn("3 files / apps", dialog.summary.text())
        self.assertIn("1 folder", dialog.summary.text())

        def find(node: QTreeWidgetItem, name: str) -> QTreeWidgetItem | None:
            if node.text(0) == name:
                return node
            for position in range(node.childCount()):
                if found := find(node.child(position), name):
                    return found
            return None

        tree = dialog.tree.invisibleRootItem()
        self.assertEqual(find(tree, "fddb").text(1), "Matches rule")
        self.assertEqual(find(tree, "deep.txt").text(1), "Inside excluded folder")
        self.assertEqual(find(tree, "Dokumente").text(1), "Context folder")
        self.assertIsNone(find(tree, "keep.txt"))
        self.assertTrue(dialog.expand.isEnabled())
        self.assertTrue(find(tree, "fddb").isExpanded())
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_no_matches_is_explicit(self) -> None:
        dialog = self.dialog("*.missing")
        self.await_preview(dialog)
        self.assertIn("No matching entries", dialog.summary.text())
        self.assertEqual(dialog.tree.topLevelItemCount(), 0)

    def test_closing_before_delivery_ignores_late_success_and_failure(self) -> None:
        with patch.object(self.tasks, "submit"):
            dialog = self.dialog("*.txt")
        dialog.reject()
        previous = dialog.summary.text()
        dialog._loaded(preview_exclusion(self.store, "*.txt", Event()))
        dialog._failed("late error")
        self.app.processEvents()
        self.assertEqual(dialog.summary.text(), previous)
        self.assertEqual(dialog.tree.topLevelItemCount(), 0)

    def test_editor_previews_current_text_without_accepting_the_rule(self) -> None:
        picker = ExcludeFolderDialog(
            [FolderChoice(self.root, 6)], store=self.store, tasks=self.tasks
        )
        self.addCleanup(picker.deleteLater)

        def edit(editor: PatternDialog) -> int:
            editor.field.setText("  **\\Dokumente\\fddb\\*.txt  ")
            self.assertTrue(editor.preview_button.isEnabled())

            def preview(dialog: RulePreviewDialog) -> int:
                dialog.show()
                self.await_preview(dialog)
                self.assertIn("1 entry", dialog.summary.text())
                dialog.reject()
                return dialog.result()

            with patch.object(RulePreviewDialog, "exec", preview):
                editor.preview_rule()
            self.assertEqual(editor.field.text(), "  **\\Dokumente\\fddb\\*.txt  ")
            self.assertEqual(editor.result(), QDialog.DialogCode.Rejected)
            editor.field.setText("   ")
            self.assertFalse(editor.preview_button.isEnabled())
            self.assertFalse(editor.add_rule.isEnabled())
            return QDialog.DialogCode.Rejected

        with patch.object(PatternDialog, "exec", edit):
            picker.edit_pattern()
        self.assertIsNone(picker.pattern)
