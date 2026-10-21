"""Checks for folder pickers, drag and drop, restart and the release updater."""

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

from winlauncher.releases import Release
from winlauncher.ui.exclude_dialog import ExcludeFolderDialog, FolderChoice, exclusion_candidates
from winlauncher.ui.settings import PathList, dropped_folders
from winlauncher.ui.updater import ReleaseUpdater


class FolderFeaturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-features-")
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
class ReleaseUpdaterTest(unittest.TestCase):
    """A real local git remote; nothing leaves this computer."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-git-")
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
        identity = ["-c", "user.name=WinLauncher Test", "-c", "user.email=test@example.invalid"]
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

    def updater(self) -> ReleaseUpdater:
        updater = ReleaseUpdater()
        updater.root = self.checkout
        updater.current = Release(1, 0, 0)
        self.addCleanup(updater.stop)
        return updater

    def release(self, text: str, tag: str) -> None:
        self.commit(self.author, text)
        self.git(self.author, "tag", tag)
        self.git(self.author, "push", "origin", "HEAD:main", tag)

    def test_detects_and_applies_a_newer_release(self):
        self.release("second", "v1.1.0")
        self.commit(self.author, "unreleased")
        self.git(self.author, "push", "origin", "HEAD:main")
        updater = self.updater()
        updater._check_in_background()
        self.assertEqual(updater._check.processEnvironment().value("GCM_INTERACTIVE"), "Never")
        self.wait_for(lambda: updater.available)
        self.assertEqual(updater.latest, Release(1, 1, 0))
        results = []
        updater.update_finished.connect(lambda *result: results.append(result))
        updater.apply()
        self.wait_for(lambda: results)
        succeeded, moved, _output = results[0]
        self.assertTrue(succeeded and moved)
        self.assertFalse(updater.available)
        # The release, not the newer unreleased commit.
        self.assertEqual((self.checkout / "file.txt").read_text(), "second")

    def test_unreleased_commits_and_older_tags_are_no_update(self):
        self.release("old", "v0.9.0")
        self.commit(self.author, "unreleased")
        self.git(self.author, "push", "origin", "HEAD:main")
        updater = self.updater()
        checks = []
        updater.checked.connect(checks.append)
        updater.check_now()
        self.wait_for(lambda: checks)
        self.assertEqual(checks, [True])
        self.assertFalse(updater.available)

    def test_checkout_past_the_release_stays_where_it_is(self):
        self.release("second", "v1.1.0")
        self.git(self.checkout, "pull", "--ff-only")
        self.commit(self.checkout, "local work")
        updater = self.updater()
        updater.latest = Release(1, 1, 0)
        results = []
        updater.update_finished.connect(lambda *result: results.append(result))
        updater.apply()
        self.wait_for(lambda: results)
        self.assertEqual(results[0][:2], (True, False))
        self.assertEqual((self.checkout / "file.txt").read_text(), "local work")


if __name__ == "__main__":
    unittest.main()
