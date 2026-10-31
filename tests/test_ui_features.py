"""Checks for folder pickers, drag and drop, restart and the release updater."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from winlauncher.platform.git import release_changes
from winlauncher.releases import Release
from winlauncher.ui import messages
from winlauncher.ui.exclude_dialog import (
    ExcludeFolderDialog,
    FolderChoice,
    PatternDialog,
    exclusion_candidates,
)
from winlauncher.ui.messages import MessageDialog
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

    def test_pickers_block_only_their_window_so_quick_switch_can_type(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QFileDialog, QWidget

        from winlauncher.ui.widgets import pick_path

        seen = []

        def run(dialog: QFileDialog) -> int:
            seen.append((dialog.windowModality(), dialog.fileMode(), dialog.nameFilters()))
            dialog.selectFile(str(self.base / "icon.ico"))
            return QDialog.DialogCode.Accepted

        owner = QWidget()
        with (
            patch.object(QFileDialog, "exec", run),
            patch.object(QFileDialog, "selectedFiles", return_value=[str(self.base / "x.ico")]),
        ):
            chosen = pick_path(owner, "Choose icon", "Icons (*.ico);;All files (*)")
            folder = pick_path(owner, "Choose folder")
        self.assertEqual(chosen, self.base / "x.ico")
        self.assertEqual(folder, self.base / "x.ico")
        modal = Qt.WindowModality.WindowModal
        self.assertEqual(
            seen,
            [
                (modal, QFileDialog.FileMode.ExistingFile, ["Icons (*.ico)", "All files (*)"]),
                (modal, QFileDialog.FileMode.Directory, ["All Files (*)"]),
            ],
        )
        with patch.object(QFileDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(pick_path(owner, "Choose folder"))
        owner.deleteLater()

    def test_exclusion_candidates_stop_at_the_owning_root(self):
        root = self.base / "Work"
        folder = root / "a" / "b"
        self.assertEqual(exclusion_candidates(folder, [root]), [folder, folder.parent, root])
        outside = exclusion_candidates(folder, [])
        self.assertEqual(outside[0], folder)
        self.assertNotIn(Path(folder.anchor), outside)
        # A file comes first itself; in a generated folder it is the only choice.
        file = folder / "notes.txt"
        self.assertEqual(exclusion_candidates(file, [root])[:2], [file, folder])
        generated = self.base / "Windows settings"
        setting = generated / "Settings" / "Bluetooth.lnk"
        self.assertEqual(exclusion_candidates(setting, [generated], (generated,)), [setting])

    def test_exclude_dialog_returns_the_selected_parent(self):
        choices = [FolderChoice(self.base / "a" / "b", 3), FolderChoice(self.base / "a", 900)]
        dialog = ExcludeFolderDialog(choices)
        self.assertEqual(dialog.chosen_folder(), self.base / "a" / "b")
        dialog.folders.setCurrentRow(1)
        self.assertEqual(dialog.chosen_folder(), self.base / "a")
        self.assertIn("900", dialog.folders.item(1).text())
        dialog.deleteLater()

    def test_exclude_dialog_custom_rule_starts_from_the_selected_folder(self):
        choices = [FolderChoice(self.base / "a" / "b", 3), FolderChoice(self.base / "a", 900)]
        offered = []

        def edit(editor: PatternDialog) -> int:
            offered.append(editor.field.text())
            editor.field.setText("  **\\a\\b  ")
            return QDialog.DialogCode.Accepted

        dialog = ExcludeFolderDialog(choices)
        dialog.folders.setCurrentRow(1)
        with patch.object(PatternDialog, "exec", edit):
            dialog.edit_pattern()
        self.assertEqual(offered, [str(self.base / "a")])
        self.assertEqual(dialog.pattern, "**\\a\\b")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.deleteLater()
        # Cancelling the editor, or leaving the rule empty, keeps the picker open.
        for result, text in (
            (QDialog.DialogCode.Rejected, "x"),
            (QDialog.DialogCode.Accepted, " "),
        ):
            dialog = ExcludeFolderDialog(choices)
            dialog.show()

            def answer(editor: PatternDialog, result=result, text=text) -> int:
                editor.field.setText(text)
                return result

            with patch.object(PatternDialog, "exec", answer):
                dialog.edit_pattern()
            self.assertIsNone(dialog.pattern)
            self.assertTrue(dialog.isVisible())
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

    def updater(self, published: bool | None = True) -> ReleaseUpdater:
        """`published` answers whether a release's engine wheel exists; GitHub is never asked."""
        updater = ReleaseUpdater()
        updater.root = self.checkout
        updater.current = Release(1, 0, 0)
        self.asked: list[Release] = []

        def ask(release: Release, answer) -> None:
            self.asked.append(release)
            answer(published)

        updater.ask_published = ask
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

    def test_a_found_release_is_fetched_so_its_changes_show_offline(self):
        self.release("second", "v1.1.0")
        updater = self.updater()
        updater.check_now()
        self.wait_for(lambda: updater.available)
        changes = release_changes(self.checkout, "v1.1.0")
        self.assertIn("second", changes.partition("\n\n")[0])  # The commit list comes first.
        self.assertIn("file.txt", changes)
        self.assertIn("+second", changes)
        with patch("winlauncher.platform.git.RELEASE_CHANGES_MAX_CHARS", 20):
            self.assertIn("more characters not shown", release_changes(self.checkout, "v1.1.0"))
        self.assertIsNone(release_changes(self.checkout, "v9.9.9"))

    def test_a_forced_update_stashes_local_changes_that_block_the_normal_one(self):
        self.release("second", "v1.1.0")
        (self.checkout / "file.txt").write_text("local edit")
        (self.checkout / "new.txt").write_text("untracked")
        before = self.git(self.checkout, "rev-parse", "HEAD").strip()
        updater = self.updater()
        updater.latest = Release(1, 1, 0)
        results = []
        updater.update_finished.connect(lambda *result: results.append(result))
        updater.apply()
        self.wait_for(lambda: results)
        self.assertEqual(results[0][:2], (False, False))
        self.assertFalse(updater.forced)
        identity = {
            f"GIT_{role}_{field}": value
            for role in ("AUTHOR", "COMMITTER")
            for field, value in (("NAME", "WinLauncher Test"), ("EMAIL", "test@example.invalid"))
        }
        with patch.dict(os.environ, identity):  # A stash is a commit and needs an identity.
            updater.force_apply()
            self.wait_for(lambda: len(results) == 2)
        succeeded, moved, output = results[1]
        self.assertTrue(succeeded and moved and updater.forced)
        self.assertTrue(output.startswith(f"HEAD {before}"))
        self.assertEqual((self.checkout / "file.txt").read_text(), "second")
        self.assertFalse((self.checkout / "new.txt").exists())
        self.assertIn("forced update", self.git(self.checkout, "stash", "list"))
        self.assertFalse(updater.available)

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

    def test_a_tag_counts_only_once_its_release_is_published(self):
        """The tag starts the release workflow; its engine wheel follows minutes later."""
        self.release("second", "v1.1.0")
        for published, reachable in ((False, True), (None, False)):
            with self.subTest(published=published):
                updater = self.updater(published)
                checks = []
                updater.checked.connect(checks.append)
                updater.check_now()
                self.wait_for(lambda checks=checks: checks)
                self.assertEqual(self.asked, [Release(1, 1, 0)])
                self.assertEqual(checks, [reachable])
                self.assertFalse(updater.available)
                # A missing wheel is asked again soon, not after the usual interval.
                self.assertEqual(updater._unpublished_retry.isActive(), published is False)

    def test_github_answers_map_to_published_or_unknown(self):
        from winlauncher.ui.updater import _published

        for status, expected in ((302, True), (200, True), (404, False), (500, None), (0, None)):
            with self.subTest(status=status):
                reply = Mock()
                reply.attribute.return_value = status or None
                self.assertIs(_published(reply), expected)

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


class MessageDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_details_toggle_names_what_a_click_does_and_loads_once(self):
        loads = []
        dialog = MessageDialog(
            "Update",
            "Text",
            [("Close", messages.SECONDARY)],
            details=lambda: loads.append(1) or "diff",
            details_labels=("Show changes", "Hide changes"),
        )
        dialog.finish()
        dialog.show()
        dialog.toggle_details()
        self.assertTrue(dialog.details.isVisible())
        self.assertEqual(dialog.details_button.text(), "Hide changes")
        dialog.toggle_details()
        self.assertFalse(dialog.details.isVisible())
        self.assertEqual(dialog.details_button.text(), "Show changes")
        self.assertEqual(loads, [1])
        dialog.deleteLater()

    def test_enter_runs_the_main_action_or_cancels_a_destructive_question(self):
        def defaults(buttons):
            dialog = MessageDialog("Title", "Text", buttons)
            names = [button.text() for button in dialog.buttons if button.isDefault()]
            dialog.deleteLater()
            return names

        plain = [("Cancel", messages.SECONDARY), ("Update", messages.PRIMARY)]
        self.assertEqual(defaults(plain), ["Update"])
        destructive = [("Cancel", messages.SECONDARY), ("Remove", messages.DANGER)]
        self.assertEqual(defaults(destructive), ["Cancel"])

    def test_ask_reports_the_clicked_button(self):
        def click(text):
            def run():
                dialog = next(
                    w
                    for w in QApplication.topLevelWidgets()
                    if isinstance(w, MessageDialog) and w.isVisible()
                )
                next(b for b in dialog.buttons if b.text() == text).click()

            QTimer.singleShot(0, run)

        click("Remove")
        self.assertTrue(messages.ask(None, "Remove?", "", "Remove", kind=messages.WARNING))
        click("Cancel")
        self.assertFalse(messages.ask(None, "Remove?", "", "Remove", kind=messages.WARNING))


