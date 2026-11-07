"""Checks for folder pickers, drag and drop, restart and the release updater."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit, QPushButton

from winlauncher.platform.git import release_changes
from winlauncher.plugins.api import Choice, PluginOption
from winlauncher.releases import Release
from winlauncher.ui import messages
from winlauncher.ui.exclude_dialog import (
    ExcludeFolderDialog,
    FolderChoice,
    PatternDialog,
    exclusion_candidates,
)
from winlauncher.ui.messages import MessageDialog
from winlauncher.ui.plugin_dialogs import ChecklistDialog, FormDialog, choose_items, fill_form
from winlauncher.ui.plugins import Questions
from winlauncher.ui.settings import PathList, dropped_folders
from winlauncher.ui.updater import ReleaseUpdater
from winlauncher.ui.widgets import ToggleSwitch


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

    def test_checks_keep_finding_new_releases_after_an_update_is_available(self):
        self.release("second", "v1.1.0")
        updater = self.updater()
        notices, checks = [], []
        updater.availability_changed.connect(notices.append)
        updater.checked.connect(checks.append)
        updater.check_now()
        self.wait_for(lambda: checks)
        self.assertEqual(updater.latest, Release(1, 1, 0))

        updater._check_in_background()
        self.wait_for(lambda: len(checks) == 2)
        self.assertEqual(notices, [True])  # The same version is announced only once.

        self.release("third", "v1.2.0")
        updater._check_in_background()
        self.wait_for(lambda: len(checks) == 3)
        self.assertEqual(updater.latest, Release(1, 2, 0))
        self.assertEqual(notices, [True, True])
        self.assertIn("third", release_changes(self.checkout, "v1.2.0"))

    def test_an_unpublished_newer_tag_keeps_the_known_update_and_retries(self):
        self.release("second", "v1.1.0")
        updater = self.updater(published=False)
        updater.latest = Release(1, 0, 1)
        checks = []
        updater.checked.connect(checks.append)
        updater._check_in_background()
        self.wait_for(lambda: checks)
        self.assertEqual(checks, [True])
        self.assertEqual(updater.latest, Release(1, 0, 1))
        self.assertTrue(updater._unpublished_retry.isActive())

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


class PluginDialogsTest(unittest.TestCase):
    """The checklist and the form plugin commands show through their context."""

    CHOICES = (
        Choice("user:1", "Anna Schmidt", checked=True),
        Choice("supergroup:2", "Team Annahme", "Group", checked=True),
        Choice("channel:3", "Tech News", "Channel"),
    )

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_filter_and_helpers_touch_only_visible_rows(self):
        others = tuple(Choice(f"user:{n}", f"Person {n}") for n in range(10, 17))
        dialog = ChecklistDialog("Chats", "Ticked chats are listed.", self.CHOICES + others)
        self.assertTrue(dialog.filter.isVisibleTo(dialog))
        self.assertEqual(dialog.ticked(), {"user:1", "supergroup:2"})
        dialog.filter.setText("ANN group")
        hidden = [dialog.rows.topLevelItem(row).isHidden() for row in range(3)]
        self.assertEqual(hidden, [True, False, True])
        next(b for b in dialog.findChildren(QPushButton) if b.text() == "None").click()
        self.assertEqual(dialog.ticked(), {"user:1"})
        dialog.filter.clear()
        next(b for b in dialog.findChildren(QPushButton) if b.text() == "All").click()
        self.assertEqual(len(dialog.ticked()), len(self.CHOICES + others))
        dialog.deleteLater()

    def test_a_short_checklist_shows_its_rows_without_a_filter(self):
        dialog = ChecklistDialog("Exclude", "", self.CHOICES)
        self.assertFalse(dialog.filter.isVisibleTo(dialog))
        labels = {button.text() for button in dialog.findChildren(QPushButton)}
        self.assertEqual(labels, {"Cancel", "Save"})
        dialog.deleteLater()

    def test_save_returns_the_ticked_keys_and_cancel_none(self):
        def answer(save: bool):
            def run(dialog: QDialog) -> int:
                if save:
                    dialog.accept()
                else:
                    dialog.reject()
                return dialog.result()

            return patch.object(ChecklistDialog, "exec", run)

        with answer(True):
            self.assertEqual(
                choose_items(None, "Chats", "", self.CHOICES), {"user:1", "supergroup:2"}
            )
        with answer(False):
            self.assertIsNone(choose_items(None, "Chats", "", self.CHOICES))

    def test_a_form_edits_its_fields_with_the_settings_editors(self):
        fields = (
            PluginOption("names", "Names", "anna", multiline=True),
            PluginOption("unread", "Unread only", False),
            PluginOption("limit", "At most", 50, bounds=(1, 500)),
        )
        dialog = FormDialog("Filter", "", fields)
        self.assertEqual(dialog.values(), {"names": "anna", "unread": False, "limit": 50})
        dialog.findChild(QPlainTextEdit).setPlainText("anna\nbob")
        dialog.findChild(ToggleSwitch).setChecked(True)
        self.assertEqual(dialog.values(), {"names": "anna\nbob", "unread": True, "limit": 50})
        dialog.deleteLater()

        def cancel(dialog: QDialog) -> int:
            dialog.reject()
            return dialog.result()

        with patch.object(FormDialog, "exec", cancel):
            self.assertIsNone(fill_form(None, "Filter", "", fields))

    def test_a_worker_waits_for_the_checklist_on_the_gui_thread(self):
        questions = Questions()
        answers = []
        shown_on = []

        def shown(*_args):
            shown_on.append(threading.current_thread())
            return frozenset({"channel:3"})

        with patch("winlauncher.ui.plugins.choose_items", shown):
            worker = threading.Thread(
                target=lambda: answers.append(questions.choose(None, "Chats", "", self.CHOICES))
            )
            worker.start()
            while worker.is_alive():
                self.app.processEvents()
            worker.join()
        self.assertEqual(answers, [frozenset({"channel:3"})])
        self.assertEqual(shown_on, [threading.main_thread()])


class WelcomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_folders_in_the_profile_go_by_their_short_names(self):
        from winlauncher.ui.welcome import _folders_text

        home = Path.home()
        text = _folders_text(
            [str(home / "Desktop"), str(home / "OneDrive" / "Documents"), "D:\\Work"]
        )
        self.assertTrue(text.startswith(f"Desktop, {Path('OneDrive') / 'Documents'}, D:\\Work<br>"))
        many = _folders_text([str(home / name) for name in "abcdef"])
        self.assertTrue(many.startswith("a, b, c, d, 2 more<br>"))
        self.assertTrue(_folders_text([]).startswith("No folders yet"))


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
