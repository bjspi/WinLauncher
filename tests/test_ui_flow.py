"""Regression checks for scheduling and actions while search results change."""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QLocale, Qt, QTimer
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMenu

import winlauncher
from winlauncher.actions import ExplorerTarget
from winlauncher.catalog import long_paths
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.commands import MenuCommand
from winlauncher.platform.changes import FileChange, FolderChange, WatchFailed, WatchRestored
from winlauncher.platform.gesture import (
    ExplorerTyping,
    FolderJumpRequested,
    InputUnavailable,
    ViewKind,
)
from winlauncher.search.native_engine import warm_search_index
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import SearchHit
from winlauncher.settings import Settings, SettingsFile
from winlauncher.ui.controller import LauncherController
from winlauncher.ui.file_transfer import DROPEFFECT_COPY, DROPEFFECT_MOVE, PREFERRED_DROP_EFFECT
from winlauncher.ui.indexing import IndexJobKind
from winlauncher.ui.settings import NUMBER_FIELD_WIDTH, SettingsDialog
from winlauncher.ui.tasks import TaskCategory
from winlauncher.ui.theme import colors
from winlauncher.ui.window import EXPLORER_RIGHT_GAP, EXPLORER_WIDTH, SHADOW_MARGIN
from winlauncher.websites import WebsiteQuery, WebsiteShortcut, WebsiteTarget