if __name__ == "__main__":
    unittest.main()


class BarPlacementTest(unittest.TestCase):
    def test_a_position_keeps_the_bar_on_its_screen(self):
        from PySide6.QtCore import QPoint, QRect, QSize

        from winlauncher.ui.bar_placement import RelativePosition

        area = QRect(100, 0, 1000, 800)
        bottom_right = RelativePosition.of(QRect(1500, 900, 200, 50), area)
        self.assertEqual(bottom_right, RelativePosition(1.0, 1.0))
        # A tall bar moves up so it ends at the screen's bottom.
        self.assertEqual(bottom_right.top_left(QSize(200, 300), area), QPoint(900, 500))
        # A bar wider than the screen starts at its left edge.
        self.assertEqual(RelativePosition(0.5, 0).top_left(QSize(1200, 50), area), QPoint(100, 0))

    def test_an_unreadable_file_keeps_the_defaults(self):
        from winlauncher.ui.bar_placement import (
            COMPACT_WIDTH,
            EXPANDED_WIDTH,
            MIN_WIDTH,
            BarPlacement,
        )

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launcher-bar.json"
            for text in ("{", "[]", '{"compact_width": "wide", "position": {"x": "left"}}'):
                path.write_text(text, encoding="utf-8")
                placement = BarPlacement(path)
                self.assertEqual(placement.width(expanded=False), COMPACT_WIDTH)
                self.assertEqual(placement.width(expanded=True), EXPANDED_WIDTH)
                self.assertTrue(placement.centered)
            path.write_text('{"compact_width": 10}', encoding="utf-8")
            self.assertEqual(BarPlacement(path).width(expanded=False), MIN_WIDTH)
