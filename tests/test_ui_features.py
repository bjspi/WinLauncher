"""Checks for folder pickers, drag and drop, restart and the git self-updater."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pathscout.ui.exclude_dialog import ExcludeFolderDialog, FolderChoice, exclusion_candidates
from pathscout.ui.settings import PathList, dropped_folders
from pathscout.ui.updater import GitUpdater


class FolderFeaturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pathscout-features-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_dropping_several_folders_adds_each_once_and_ignores_files(self):
        first, second = self.base / "First", self.base / "Second"
        first.mkdir()
        second.mkdir()
        loose_file = self.base / "notes.txt"
        loose_file.write_text("fixture")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in (first, second, loose_file)])
        self.assertEqual(dropped_folders(mime), [first, second])
        paths = PathList((str(first).upper(),), "Search folders")
        paths.add_folders(dropped_folders(mime))
        self.assertEqual(paths.paths(), (str(first).upper(), str(second)))

    def test_exclusion_candidates_stop_at_the_owning_root(self):
        root = self.base / "Work"
        folder = root / "a" / "b"
        self.assertEqual(exclusion_candidates(folder, [root]), [folder, folder.parent, root])
        outside = exclusion_candidates(folder, [])
        self.assertEqual(outside[0], folder)
        self.assertNotIn(Path(folder.anchor), outside)

    def test_exclude_dialog_returns_the_selected_parent(self):
        choices = [FolderChoice(self.base / "a" / "b", 3), FolderChoice(self.base / "a", 900)]
        dialog = ExcludeFolderDialog(choices)
        self.assertEqual(dialog.chosen_folder(), self.base / "a" / "b")
        dialog.folders.setCurrentRow(1)
        self.assertEqual(dialog.chosen_folder(), self.base / "a")
        self.assertIn("900", dialog.folders.item(1).text())
        dialog.deleteLater()


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitUpdaterTest(unittest.TestCase):
    """A real local git remote; nothing leaves this computer."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pathscout-git-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.remote, self.checkout, self.author = (
            base / "remote.git",
            base / "checkout",
            base / "author",
        )
        self.git(base, "init", "--bare", "-b", "main", str(self.remote))
        self.git(base, "clone", str(self.remote), str(self.author))
        self.commit(self.author, "first")
        self.git(self.author, "push", "origin", "HEAD:main")
        self.git(base, "clone", str(self.remote), str(self.checkout))

    def git(self, cwd: Path, *arguments: str) -> str:
        identity = ["-c", "user.name=PathScout Test", "-c", "user.email=test@example.invalid"]
        result = subprocess.run(
            ["git", *identity, *arguments], cwd=cwd, capture_output=True, text=True, check=True
        )
        return result.stdout

    def commit(self, repository: Path, text: str) -> None:
        (repository / "file.txt").write_text(text)
        self.git(repository, "add", "file.txt")
        self.git(repository, "commit", "-m", text)

    def wait_for(self, condition, timeout_ms: int = 20_000) -> None:
        for _ in range(timeout_ms // 50):
            if condition():
                return
            QTest.qWait(50)
        self.fail("Timed out waiting for git")

    def test_detects_and_pulls_an_upstream_update(self):
        self.commit(self.author, "second")
        self.git(self.author, "push", "origin", "HEAD:main")
        updater = GitUpdater()
        updater.root = self.checkout
        updater._fetch_in_background()
        self.wait_for(lambda: updater.available)
        results = []
        updater.pull_finished.connect(lambda *result: results.append(result))
        updater.pull()
        self.wait_for(lambda: results)
        succeeded, moved, _output = results[0]
        self.assertTrue(succeeded and moved)
        self.assertFalse(updater.available)
        self.assertEqual((self.checkout / "file.txt").read_text(), "second")

    def test_pull_without_new_commits_reports_up_to_date(self):
        updater = GitUpdater()
        updater.root = self.checkout
        results = []
        updater.pull_finished.connect(lambda *result: results.append(result))
        updater.pull()
        self.wait_for(lambda: results)
        self.assertEqual(results[0][:2], (True, False))


if __name__ == "__main__":
    unittest.main()
