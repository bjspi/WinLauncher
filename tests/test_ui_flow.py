"""Regression checks for scheduling and actions while search results change."""

from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QLocale, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from pathscout.actions import ExplorerTarget
from pathscout.catalog import long_paths
from pathscout.catalog.model import CatalogItem, ItemKind
from pathscout.platform.changes import FileChange, FolderChange, WatchFailed, WatchRestored
from pathscout.platform.gesture import ExplorerTyping, InputUnavailable
from pathscout.search.query_parser import parse_query
from pathscout.search.ranking import SearchHit
from pathscout.settings import Settings, SettingsFile
from pathscout.ui.controller import LauncherController
from pathscout.ui.indexing import IndexJobKind
from pathscout.ui.settings import SettingsDialog
from pathscout.ui.tasks import TaskCategory


@dataclass
class DeferredJob:
    category: TaskCategory
    work: object
    on_success: object
    on_failure: object
    done: bool = False


class DeferredTasks:
    """Runs submitted work only when a test calls `finish`, on the test thread."""

    def __init__(self, parent=None):
        self.jobs: list[DeferredJob] = []

    def submit(self, category, work, on_success, on_failure) -> Future:
        self.jobs.append(DeferredJob(category, work, on_success, on_failure))
        return Future()

    def count(self, category: TaskCategory) -> int:
        return sum(job.category == category for job in self.jobs)

    def finish(self, category: TaskCategory) -> None:
        """Complete the newest unfinished job of `category`."""
        job = next(j for j in reversed(self.jobs) if j.category == category and not j.done)
        job.done = True
        job.on_success(job.work())

    def close(self):
        pass


class UiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pathscout-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()
        self.data = self.root.parent / "state"
        SettingsFile(self.data / "settings.json").save(
            Settings(roots=(str(self.root),), index_apps=False)
        )
        with (
            patch("pathscout.ui.controller.BackgroundTasks", DeferredTasks),
            patch.object(LauncherController, "_start_monitor"),
            patch("pathscout.ui.tray.QSystemTrayIcon.isSystemTrayAvailable", return_value=False),
        ):
            self.controller = LauncherController(self.data, hooks=False)
        self.addCleanup(self.controller.close)
        # Never launch real files or Explorer windows from tests.
        self.launch = patch.object(self.controller.actions, "open").start()
        self.reveal = patch.object(self.controller.actions, "reveal").start()
        self.addCleanup(patch.stopall)
        self.app.processEvents()
        self.controller.keyboard_timer.stop()
        self.controller.watch_timer.stop()
        self.controller.window.show()
        self.tasks: DeferredTasks = self.controller.tasks
        self.tasks.finish(TaskCategory.INDEX)
        self.tasks.finish(TaskCategory.SEARCH)
        self.window = self.controller.window
        self.indexing = self.controller.indexing

    def test_filtered_results_show_cached_size_and_modification_date(self):
        modified = datetime(2026, 2, 15, 12, 34).timestamp()
        hit = SearchHit(
            CatalogItem(self.root / "report.json", ItemKind.FILE, size=1536, modified=modified),
            90,
            "Name wildcard",
        )
        query = parse_query("*.json size:>1kb after:2026-02*")
        for explorer in (False, True):
            with self.subTest(explorer=explorer):
                self.window._set_explorer_mode(explorer, self.root)
                read = AssertionError("GUI filesystem read")
                with (
                    patch.object(Path, "stat", side_effect=read),
                    patch.object(long_paths, "stat", side_effect=read),
                    patch.object(long_paths, "lstat", side_effect=read),
                ):
                    self.window.set_results([hit], 1, query=query)
                    self.assertFalse(self.window.grab().isNull())
                item = self.window.results.item(0)
                metadata = item.data(Qt.ItemDataRole.UserRole + 1)
                self.assertIn(QLocale().formattedDataSize(1536), metadata)
                self.assertIn("Modified 2026-02-15 12:34", metadata)
                self.assertIn(metadata, item.text())
        self.window.set_results([hit], 1, query=parse_query("report"))
        self.assertFalse(self.window.results.item(0).data(Qt.ItemDataRole.UserRole + 1))

    def test_scan_requests_coalesce_without_cancelling_active_scan(self):
        child = self.root / "nested"
        child.mkdir()
        self.indexing.request_full()
        active = self.indexing.running
        self.indexing.request_folder(child)
        self.indexing.request_full()
        self.assertIs(self.indexing.running, active)
        self.assertFalse(active.cancel.is_set())
        self.assertIn(child, self.indexing.dirty_folders)
        self.assertTrue(self.indexing.full_scan_pending)
        self.tasks.finish(TaskCategory.INDEX)
        self.assertEqual(self.indexing.running.kind, IndexJobKind.FULL)
        self.tasks.finish(TaskCategory.INDEX)
        self.assertEqual(self.indexing.running.kind, IndexJobKind.FOLDER)
        self.assertNotIn(child, self.indexing.dirty_folders)
        self.tasks.finish(TaskCategory.INDEX)
        self.assertIsNone(self.indexing.running)
        self.assertTrue(self.indexing.periodic.isSingleShot())
        self.assertTrue(self.indexing.periodic.isActive())

    def test_watcher_errors_do_not_schedule_scans_and_file_events_do_not_recurse(self):
        c = self.controller
        c.watch_events.put(WatchFailed(self.root, "fixture"))
        c._drain_watch_events()
        self.assertIsNone(self.indexing.running)
        path = self.root / "report.txt"
        path.write_text("fixture")
        indexer = self.indexing.indexer
        with (
            patch.object(indexer, "refresh_files", wraps=indexer.refresh_files) as refresh,
            patch.object(indexer, "scan", side_effect=AssertionError("Recursive repair")),
        ):
            c.watch_events.put(FileChange(path))
            c._drain_watch_events()
            self.tasks.finish(TaskCategory.INDEX)
        refresh.assert_called_once()
        self.assertEqual(c.store.count(), 1)

    def test_restored_watch_repairs_its_root_and_clears_only_its_problem(self):
        c = self.controller
        other = self.root.parent / "other"
        c.watch_events.put(WatchFailed(self.root, "fixture"))
        c.watch_events.put(WatchFailed(other, "fixture"))
        c._drain_watch_events()
        c.watch_events.put(WatchRestored(self.root))
        c._drain_watch_events()
        self.assertEqual(self.indexing.running.folder, self.root)
        problems = c.status.problem_text()
        self.assertNotIn(str(self.root) + ";", problems)
        self.assertIn(str(other), problems)

    def test_own_data_directory_changes_are_ignored(self):
        c = self.controller
        c.watch_events.put(FolderChange(self.data))
        c.watch_events.put(FileChange(self.data / "catalog.sqlite3"))
        c._drain_watch_events()
        self.assertIsNone(self.indexing.running)
        self.assertFalse(self.indexing.dirty_files)

    def test_partial_repairs_cannot_postpone_periodic_full_scan(self):
        self.indexing.request_folder(self.root)
        self.assertTrue(self.indexing.periodic.isActive())
        self.indexing.periodic.stop()  # Simulate expiration while the repair is running.
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        self.assertEqual(self.indexing.running.kind, IndexJobKind.FULL)
        self.assertFalse(self.indexing.full_scan_pending)
        self.tasks.finish(TaskCategory.INDEX)
        self.assertTrue(self.indexing.periodic.isActive())

    def test_restart_cancels_running_scan_and_discards_queued_repairs(self):
        self.indexing.request_folder(self.root)
        repair = self.indexing.running
        self.indexing.mark_file_dirty(self.root / "stale.txt")
        self.indexing.restart()
        self.assertTrue(repair.cancel.is_set())
        self.assertFalse(self.indexing.dirty_files)
        self.tasks.finish(TaskCategory.INDEX)
        self.assertEqual(self.indexing.running.kind, IndexJobKind.FULL)

    def test_enter_during_debounce_opens_new_result_and_records_matching_query(self):
        first, second = self.root / "first.txt", self.root / "second.txt"
        first.write_text("first")
        second.write_text("second")
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        self.window.query.setText("first")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.assertEqual(self.window.selected_hit().item.path, first)
        self.window.query.setText("second")
        self.assertEqual(self.window.results.count(), 0)
        self.window.open_selected()
        self.assertTrue(self.controller.search.open_pending)
        self.tasks.finish(TaskCategory.SEARCH)
        self.tasks.finish(TaskCategory.ACTION)
        self.launch.assert_called_once_with(second, "second", explorer=None)

    def test_results_reset_selection_for_new_query_and_preserve_for_same_query(self):
        hits = [
            SearchHit(CatalogItem(self.root / name, ItemKind.FILE), 100, "fixture")
            for name in ("first", "second")
        ]
        self.window.set_results(hits, 1)
        self.window.results.setCurrentRow(1)
        self.window.set_results(hits, 1)
        self.assertEqual(self.window.results.currentRow(), 1)
        self.window.set_results(hits, 1, preserve_selection=False)
        self.assertEqual(self.window.results.currentRow(), 0)

    def test_keypad_enter_filter_focus_and_drive_scope_label(self):
        selected = []
        self.window.open_requested.connect(selected.append)
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier
        )
        self.assertTrue(self.window.eventFilter(self.window.query, event))
        self.assertEqual(selected, [None])
        self.window.show()
        self.window.kind_actions[2].trigger()
        self.app.processEvents()
        self.assertIs(self.window.focusWidget(), self.window.query)
        self.assertEqual(self.controller.search.current_query().kind, ItemKind.FILE)
        self.assertTrue(self.window.kind_actions[2].isChecked())
        self.window.set_scope(Path(self.root.anchor))
        self.assertIn(self.root.anchor, self.window.clear_scope.text())
        self.window.clear_scope.click()
        self.assertIsNone(self.window.scope)

    def test_footer_type_buttons_shortcuts_and_typed_filters_agree(self):
        window, search = self.window, self.controller.search
        window.query.setText('report type:app contains:"API key"')
        self.assertEqual(search.current_query().kind, ItemKind.APP)
        self.assertTrue(window.kind_buttons[ItemKind.APP].isChecked())
        window.kind_buttons[ItemKind.FILE].click()
        query = search.current_query()
        self.assertEqual(query.kind, ItemKind.FILE)
        self.assertEqual(query.content_text, "API key")
        self.assertEqual(query.terms, ("report",))
        self.assertNotIn("type:", window.query.text())
        self.assertIs(window.focusWidget(), window.query)
        window.kind_buttons[ItemKind.FILE].click()
        self.assertIsNone(search.current_query().kind)
        window.activateWindow()
        QTest.qWait(20)
        for key, expected in (
            (Qt.Key.Key_1, ItemKind.FILE),
            (Qt.Key.Key_2, ItemKind.FOLDER),
            (Qt.Key.Key_3, ItemKind.APP),
            (Qt.Key.Key_0, None),
        ):
            QTest.keyClick(window.query, key, Qt.KeyboardModifier.AltModifier)
            self.assertEqual(search.current_query().kind, expected)
            self.assertEqual(
                sum(b.isChecked() for b in window.kind_buttons.values()), int(expected is not None)
            )
        for text in ("report type:folder", "report type:dir", "report t:dir"):
            window.query.setText(text)
            self.assertEqual(search.current_query().kind, ItemKind.FOLDER)
            self.assertTrue(window.kind_buttons[ItemKind.FOLDER].isChecked())
            window.set_kind(None)
            self.assertIsNone(search.current_query().kind)
        self.assertFalse(window.footer.isHidden())

    def test_menu_actions_reach_their_intents(self):
        requests = []
        for signal, name in (
            (self.window.settings_requested, "settings"),
            (self.window.rescan_requested, "rescan"),
        ):
            signal.disconnect()  # The controller would open a modal dialog.
            signal.connect(lambda name=name: requests.append(name))
        actions = {action.text(): action for action in self.window.menu_button.menu().actions()}
        actions["Settings…"].trigger()
        actions["Refresh catalog"].trigger()
        actions["Expanded view"].trigger()
        self.assertTrue(self.window.expanded)
        self.assertEqual(requests, ["settings", "rescan"])
        actions["Hide"].trigger()
        self.assertFalse(self.window.isVisible())

    def test_numbered_shortcuts_open_their_row(self):
        hits = [
            SearchHit(CatalogItem(self.root / name, ItemKind.FILE), 100, "fixture")
            for name in ("first", "second", "third")
        ]
        opened = []
        self.window.open_requested.disconnect()
        self.window.open_requested.connect(opened.append)
        self.window.set_results(hits, 1)
        self.window.open_row(2)
        self.window.open_row(8)  # No ninth result: nothing happens.
        self.assertEqual(opened, [hits[2]])

    def test_matched_characters_are_highlighted(self):
        from pathscout.ui.highlight import matched_positions

        self.assertEqual(matched_positions("tomato_salad", ("tom",)), {0, 1, 2})
        self.assertEqual(matched_positions("tabulator_map", ("tom",)), {0, 7, 10})
        self.assertEqual(matched_positions("Straße", ("xyz",)), set())

    def test_unknown_advanced_rule_key_is_rejected(self):
        dialog = SettingsDialog(self.controller.settings)
        dialog.rule_json.setPlainText('{"extension_priority": []}')
        with patch.object(QMessageBox, "warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), 0)
        dialog.deleteLater()

    def test_folder_priority_errors_name_the_line(self):
        dialog = SettingsDialog(self.controller.settings)
        dialog.priorities.setPlainText(f"10 | {self.root}\nmany | {self.root}")
        with self.assertRaisesRegex(ValueError, "line 2"):
            dialog.read_settings()
        dialog.priorities.setPlainText(f"10 | {self.root} | high")
        self.assertEqual(dialog.read_settings().folder_priorities[0].weight, 10)
        dialog.deleteLater()

    def test_hiding_configured_explorer_scope_does_not_request_full_scan(self):
        sub = self.root / "sub"
        sub.mkdir()
        before = self.tasks.count(TaskCategory.INDEX)
        self.window.present(text="a", scope=sub)
        self.window.hide()
        QTest.qWait(600)
        self.assertEqual(self.tasks.count(TaskCategory.INDEX), before)
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.window.present(text="a", scope=outside)
        self.window.hide()
        QTest.qWait(600)
        self.assertEqual(self.tasks.count(TaskCategory.INDEX), before + 1)

    def test_full_scan_waits_while_explorer_session_is_open(self):
        self.window.present(text="a", scope=self.root)
        self.indexing.request_full()
        self.assertIsNone(self.indexing.running)
        self.assertTrue(self.indexing.full_scan_pending)
        self.window.hide()
        self.assertEqual(self.indexing.running.kind, IndexJobKind.FULL)

    def test_context_actions_keep_clicked_item_during_async_refresh(self):
        first = SearchHit(CatalogItem(self.root / "first.txt", ItemKind.FILE), 100, "Fixture")
        second = SearchHit(CatalogItem(self.root / "second.txt", ItemKind.FILE), 99, "Fixture")
        received = []
        for signal in (
            self.window.open_requested,
            self.window.reveal_requested,
            self.window.favorite_requested,
        ):
            signal.disconnect()
            signal.connect(received.append)
        self.window.show()
        original_clipboard = self.app.clipboard().text()
        self.addCleanup(lambda: self.app.clipboard().setText(original_clipboard))

        for action in ("Open", "Show in containing folder", "Add to favorites", "Copy path"):
            with self.subTest(action=action):
                self.window.set_results([first], 1)
                received.clear()
                self.app.clipboard().clear()

                def choose(action=action):
                    self.window.set_results([second], 1)
                    for menu in self.window.findChildren(QMenu):
                        if menu.isVisible():
                            next(item for item in menu.actions() if item.text() == action).trigger()
                            menu.close()

                timer = QTimer(self.window)
                timer.setSingleShot(True)
                timer.timeout.connect(choose)
                timer.start(20)
                self.window.context_menu(
                    self.window.results.visualItemRect(self.window.results.item(0)).center()
                )
                timer.stop()
                timer.deleteLater()
                if action == "Copy path":
                    self.assertEqual(self.app.clipboard().text(), str(first.item.path))
                else:
                    self.assertEqual(received, [first])

    def test_same_query_refresh_does_not_delay_explicit_action(self):
        path = self.root / "report.txt"
        path.write_text("Fixture")
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        self.window.query.setText("report")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        hit = self.window.selected_hit()
        self.controller.search.run()  # A background refresh of the query already displayed.
        self.window.open_requested.emit(hit)
        self.assertFalse(self.controller.search.open_pending)
        self.tasks.finish(TaskCategory.ACTION)
        self.launch.assert_called_once_with(path, "report", explorer=None)

    def test_invalid_query_shows_validation_message_without_searching(self):
        before = self.tasks.count(TaskCategory.SEARCH)
        self.window.query.setText("type:unknown")
        self.controller.search.run()
        self.assertEqual(self.tasks.count(TaskCategory.SEARCH), before)
        self.assertIn("type:", self.window.empty.text())
        self.assertFalse(self.window.empty.isHidden())
        self.assertTrue(self.window.results.isHidden())

    def test_empty_query_is_a_bare_bar_and_unmatched_query_says_so(self):
        self.window.query.setText("")
        self.window.set_results([], 1)
        self.assertTrue(self.window.empty.isHidden())
        self.assertTrue(self.window.results.isHidden())
        self.window.query.setText("zzzz-no-such-item")
        self.window.set_results([], 1)
        self.assertFalse(self.window.empty.isHidden())
        self.assertIn("No matches", self.window.empty.text())

    def test_background_results_are_delivered_after_submit_returns(self):
        from pathscout.ui.tasks import BackgroundTasks

        tasks = BackgroundTasks()
        self.addCleanup(tasks.close)
        events = []
        tasks.submit(TaskCategory.ACTION, lambda: "done", events.append, events.append)
        events.append("submitted")
        for _ in range(100):
            if len(events) == 2:
                break
            QTest.qWait(20)
        self.assertEqual(events, ["submitted", "done"])

    def test_unrepresentable_modified_date_does_not_break_details(self):
        hit = SearchHit(
            CatalogItem(self.root / "old.txt", ItemKind.FILE, modified=1e30), 100, "Fixture"
        )
        self.window.set_results([hit], 1)
        self.window.update_detail()
        self.assertIn("Unknown", self.window.detail.text())

    def test_input_and_watch_diagnostics_survive_index_updates(self):
        c = self.controller
        c.keyboard_events.put(InputUnavailable("Keyboard unavailable"))
        c._drain_keyboard()
        c.watch_events.put(WatchFailed(self.root, "Watch failed"))
        c._drain_watch_events()
        self.indexing.request_full()
        self.indexing.progress.emit(500)
        self.tasks.finish(TaskCategory.INDEX)
        for text in (self.window.activity.text(), c.tray.icon.toolTip()):
            self.assertIn("Keyboard unavailable", text)
            self.assertIn("Folder watch unavailable", text)

    def test_ui_settings_preserve_scope_and_do_not_restart_index_or_monitor(self):
        c = self.controller
        self.window.set_scope(self.root)
        before = self.tasks.count(TaskCategory.INDEX)
        with patch.object(c, "_start_monitor") as monitor:
            c.apply_settings(replace(c.settings, max_results=20))
        monitor.assert_not_called()
        self.assertEqual(self.tasks.count(TaskCategory.INDEX), before)
        self.assertEqual(self.window.scope, self.root)
        self.assertEqual(c.settings_file.load().max_results, 20)

    def test_indexing_settings_restart_monitor_and_rescan(self):
        c = self.controller
        before = self.tasks.count(TaskCategory.INDEX)
        with patch.object(c, "_start_monitor") as monitor:
            c.apply_settings(replace(c.settings, excluded_patterns=("build",)))
        monitor.assert_called_once()
        self.assertEqual(self.tasks.count(TaskCategory.INDEX), before + 1)

    def test_calculation_is_selected_copied_and_left_with_the_arrow_keys(self):
        window = self.window
        (self.root / "report.txt").write_text("Fixture")
        sum_file = self.root / "1+1.txt"
        sum_file.write_text("Fixture")
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        window.query.setText("report")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.assertTrue(window.calculation_row.isHidden())
        window.query.setText("200 + 10%")
        self.assertFalse(window.calculation_row.isHidden())
        self.assertTrue(window.calculation_selected)
        self.assertIn("= 220", window.calculation_row.result.text())
        QTest.keyClick(window.query, Qt.Key.Key_Return)
        self.assertEqual(QApplication.clipboard().text(), "220")
        self.launch.assert_not_called()
        # A file whose name is also a calculation: the result row comes first.
        window.query.setText("1+1")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.assertEqual(window.results.item(0).data(Qt.ItemDataRole.UserRole).item.path, sum_file)
        self.assertTrue(window.calculation_selected)
        self.assertIsNone(window.selected_hit())
        QTest.keyClick(window.query, Qt.Key.Key_Down)
        self.assertFalse(window.calculation_selected)
        self.assertEqual(window.selected_hit().item.path, sum_file)
        QTest.keyClick(window.query, Qt.Key.Key_Up)
        self.assertTrue(window.calculation_selected)
        window.query.setText("1+1 report")
        self.assertTrue(window.calculation_row.isHidden())
        self.assertFalse(window.calculation_selected)

    def test_number_fields_show_values_and_clamp_typed_numbers(self):
        dialog = SettingsDialog(self.controller.settings)
        dialog.show()
        QApplication.processEvents()
        field = dialog.content_threads
        self.assertEqual(field.text(), str(self.controller.settings.content_search_threads))
        # Qt 6.12 collapsed the text area of styled spin boxes to one pixel.
        self.assertGreater(field.width(), 100)
        field.setText("")
        self.assertEqual(field.value(), self.controller.settings.content_search_threads)
        field.setText("99")
        self.assertEqual(field.value(), 16)
        field.setValue(4)
        QTest.keyClick(field, Qt.Key.Key_Up)
        self.assertEqual(field.text(), "5")
        dialog.deleteLater()

    def test_search_settings_save_and_refresh_without_reindexing(self):
        c = self.controller
        self.window.set_scope(self.root)
        indexed = self.tasks.count(TaskCategory.INDEX)
        for threads in (1, 8):
            with self.subTest(threads=threads):
                dialog = SettingsDialog(c.settings, save=c.apply_settings)
                dialog.content_threads.setValue(threads)
                c.search.run()
                previous_cancel = c.search.running
                with patch.object(c, "_start_monitor") as monitor:
                    dialog.accept()
                self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
                self.assertEqual(c.settings_file.load().content_search_threads, threads)
                self.assertTrue(previous_cancel.is_set())
                self.assertEqual(self.tasks.count(TaskCategory.INDEX), indexed)
                self.assertEqual(self.window.scope, self.root)
                monitor.assert_not_called()
                with patch("pathscout.search.service.select_native", return_value=None) as select:
                    self.tasks.finish(TaskCategory.SEARCH)
                select.assert_called_once_with()
                dialog.deleteLater()

    def test_hiding_cancels_running_and_debounced_searches(self):
        self.controller.search.run()
        running = self.controller.search.running
        self.controller.search.debounce.start()
        self.window.hide()
        self.assertTrue(running.is_set())
        self.assertIsNone(self.controller.search.running)
        self.assertFalse(self.controller.search.debounce.isActive())

    def test_home_shortcut_opens_an_unindexed_folder(self):
        folder = self.root.parent / ".toolbox"
        folder.mkdir()
        with patch.dict(os.environ, USERPROFILE=str(self.root.parent), HOME=str(self.root.parent)):
            self.window.query.setText("~/.toolbox")
            self.controller.search.run()
            self.tasks.finish(TaskCategory.SEARCH)
            self.assertEqual(self.window.selected_hit().item.path, folder)
            self.window.open_selected()
            self.tasks.finish(TaskCategory.ACTION)
        self.launch.assert_called_once_with(folder, "navigate:~/.toolbox", explorer=None)

    def test_explorer_typing_opens_live_folder_in_original_window_on_one_click(self):
        self.window.hide()
        folder = self.root / "toolbox-cli"
        folder.mkdir()
        (folder / ".nobackup").touch()
        intent = ExplorerTyping("toolbox", self.root, (0, 0, 1000, 800), (11, 22))
        self.controller._search_from_explorer(intent)
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.assertTrue(self.controller.search.current_query().explorer)
        self.assertEqual(self.window.selected_hit().item.path, folder)
        self.assertLessEqual(self.window.delegate.rows.height, 28)
        self.assertLess(self.window.width(), 520)
        self.window.results.itemClicked.emit(self.window.results.item(0))
        self.assertFalse(self.window.isVisible())
        self.tasks.finish(TaskCategory.ACTION)
        self.launch.assert_called_once_with(
            folder, "toolbox", explorer=ExplorerTarget(11, 22, self.root)
        )
        self.window.present(text="toolbox")
        self.assertFalse(self.window.explorer_mode)
        self.assertIsNone(self.window.explorer_view)
        self.assertFalse(self.controller.search.current_query().explorer)

    def test_clearing_explorer_scope_restores_global_launcher(self):
        self.window.present(scope=self.root, near=(0, 0, 1000, 800), explorer_view=(11, 22))
        self.window.set_scope(None)
        self.assertFalse(self.window.explorer_mode)
        self.assertIsNone(self.window.explorer_view)
        self.assertFalse(self.controller.search.current_query().explorer)

    def test_settings_save_failure_keeps_dialog_and_edits(self):
        save = Mock(side_effect=OSError("Disk unavailable"))
        dialog = SettingsDialog(self.controller.settings, save=save)
        dialog.show()
        dialog.limit.setValue(25)
        with patch.object(QMessageBox, "warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.limit.value(), 25)
        save.side_effect = None
        dialog.accept()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual(dialog.result_settings.max_results, 25)
        dialog.deleteLater()

    def test_hidden_completions_do_not_submit_searches(self):
        self.window.hide()
        self.indexing.request_full()
        before = self.tasks.count(TaskCategory.SEARCH)
        self.tasks.finish(TaskCategory.INDEX)
        self.controller.clear_history()
        self.tasks.finish(TaskCategory.ACTION)
        self.assertEqual(self.tasks.count(TaskCategory.SEARCH), before)
        self.assertIsNone(self.controller.search.displayed_query)

    def test_favorite_repeats_wait_for_pending_operation(self):
        c = self.controller
        hit = SearchHit(CatalogItem(self.root / "favorite.txt", ItemKind.FILE), 1, "Fixture")
        before = self.tasks.count(TaskCategory.ACTION)
        c.favorite_item(hit)
        c.favorite_item(hit)
        self.assertEqual(self.tasks.count(TaskCategory.ACTION), before + 1)
        self.tasks.finish(TaskCategory.ACTION)
        self.assertFalse(c.favorite_pending)

    def test_restart_starts_a_replacement_and_quits(self):
        c = self.controller
        with (
            patch("pathscout.ui.controller.QProcess.startDetached", return_value=True) as start,
            patch("pathscout.ui.controller.QApplication.instance") as application,
        ):
            c.restart()
        _program, arguments, _cwd = start.call_args.args
        self.assertEqual(arguments[:3], ["-m", "pathscout", "--replace"])
        self.assertIn(str(c.data_dir), arguments)
        self.assertIn("--no-hooks", arguments)
        self.assertIn("--show", arguments)  # The launcher was open, so it reopens.
        application.return_value.quit.assert_called_once()

    def test_exclude_picker_adds_the_chosen_parent(self):
        c = self.controller
        folder = self.root / "project" / "node_stuff" / "deep"
        folder.mkdir(parents=True)
        with patch("pathscout.ui.controller.ExcludeFolderDialog") as dialog:
            dialog.return_value.exec.return_value = True
            dialog.return_value.chosen_folder.return_value = folder.parent
            c.exclude_folder(folder)
        choices = dialog.call_args.args[0]
        self.assertEqual([choice.folder for choice in choices][-1], self.root)
        self.assertIn(str(folder.parent), c.settings.excluded_paths)

    def test_autostart_tray_and_settings_share_state_and_restore_on_error(self):
        c = self.controller
        startup = c.startup = Mock()
        startup.is_enabled.return_value = False
        c._sync_autostart()
        self.assertFalse(c.tray.autostart.isChecked())
        startup.is_enabled.return_value = True
        c.tray.autostart.trigger()
        startup.set_enabled.assert_called_once_with(True)
        self.assertTrue(c.tray.autostart.isChecked())
        dialog = SettingsDialog(c.settings, startup_enabled=True, set_startup=c._set_autostart)
        startup.set_enabled.side_effect = OSError("Access denied")
        with patch.object(QMessageBox, "warning") as warning:
            dialog.autostart.click()
        warning.assert_called_once()
        self.assertTrue(dialog.autostart.isChecked())
        self.assertTrue(c.tray.autostart.isChecked())
        dialog.deleteLater()

    def test_tray_failure_restores_actual_autostart_state(self):
        c = self.controller
        startup = c.startup = Mock()
        startup.is_enabled.return_value = False
        startup.set_enabled.side_effect = OSError("Access denied")
        with patch.object(QMessageBox, "warning") as warning:
            c.tray.autostart.trigger()
        warning.assert_called_once()
        self.assertFalse(c.tray.autostart.isChecked())


if __name__ == "__main__":
    unittest.main()