DIALOG = ViewKind.FILE_DIALOG


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
        self.temporary = tempfile.TemporaryDirectory(prefix="winlauncher-flow-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.root.mkdir()
        self.data = self.root.parent / "state"
        SettingsFile(self.data / "settings.json").save(
            Settings(roots=(str(self.root),), index_apps=False, index_windows_tools=False)
        )
        with (
            patch("winlauncher.ui.controller.BackgroundTasks", DeferredTasks),
            patch.object(LauncherController, "_start_monitor"),
            patch("winlauncher.ui.tray.QSystemTrayIcon.isSystemTrayAvailable", return_value=False),
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
        # Never fetch favicons from the network in tests.
        websites = self.controller.website_icons
        patch.object(websites, "icon_for", return_value=websites.fallback).start()

    def settings_dialog(self, settings: Settings, **options) -> SettingsDialog:
        return SettingsDialog(settings, website_icons=self.controller.website_icons, **options)

    def test_website_enter_waits_for_current_query_and_keeps_local_results(self):
        site = WebsiteShortcut("az", "Amazon", "https://www.amazon.de/s?k=%s")
        self.controller.settings = replace(self.controller.settings, websites=(site,))
        (self.root / "az Book.txt").touch()
        local = SearchHit(CatalogItem(self.root / "az Book.txt", ItemKind.FILE), 900, "Exact name")
        search = patch.object(self.controller.search.service, "search", return_value=[local])
        search = search.start()
        self.window.query.setText("az old")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        stale = self.window.selected_hit()
        self.window.query.setText("az C++ Book & More")
        # The old results stay visible while the new search runs; nothing flickers.
        self.assertEqual(self.window.selected_hit(), stale)
        with patch(
            "winlauncher.ui.controller.QDesktopServices.openUrl", return_value=True
        ) as browser:
            self.controller.search.request_open(stale)
            browser.assert_not_called()
            self.tasks.finish(TaskCategory.SEARCH)
            browser.assert_called_once()
            self.assertEqual(
                bytes(browser.call_args.args[0].toEncoded()).decode(),
                "https://www.amazon.de/s?k=C%2B%2B%20Book%20%26%20More",
            )
        # The catalog search is the typed text itself, so its history and highlighting work.
        self.assertEqual(search.call_args.args[0], parse_query("az C++ Book & More"))
        first = self.window.results.item(0).data(Qt.ItemDataRole.UserRole)
        self.assertIsInstance(first.hit, WebsiteTarget)
        self.assertEqual(first.name, "Search Amazon for C++ Book & More")
        self.assertIsNone(first.file_hit)
        self.assertEqual(self.window.results.item(1).data(Qt.ItemDataRole.UserRole).hit, local)
        self.launch.assert_not_called()
        self.window.show()
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.window.results.setCurrentRow(1)
        self.window.open_selected()
        self.tasks.finish(TaskCategory.ACTION)
        self.launch.assert_called_once()
        self.assertEqual(
            self.launch.call_args.args[1], parse_query("az C++ Book & More").history_key
        )
        self.window.show()
        self.window.results.setCurrentRow(0)
        self.window.copy_target()
        self.assertEqual(self.app.clipboard().text(), first.target)
        self.assertEqual(self.window.status.text(), "URL copied to clipboard")
        self.window.reveal_selected()
        self.window.favorite_selected()
        self.reveal.assert_not_called()
        self.assertFalse(self.controller.favorite_pending)

    def test_website_keywords_respect_scope_filters_and_literal_search_text(self):
        site = WebsiteShortcut("yt", "YouTube", "https://www.youtube.com/results?search_query=%s")
        self.controller.settings = replace(self.controller.settings, websites=(site,))
        self.window.query.setText('yt "Unfinished type:unknown')
        query = self.controller.search.current_query()
        self.assertIsInstance(query, WebsiteQuery)
        hits = self.controller.search._work(query, self.controller.settings, threading.Event()).hits
        self.assertEqual(len(hits), 1)
        self.assertIn("%22Unfinished%20type%3Aunknown", hits[0].url)
        self.window.query.setText("yt tutorial")
        self.window.kind = ItemKind.FILE
        self.assertNotIsInstance(self.controller.search.current_query(), WebsiteQuery)
        self.window.kind = None
        self.window.scope = self.root
        self.assertNotIsInstance(self.controller.search.current_query(), WebsiteQuery)

    def test_website_browser_failure_is_reported(self):
        hit = WebsiteTarget("Example", "ex", "https://example.org/")
        with (
            patch("winlauncher.ui.controller.QDesktopServices.openUrl", return_value=False),
            patch("winlauncher.ui.controller.messages.message") as warning,
        ):
            self.controller.open_item(hit, "")
        warning.assert_called_once()
        self.launch.assert_not_called()

    def show_file_result(self, name: str = "report.txt") -> SearchHit:
        path = self.root / name
        path.touch()
        hit = SearchHit(CatalogItem(path, ItemKind.FILE), 100, "Fixture")
        self.window.set_results([hit], 1)
        return hit

    def test_copy_and_cut_put_the_selected_file_on_the_clipboard_for_explorer(self):
        hit = self.show_file_result()
        for key, effect in ((Qt.Key.Key_C, DROPEFFECT_COPY), (Qt.Key.Key_X, DROPEFFECT_MOVE)):
            with self.subTest(key=key):
                self.app.clipboard().clear()
                QTest.keyClick(self.window.query, key, Qt.KeyboardModifier.ControlModifier)
                data = self.app.clipboard().mimeData()
                self.assertEqual([Path(url.toLocalFile()) for url in data.urls()], [hit.item.path])
                self.assertEqual(
                    struct.unpack("<I", data.data(PREFERRED_DROP_EFFECT).data()), (effect,)
                )
        # Selected query text keeps the ordinary text copy.
        self.window.query.setText("report")
        self.window.query.selectAll()
        QTest.keyClick(self.window.query, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), "report")
        self.assertFalse(self.app.clipboard().mimeData().hasUrls())

    def test_windows_context_menu_opens_from_the_menu_or_with_shift(self):
        hit = self.show_file_result()
        point = self.window.results.visualItemRect(self.window.results.item(0)).center()
        with patch.object(self.controller, "show_shell_menu") as shell_menu:
            with patch.object(
                QApplication,
                "keyboardModifiers",
                return_value=Qt.KeyboardModifier.ShiftModifier,
            ):
                self.window.context_menu(point)  # Shift skips WinLauncher's own menu.
            shell_menu.assert_called_once_with(hit, True)

            def choose_more_options():
                for menu in self.window.findChildren(QMenu):
                    if menu.isVisible():
                        actions = {a.text().partition("\t")[0]: a for a in menu.actions()}
                        menu.close()
                        actions["More options…"].trigger()

            QTimer.singleShot(20, choose_more_options)
            self.window.context_menu(point)
            shell_menu.assert_called_with(hit, False)

    def test_own_menu_commands_show_for_their_kind_and_run(self):
        folder = self.root / "project"
        folder.mkdir()
        terminal = MenuCommand("Terminal here", "cmd.exe")
        editor = MenuCommand("Edit", 'code "{path}"', folders=False, files=True)
        c = self.controller
        c.apply_settings(replace(c.settings, menu_commands=(terminal, editor)))
        file_hit = self.show_file_result()
        folder_hit = SearchHit(CatalogItem(folder, ItemKind.FOLDER), 100, "Fixture")
        app_hit = SearchHit(CatalogItem(self.root / "Tool.lnk", ItemKind.APP), 100, "Fixture")
        shown = {}

        def choose(kind: ItemKind):
            for menu in self.window.findChildren(QMenu):
                if menu.isVisible():
                    actions = {a.text().partition("\t")[0]: a for a in menu.actions()}
                    shown[kind] = actions.keys()
                    menu.close()
                    if kind == ItemKind.FOLDER:
                        shown["icon"] = actions[terminal.title].icon()
                        actions[terminal.title].trigger()

        for hit in (file_hit, app_hit, folder_hit):
            self.window.set_results([hit], 1)
            point = self.window.results.visualItemRect(self.window.results.item(0)).center()
            QTimer.singleShot(20, partial(choose, hit.item.kind))
            self.window.context_menu(point)
        self.assertIn(editor.title, shown[ItemKind.FILE])
        self.assertNotIn(terminal.title, shown[ItemKind.FILE])
        self.assertTrue({editor.title, terminal.title}.isdisjoint(shown[ItemKind.APP]))
        self.assertIn(terminal.title, shown[ItemKind.FOLDER])
        self.assertNotIn(editor.title, shown[ItemKind.FOLDER])
        self.assertFalse(self.window.isVisible())
        with patch("winlauncher.actions.shell.run_command") as run:
            self.tasks.finish(TaskCategory.ACTION)
        run.assert_called_once_with("cmd.exe", folder)
        if os.name == "nt":  # The icon of cmd.exe, the program the command runs.
            self.assertFalse(shown["icon"].isNull())

    @unittest.skipUnless(os.name == "nt", "Windows shell context menus")
    def test_windows_context_menu_is_built_by_the_shell(self):
        from winlauncher.platform.shell_menu import menu_item_count

        hit = self.show_file_result()
        self.assertGreater(menu_item_count(hit.item.path), 0)
        self.assertGreater(menu_item_count(self.root), 0)
        with self.assertRaisesRegex(OSError, "no context menu"):
            menu_item_count(self.root / "missing.txt")

    def test_dragging_a_result_offers_its_file_or_url_without_moving_it(self):
        hit = self.show_file_result()
        data = self.window.results.mimeData([self.window.results.item(0)])
        self.assertEqual([Path(url.toLocalFile()) for url in data.urls()], [hit.item.path])
        website = WebsiteTarget("Example", "ex", "https://example.org/")
        self.window.set_results([website], 1)
        data = self.window.results.mimeData([self.window.results.item(0)])
        self.assertEqual(data.text(), "https://example.org/")
        self.assertTrue(self.window.results.dragEnabled())

    def test_footer_hint_follows_what_enter_acts_on(self):
        self.show_file_result()
        self.assertIn("Ctrl+C copy file", self.window.hint.text())
        self.window.set_results([WebsiteTarget("Example", "ex", "https://example.org/")], 1)
        self.assertIn("open in browser", self.window.hint.text())
        self.window.query.setText("2 + 2")
        self.assertEqual(self.window.hint.text(), "↵ copy result")
        self.window.query.clear()
        self.window.set_results([], 1)
        self.assertTrue(self.window.hint.isHidden())

    def test_unmatched_words_offer_a_content_search_on_enter(self):
        self.window.query.setText("tax notice")
        with patch("winlauncher.ui.search_session.native_availability") as availability:
            availability.return_value.module = object()
            self.controller.search.run()
            self.tasks.finish(TaskCategory.SEARCH)
        self.assertEqual(self.window.content_search, 'contains:"tax notice"')
        self.assertIn("Search inside files", self.window.empty.text())
        QTest.keyClick(self.window.query, Qt.Key.Key_Return)
        self.assertEqual(self.window.query.text(), 'contains:"tax notice"')
        # Query syntax is never rewritten into a content search.
        self.window.query.setText("ext:pdf tax")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.assertIsNone(self.window.content_search)

    def test_deleted_files_are_dimmed_and_refresh_their_folder_instead_of_opening(self):
        gone = SearchHit(CatalogItem(self.root / "gone.txt", ItemKind.FILE), 100, "Fixture")
        kept = self.show_file_result("kept.txt")
        results = self.controller.search._work(
            parse_query("x"), self.controller.settings, threading.Event()
        )
        self.assertEqual(results.missing, frozenset())
        with patch.object(self.controller.search.service, "search", return_value=[gone, kept]):
            results = self.controller.search._work(
                parse_query("x"), self.controller.settings, threading.Event()
            )
        self.assertEqual(results.missing, frozenset({gone.item.path}))
        self.window.set_results(results.hits, 1, missing=results.missing)
        rows = [self.window.results.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)]
        self.assertEqual([row.missing for row in rows], [True, False])
        self.assertTrue(rows[0].location.startswith("Deleted"))
        with patch.object(self.indexing, "request_folder") as refresh:
            self.controller.open_item(gone, "x")
        refresh.assert_called_once_with(self.root)
        self.launch.assert_not_called()
        self.assertIn("no longer exists", self.window.status.text())

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
                metadata = item.data(Qt.ItemDataRole.UserRole).metadata
                self.assertIn(QLocale().formattedDataSize(1536), metadata)
                self.assertIn("Modified 2026-02-15 12:34", metadata)
                self.assertIn(metadata, item.text())
        self.window.set_results([hit], 1, query=parse_query("report"))
        self.assertFalse(self.window.results.item(0).data(Qt.ItemDataRole.UserRole).metadata)

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
        self.assertEqual(c.store.count(), 2)  # The configured folder and the report.

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
        # The old result stays visible, but Enter waits for the typed query's results.
        self.assertEqual(self.window.selected_hit().item.path, first)
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

    def test_gear_opens_the_settings_outside_the_explorer_bar(self):
        requests = []
        self.window.settings_requested.disconnect()  # The controller would open a modal dialog.
        self.window.settings_requested.connect(lambda: requests.append("settings"))
        self.window.settings_button.click()
        self.assertEqual(requests, ["settings"])
        self.window._set_explorer_mode(True, self.root)
        self.assertTrue(self.window.settings_button.isHidden())
        self.window._set_explorer_mode(False, None)
        self.assertFalse(self.window.settings_button.isHidden())

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

    def test_command_shortcuts_run_their_command_instead_of_opening(self):
        claude = MenuCommand("Claude", "claude", shortcut="Alt")
        codex = MenuCommand("Codex", "codex", shortcut="Alt+Shift")
        self.window.set_menu_commands((claude, codex))
        folders = [
            SearchHit(CatalogItem(self.root / name, ItemKind.FOLDER), 100, "fixture")
            for name in ("first", "second")
        ]
        file = SearchHit(CatalogItem(self.root / "notes.txt", ItemKind.FILE), 100, "fixture")
        self.window.set_results([*folders, file], 1)
        opened, ran = [], []
        self.window.open_requested.disconnect()
        self.window.open_requested.connect(opened.append)
        self.window.command_requested.disconnect()
        self.window.command_requested.connect(lambda hit, command: ran.append((hit, command)))
        alt, shift = Qt.KeyboardModifier.AltModifier, Qt.KeyboardModifier.ShiftModifier
        ctrl = Qt.KeyboardModifier.ControlModifier
        self.window.results.setCurrentRow(0)
        QTest.keyClick(self.window.query, Qt.Key.Key_Return, alt)
        # Shift+1 types "!" and AltGr+2 "²"; the virtual key still names the digit.
        exclam = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Exclam, ctrl | alt | shift, 0, ord("1"), 0, "!"
        )
        QApplication.sendEvent(self.window.query, exclam)
        QTest.keyClick(self.window.query, Qt.Key.Key_2, ctrl | alt)
        item = self.window.results.item(1)
        point = self.window.results.visualItemRect(item).center()
        with patch("winlauncher.ui.window.QApplication.keyboardModifiers", return_value=alt):
            QTest.mouseClick(self.window.results.viewport(), Qt.MouseButton.LeftButton, alt, point)
            QTest.mouseDClick(self.window.results.viewport(), Qt.MouseButton.LeftButton, alt, point)
        self.assertEqual(
            ran,
            [(folders[0], claude), (folders[0], codex), (folders[1], claude), (folders[1], claude)],
        )
        self.assertEqual(opened, [])
        # Files have no Alt command; unused modifiers keep their old meaning.
        QTest.keyClick(self.window.query, Qt.Key.Key_3, ctrl | alt)
        self.assertEqual(len(ran), 4)
        self.assertIn("No command uses Alt for notes.txt", self.window.status.text())
        self.assertFalse(self.window.run_shortcut(self.window.selected_row(), "Shift"))
        menu_titles = []
        with patch(
            "winlauncher.ui.window.Menu.exec",
            lambda menu, _point: menu_titles.extend(action.text() for action in menu.actions()),
        ):
            self.window.context_menu(
                self.window.results.visualItemRect(self.window.results.item(0)).center()
            )
        self.assertIn("Claude\tAlt+Enter", menu_titles)

    def test_matched_characters_are_highlighted(self):
        from winlauncher.ui.highlight import matched_positions

        self.assertEqual(matched_positions("tomato_salad", ("tom",)), {0, 1, 2})
        self.assertEqual(matched_positions("tabulator_map", ("tom",)), {0, 7, 10})
        self.assertEqual(matched_positions("Löhne.pdf", ("lohn",)), {0, 1, 2, 3})
        self.assertEqual(matched_positions("Straße", ("xyz",)), set())

    def test_unknown_advanced_rule_key_is_rejected(self):
        dialog = self.settings_dialog(self.controller.settings)
        dialog.rule_json.setPlainText('{"extension_priority": []}')
        with patch("winlauncher.ui.settings.message") as warning:
            dialog.save()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), 0)
        dialog.deleteLater()

    def test_folder_priority_errors_name_the_line(self):
        dialog = self.settings_dialog(self.controller.settings)
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

        actions = ("Open", "Show in containing folder", "Add to favorites", "Copy path", "Copy")
        for action in actions:
            with self.subTest(action=action):
                self.window.set_results([first], 1)
                received.clear()
                self.app.clipboard().clear()

                def choose(action=action):
                    self.window.set_results([second], 1)
                    for menu in self.window.findChildren(QMenu):
                        if menu.isVisible():
                            # Text after a tab is the displayed shortcut.
                            labels = {
                                item.text().partition("\t")[0]: item for item in menu.actions()
                            }
                            menu.close()  # Even if the action is missing, never block the test.
                            labels[action].trigger()

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
                elif action == "Copy":
                    copied = self.app.clipboard().mimeData().urls()
                    self.assertEqual([Path(url.toLocalFile()) for url in copied], [first.item.path])
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

    def test_start_list_is_prefetched_while_hidden_and_shown_at_once(self):
        recent = SearchHit(CatalogItem(self.root / "recent.txt", ItemKind.FILE), 100, "Recent")
        (self.root / "recent.txt").touch()
        with patch.object(self.controller.search.service, "search", return_value=[recent]):
            self.window.query.setText("old query")
            self.controller.search.run()
            self.tasks.finish(TaskCategory.SEARCH)
            self.window.hide()  # Prefetches the start list in its own pool.
            self.assertIsNone(self.controller.search.start_list)
            self.tasks.finish(TaskCategory.PREFETCH)
            self.assertEqual(self.controller.search.start_list.results.hits, [recent])
            searches = self.tasks.count(TaskCategory.SEARCH)
            self.window.present()
            # Shown before any search finishes; no rows of "old query" in between.
            self.assertEqual(self.window.selected_hit(), recent)
            self.assertFalse(self.window.results.isHidden())
            # A fresh search confirms it right away, without the typing delay.
            self.assertEqual(self.tasks.count(TaskCategory.SEARCH), searches + 1)
            self.assertFalse(self.controller.search.debounce.isActive())
            self.tasks.finish(TaskCategory.SEARCH)
            self.assertEqual(self.window.selected_hit(), recent)

    def test_catalog_changes_warm_the_search_index_before_the_start_list(self):
        self.window.hide()
        before = len(self.tasks.jobs)
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        prefetches = [
            job.work for job in self.tasks.jobs[before:] if job.category == TaskCategory.PREFETCH
        ]
        # One prefetch worker runs them in order: the start list uses the fresh index.
        self.assertIs(prefetches[0].func, warm_search_index)
        self.assertEqual(prefetches[0].args, (self.controller.store,))
        self.assertEqual(prefetches[1].func, self.controller.search._work)

    def test_hiding_while_quitting_prefetches_nothing(self):
        # Quitting closes the worker pools before it hides the bar.
        prefetches = self.tasks.count(TaskCategory.PREFETCH)
        self.controller.search.close()
        self.window.hide()
        self.controller.search.refresh()
        self.assertEqual(self.tasks.count(TaskCategory.PREFETCH), prefetches)

    def test_background_results_are_delivered_after_submit_returns(self):
        from winlauncher.ui.tasks import BackgroundTasks

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

    def test_work_that_never_returns_neither_blocks_the_pool_nor_quitting(self):
        """A plugin hanging on the network must not keep WinLauncher's process alive.

        Otherwise the window would be gone while the next start waits for the
        instance lock. Checked in a process of its own, since only interpreter
        shutdown shows it.
        """
        from winlauncher.ui.tasks import WorkerPool

        pool = WorkerPool(2, "test")
        self.addCleanup(pool.close)
        hanging = threading.Event()
        pool.submit(hanging.wait)
        self.addCleanup(hanging.set)
        self.assertEqual(pool.submit(lambda: 21 * 2).result(timeout=5), 42)
        checkout = Path(winlauncher.__file__).resolve().parent.parent
        script = (
            f"import sys, threading; sys.path.insert(0, {str(checkout)!r});"
            "from winlauncher.ui.tasks import WorkerPool;"
            "pool = WorkerPool(1, 'test'); pool.submit(threading.Event().wait);"
            "threading.Event().wait(0.3); pool.close()"
        )
        finished = subprocess.run(  # noqa: S603 - this interpreter, a literal script.
            [sys.executable, "-I", "-c", script], timeout=60, capture_output=True
        )
        self.assertEqual((finished.returncode, finished.stderr), (0, b""))

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
        self.assertEqual(
            window.results.item(0).data(Qt.ItemDataRole.UserRole).hit.item.path, sum_file
        )
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
        dialog = self.settings_dialog(self.controller.settings)
        dialog.show()
        QApplication.processEvents()
        field = dialog.content_threads
        self.assertEqual(field.text(), str(self.controller.settings.content_search_threads))
        # Qt 6.12 collapsed the text area of styled spin boxes to one pixel.
        self.assertEqual(field.width(), NUMBER_FIELD_WIDTH)
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
                dialog = self.settings_dialog(c.settings, save=c.apply_settings)
                dialog.show()
                dialog.content_threads.setValue(threads)
                c.search.run()
                previous_cancel = c.search.running
                with patch.object(c, "_start_monitor") as monitor:
                    dialog.save()
                self.assertTrue(dialog.isVisible())
                self.assertEqual(c.settings_file.load().content_search_threads, threads)
                self.assertTrue(previous_cancel.is_set())
                self.assertEqual(self.tasks.count(TaskCategory.INDEX), indexed)
                self.assertEqual(self.window.scope, self.root)
                monitor.assert_not_called()
                with patch("winlauncher.search.service.select_native", return_value=None) as select:
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

    def test_explorer_bar_docks_bottom_right_and_explorer_follows_the_selection(self):
        self.window.hide()
        for name in ("toolbox-a", "toolbox-b"):
            (self.root / name).mkdir()
        select = patch.object(self.controller.actions, "select_in_explorer").start()
        intent = ExplorerTyping("toolbox", self.root, (0, 0, 1000, 800), (11, 22))
        self.controller._search_from_explorer(intent)
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        area = self.window.screen().availableGeometry()
        self.assertEqual(
            self.window.geometry().right(), min(999 - EXPLORER_RIGHT_GAP, area.right())
        )
        explorer = ExplorerTarget(11, 22, self.root)
        # Explorer jumps to the best direct entry, replacing its own type-ahead pick.
        self.assertTrue(self.controller.explorer_select.isActive())
        self.controller.explorer_select.timeout.emit()
        self.tasks.finish(TaskCategory.ACTION)
        select.assert_called_once_with(self.root / "toolbox-a", explorer)
        QTest.keyClick(self.window.query, Qt.Key.Key_Down)
        self.assertTrue(self.controller.explorer_select.isActive())
        self.controller.explorer_select.timeout.emit()
        self.tasks.finish(TaskCategory.ACTION)
        select.assert_called_with(self.root / "toolbox-b", explorer)
        self.window.hide()
        self.assertFalse(self.controller.explorer_select.isActive())

    def test_subfolder_matches_follow_a_separator_and_never_move_explorer_alone(self):
        self.window.hide()
        direct = self.root / "toolbox-a"
        nested = self.root / "archive" / "toolbox-old.txt"
        direct.mkdir()
        nested.parent.mkdir()
        nested.touch()
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        select = patch.object(self.controller.actions, "select_in_explorer").start()
        intent = ExplorerTyping("toolbox", self.root, (0, 0, 1000, 800), (11, 22))
        self.controller._search_from_explorer(intent)
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        rows = [self.window.results.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)]
        self.assertEqual([row.hit.item.path for row in rows], [direct, nested])
        self.assertEqual([row.direct for row in rows], [True, False])
        self.assertEqual([row.starts_subfolders for row in rows], [False, True])
        self.assertTrue(rows[1].accessible_text.startswith("In subfolders: "))
        self.assertFalse(self.window.grab().isNull())  # Paints the separator.
        # Without a direct match, new results leave Explorer's selection alone.
        self.controller.explorer_select.stop()
        self.window.query.setText("toolbox-old.txt")
        self.controller.search.run()
        self.tasks.finish(TaskCategory.SEARCH)
        self.assertFalse(self.controller.explorer_select.isActive())
        select.assert_not_called()

    def test_explorer_target_selects_the_entry_that_contains_a_result(self):
        explorer = ExplorerTarget(11, 22, self.root)
        self.assertEqual(explorer.entry_for(self.root / "a" / "b" / "c.txt"), self.root / "a")
        self.assertEqual(explorer.entry_for(self.root / "a.txt"), self.root / "a.txt")
        self.assertIsNone(explorer.entry_for(self.root))
        self.assertIsNone(explorer.entry_for(self.root.parent / "elsewhere.txt"))

    def test_explorer_bar_keeps_its_width_after_the_expanded_launcher(self):
        # The expanded launcher's long footer hint once held the Explorer bar at its width.
        hits = [
            SearchHit(CatalogItem(self.root / f"report {i}.txt", ItemKind.FILE), 100, "x")
            for i in range(20)
        ]
        self.window.set_catalog_count(412_345)
        self.window.toggle_expanded()
        self.window.set_results(hits, 3)
        self.app.processEvents()
        self.assertIn("Ctrl+C copy file", self.window.hint.text())
        self.window.present(
            text="rep", scope=self.root, near=(0, 0, 1000, 800), explorer_view=(1, 2)
        )
        self.window.set_results(hits, 3)
        self.app.processEvents()
        self.assertEqual(self.window.width(), EXPLORER_WIDTH + 2 * SHADOW_MARGIN)
        self.assertEqual(self.window.hint.text(), "↵ open · ↑↓ select in Explorer")

    def test_clearing_explorer_scope_restores_global_launcher(self):
        self.window.present(scope=self.root, near=(0, 0, 1000, 800), explorer_view=(11, 22))
        self.window.set_scope(None)
        self.assertFalse(self.window.explorer_mode)
        self.assertIsNone(self.window.explorer_view)
        self.assertFalse(self.controller.search.current_query().explorer)

    def test_quick_switch_bar_chooses_folders_and_files_in_the_dialog(self):
        self.window.hide()
        folder = self.root / "toolbox-cli"
        folder.mkdir()
        file = self.root / "toolbox.txt"
        file.touch()
        self.indexing.request_full()
        self.tasks.finish(TaskCategory.INDEX)
        choose = patch.object(self.controller.actions, "choose_in_dialog").start()
        select = patch.object(self.controller.actions, "select_in_explorer").start()
        dialog_folder = self.root / "toolbox-cli"  # The dialog's own folder is no limit.
        dialog = ExplorerTyping("toolbox", dialog_folder, (0, 0, 900, 600), (33, 44), DIALOG)
        for path, hint in ((folder, "↵ open in the dialog"), (file, "↵ enter as file")):
            self.controller._search_from_explorer(dialog)
            self.controller.search.run()
            self.tasks.finish(TaskCategory.SEARCH)
            # The same compact bar as in Explorer, searching everywhere; no arrow-key follow.
            self.assertTrue(self.window.explorer_mode)
            query = self.controller.search.current_query()
            self.assertIsNone(query.scope)
            self.assertFalse(query.explorer)
            self.assertTrue(self.window.clear_scope.isHidden())
            paths = [
                self.window.results.item(i).data(Qt.ItemDataRole.UserRole).hit.item.path
                for i in range(self.window.results.count())
            ]
            row = paths.index(path)
            self.assertFalse(self.controller.explorer_select.isActive())
            self.window.results.setCurrentRow(row)
            self.assertFalse(self.controller.explorer_select.isActive())
            self.assertTrue(self.window.hint.text().startswith(hint))
            self.window.open_row(row)
            self.assertFalse(self.window.isVisible())
            self.tasks.finish(TaskCategory.ACTION)
            choose.assert_called_with(path, "toolbox", 33)
        self.launch.assert_not_called()
        select.assert_not_called()
        # An Explorer search afterwards targets Explorer again.
        self.controller._search_from_explorer(
            ExplorerTyping("toolbox", self.root, (0, 0, 900, 600), (11, 22))
        )
        self.assertEqual(self.controller._explorer_target(), ExplorerTarget(11, 22, self.root))
        self.assertIsNone(self.controller._dialog_target())

    def test_dialog_on_desktop_or_this_pc_still_opens_the_bar(self):
        # Their address bar shows a name, not a path; the bar searches everywhere anyway.
        self.window.hide()
        dialog = ExplorerTyping("t", None, (0, 0, 900, 600), (33, 44), DIALOG)
        with patch.object(self.indexing, "request_folder") as refresh:
            self.controller._search_from_explorer(dialog)
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window.serves_file_dialog)
        self.assertIsNone(self.controller.search.current_query().scope)
        refresh.assert_not_called()

    def test_escape_returns_typed_text_to_the_dialog_but_not_to_explorer(self):
        hand_back = patch.object(self.controller.actions, "hand_back_to_dialog").start()
        escape = next(
            shortcut
            for shortcut in self.window.findChildren(QShortcut)
            if shortcut.key() == QKeySequence("Escape")
        )
        self.window.hide()
        dialog = ExplorerTyping("C", self.root, (0, 0, 900, 600), (33, 44), DIALOG)
        self.controller._search_from_explorer(dialog)
        self.window.query.setText(r"C:\notes")
        escape.activated.emit()
        self.assertFalse(self.window.isVisible())
        self.tasks.finish(TaskCategory.ACTION)
        hand_back.assert_called_once_with(r"C:\notes", 33)
        self.controller._search_from_explorer(
            ExplorerTyping("x", self.root, (0, 0, 900, 600), (11, 22))
        )
        escape.activated.emit()
        self.assertFalse(self.window.isVisible())
        hand_back.assert_called_once()

    def test_ctrl_g_jump_navigates_the_dialog_in_the_background(self):
        show = patch.object(self.controller.actions, "show_in_dialog").start()
        self.controller.keyboard_events.put(FolderJumpRequested(33, self.root))
        self.controller._drain_keyboard()
        self.tasks.finish(TaskCategory.ACTION)
        show.assert_called_once_with(self.root, 33)

    def test_quick_switch_setting_is_shown_and_restarts_the_keyboard(self):
        dialog = self.settings_dialog(self.controller.settings)
        self.assertTrue(dialog.quick_switch.isChecked())
        # The switch's row explains it; the switch itself draws no text.
        descriptions = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertTrue(any("Ctrl+G" in text for text in descriptions))
        self.assertEqual(
            dialog.quick_switch.accessibleName(), "Quick Switch in Open and Save dialogs"
        )
        dialog.quick_switch.setChecked(False)
        settings = dialog.read_settings()
        self.assertFalse(settings.quick_switch)
        dialog.deleteLater()
        with (
            patch.object(self.controller, "_stop_keyboard") as stop,
            patch.object(self.controller, "_start_keyboard") as start,
        ):
            self.controller.apply_settings(settings)
        stop.assert_called_once()
        start.assert_called_once()
        self.assertFalse(self.controller.settings_file.load().quick_switch)

    def test_settings_save_failure_keeps_dialog_and_edits(self):
        save = Mock(side_effect=OSError("Disk unavailable"))
        dialog = self.settings_dialog(self.controller.settings, save=save)
        dialog.show()
        dialog.limit.setValue(25)
        with patch("winlauncher.ui.settings.message") as warning:
            dialog.save()
        warning.assert_called_once()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.limit.value(), 25)
        save.side_effect = None
        dialog.save()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.result_settings.max_results, 25)
        dialog.deleteLater()

    def test_query_filters_are_colored_without_changing_the_text(self):
        def shows_filter_color() -> bool:
            image = self.window.query.grab().toImage()
            filter_background = QColor(colors().filter_background).rgb()
            return any(
                image.pixel(x, y) == filter_background
                for x in range(image.width())
                for y in range(image.height())
            )

        self.window.query.clear()
        QTest.keyClicks(self.window.query, "report")
        self.assertFalse(shows_filter_color())
        QTest.keyClicks(self.window.query, " size:>1kb")
        self.assertTrue(shows_filter_color())
        self.window.query.setSelection(0, 6)
        self.window.query_syntax.refresh()
        self.assertEqual(self.window.query.text(), "report size:>1kb")
        self.assertEqual(self.window.query.selectedText(), "report")
        self.assertTrue(self.window.query.isUndoAvailable())
        self.window.present(text="report", scope=self.root)
        self.assertFalse(shows_filter_color())
        self.window.present(text="ext:txt", scope=self.root)
        self.assertTrue(shows_filter_color())

    def test_invalid_filter_is_marked_only_after_the_cursor_leaves_it(self):
        def shows_invalid_color() -> bool:
            image = self.window.query.grab().toImage()
            invalid = QColor(colors().invalid).rgb()
            return any(
                image.pixel(x, y) == invalid
                for x in range(image.width())
                for y in range(image.height())
            )

        self.window.query.clear()
        QTest.keyClicks(self.window.query, "report after:2026-1")
        self.assertFalse(shows_invalid_color())
        self.window.query.setCursorPosition(0)
        self.assertTrue(shows_invalid_color())
        self.window.query.end(False)
        QTest.keyClicks(self.window.query, "2")
        self.window.query.setCursorPosition(0)
        self.assertFalse(shows_invalid_color())

    def test_empty_launcher_hides_after_focus_left_winlauncher(self):
        def lose_focus() -> None:
            QApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowDeactivate))

        with patch.object(QApplication, "activeWindow", return_value=None):
            lose_focus()
            self.assertTrue(self.window.auto_hide.isActive())
            self.assertEqual(self.window.auto_hide.interval(), 3000)
            QApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
            self.assertFalse(self.window.auto_hide.isActive())
            self.window.query.setText("report")
            lose_focus()
            self.window.auto_hide.timeout.emit()
            self.assertTrue(self.window.isVisible())
            self.window.query.clear()
            lose_focus()
            self.window.auto_hide.timeout.emit()
            self.assertFalse(self.window.isVisible())
        self.window.show()
        with patch.object(QApplication, "activeWindow", return_value=self.window):
            lose_focus()
            self.window.auto_hide.timeout.emit()
            self.assertTrue(self.window.isVisible())
        settings = self.controller.settings
        self.controller.apply_settings(replace(settings, auto_hide=False, auto_hide_seconds=7))
        self.assertEqual(self.window.auto_hide_ms, 7000)
        lose_focus()
        self.assertFalse(self.window.auto_hide.isActive())
        # The Explorer bar closes right away, even with the option off and text typed.
        self.window.present(text="report", scope=self.root, near=(0, 0, 800, 600))
        self.assertTrue(self.window.explorer_mode)
        with patch.object(QApplication, "activeWindow", return_value=None):
            lose_focus()
            self.assertTrue(self.window.auto_hide.isActive())
            self.assertEqual(self.window.auto_hide.interval(), 0)
            self.app.processEvents()
        self.assertFalse(self.window.isVisible())

    def test_auto_hide_settings_are_read_from_the_dialog(self):
        dialog = self.settings_dialog(self.controller.settings)
        self.assertTrue(dialog.auto_hide_delay.isEnabled())
        dialog.auto_hide.setChecked(False)
        dialog.auto_hide_delay.setValue(10)
        self.assertFalse(dialog.auto_hide_delay.isEnabled())
        settings = dialog.read_settings()
        self.assertEqual((settings.auto_hide, settings.auto_hide_seconds), (False, 10))
        with self.assertRaisesRegex(ValueError, "Auto-close"):
            Settings(roots=(), auto_hide_seconds=0)
        dialog.deleteLater()

    def test_settings_can_be_saved_repeatedly_without_closing(self):
        dialog = self.settings_dialog(self.controller.settings, save=self.controller.apply_settings)
        dialog.show()
        save_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
        for limit in (25, 30):
            dialog.limit.setValue(limit)
            save_button.click()
            self.assertTrue(dialog.isVisible())
            self.assertEqual(self.controller.settings_file.load().max_results, limit)
            self.assertEqual(dialog.original.max_results, limit)
            self.assertEqual(dialog.saved_message.text(), "Settings saved.")
            self.assertTrue(dialog.saved_message_timer.isActive())
        dialog.limit.setValue(40)
        dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.controller.settings_file.load().max_results, 30)
        self.assertEqual(dialog.result_settings.max_results, 30)
        dialog.deleteLater()

    def test_hidden_completions_do_not_submit_searches(self):
        self.window.hide()
        self.indexing.request_full()
        before = self.tasks.count(TaskCategory.SEARCH)
        self.tasks.finish(TaskCategory.INDEX)
        with patch("winlauncher.ui.controller.messages.ask", return_value=True):
            self.controller.clear_history()
        self.tasks.finish(TaskCategory.ACTION)
        self.assertEqual(self.tasks.count(TaskCategory.SEARCH), before)
        self.assertIsNone(self.controller.search.displayed_query)

    def test_clearing_history_asks_first(self):
        before = self.tasks.count(TaskCategory.ACTION)
        with patch("winlauncher.ui.controller.messages.ask", return_value=False) as asked:
            self.controller.clear_history()
        asked.assert_called_once()
        self.assertEqual(self.tasks.count(TaskCategory.ACTION), before)
        with patch("winlauncher.ui.controller.messages.ask", return_value=True):
            self.controller.clear_history()
        self.assertEqual(self.tasks.count(TaskCategory.ACTION), before + 1)
        self.tasks.finish(TaskCategory.ACTION)

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
            patch("winlauncher.ui.controller.QProcess.startDetached", return_value=True) as start,
            patch("winlauncher.ui.controller.QApplication.instance") as application,
            patch(
                "winlauncher.ui.controller.application_command",
                side_effect=lambda arguments: ["host.exe", *arguments],
            ),
        ):
            c.restart()
        _program, arguments, _cwd = start.call_args.args
        self.assertEqual(_program, "host.exe")
        self.assertEqual(arguments[0], "--replace")
        self.assertIn(str(c.data_dir), arguments)
        self.assertIn("--no-hooks", arguments)
        self.assertIn("--show", arguments)  # The launcher was open, so it reopens.
        application.return_value.quit.assert_called_once()

    def test_settings_is_a_window_of_its_own_that_keeps_outside_exclusions(self):
        c = self.controller
        c.startup = Mock()  # Never read the real Startup folder.
        c.startup.is_enabled.return_value = False
        self.assertTrue(self.window.isVisible())
        c.edit_settings()
        self.assertFalse(self.window.isVisible())  # Settings opened last, so it is in front.
        dialog = c.settings_dialog
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.isModal())
        self.assertIsNone(dialog.parent())  # The search bar can come in front of it.
        c.edit_settings()
        self.assertIs(c.settings_dialog, dialog)  # Brought forward, not opened twice.
        folder = self.root / "noise"
        folder.mkdir()
        with patch("winlauncher.ui.controller.ExcludeFolderDialog") as picker:
            picker.return_value.exec.return_value = True
            picker.return_value.chosen_folder.return_value = folder
            picker.return_value.pattern = None
            c.exclude_folder(folder)
            picker.return_value.pattern = "**\\noise\\deep"
            c.exclude_folder(folder)
        dialog.save()  # Saving the window afterwards keeps both rules.
        self.assertIn(str(folder), c.settings.excluded_paths)
        self.assertIn("**\\noise\\deep", c.settings.excluded_patterns)
        dialog.reject()
        self.assertIsNone(c.settings_dialog)

    def test_exclude_picker_adds_the_chosen_parent(self):
        c = self.controller
        folder = self.root / "project" / "node_stuff" / "deep"
        folder.mkdir(parents=True)
        with patch("winlauncher.ui.controller.ExcludeFolderDialog") as dialog:
            dialog.return_value.exec.return_value = True
            dialog.return_value.chosen_folder.return_value = folder.parent
            dialog.return_value.pattern = None
            c.exclude_folder(folder)
        choices = dialog.call_args.args[0]
        self.assertEqual([choice.folder for choice in choices][-1], self.root)
        self.assertIn(str(folder.parent), c.settings.excluded_paths)
        # A custom rule becomes a pattern, not an excluded folder.
        excluded_paths = c.settings.excluded_paths
        with patch("winlauncher.ui.controller.ExcludeFolderDialog") as dialog:
            dialog.return_value.exec.return_value = True
            dialog.return_value.chosen_folder.return_value = folder
            dialog.return_value.pattern = "**\\node_stuff\\deep"
            c.exclude_folder(folder)
        self.assertEqual(c.settings.excluded_paths, excluded_paths)
        self.assertEqual(c.settings.excluded_patterns[-1], "**\\node_stuff\\deep")
        self.assertFalse(c.settings.exclusions().allows(folder / "file.txt"))
        # A single entry, such as one Windows setting, is excluded by itself.
        entry = self.root / "project" / "Bluetooth.lnk"
        with patch("winlauncher.ui.controller.ExcludeFolderDialog") as dialog:
            dialog.return_value.exec.return_value = True
            dialog.return_value.chosen_folder.return_value = entry
            dialog.return_value.pattern = None
            c.exclude_folder(entry)
        self.assertEqual(dialog.call_args.args[0][0].folder, entry)
        exclusions = c.settings.exclusions()
        self.assertFalse(exclusions.allows(entry, application=True))
        self.assertTrue(exclusions.allows(entry.with_name("Bluetooth.lnk2"), application=True))

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
        dialog = self.settings_dialog(
            c.settings, startup_enabled=True, set_startup=c._set_autostart
        )
        startup.set_enabled.side_effect = OSError("Access denied")
        with patch("winlauncher.ui.settings.message") as warning:
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
        with patch("winlauncher.ui.controller.messages.message") as warning:
            c.tray.autostart.trigger()
        warning.assert_called_once()
        self.assertFalse(c.tray.autostart.isChecked())


if __name__ == "__main__":
    unittest.main()
