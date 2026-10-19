"""Compose the launcher: windows, tray, background work and platform adapters.

Scheduling rules live in `IndexScheduler` and `SearchSession`; catalog and
search rules live in their domains. This module connects intents to them.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import QObject, QProcess, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from pathscout.actions import ItemActions
from pathscout.catalog.indexer import CatalogIndexer
from pathscout.catalog.model import ScanReport, is_within
from pathscout.catalog.sources import catalog_sources
from pathscout.catalog.store import CatalogStore
from pathscout.platform.changes import FileChange, FolderChange, WatchFailed, WatchRestored
from pathscout.platform.gesture import (
    ExplorerTyping,
    InputUnavailable,
    KeyboardIntent,
    ShowRequested,
)
from pathscout.platform.shell import application_roots
from pathscout.platform.startup import PACKAGE_PARENT, StartupShortcut
from pathscout.search.ranking import SearchHit
from pathscout.search.service import SearchService
from pathscout.settings import Settings, SettingsFile
from pathscout.ui.about import AboutDialog
from pathscout.ui.exclude_dialog import ExcludeFolderDialog, FolderChoice, exclusion_candidates
from pathscout.ui.indexing import IndexJob, IndexJobKind, IndexScheduler
from pathscout.ui.search_session import SearchSession
from pathscout.ui.settings import SettingsDialog
from pathscout.ui.status import ProblemTopic, StatusBoard
from pathscout.ui.tasks import BackgroundTasks, TaskCategory
from pathscout.ui.tray import TrayMenu
from pathscout.ui.updater import GitUpdater
from pathscout.ui.window import LauncherWindow

logger = logging.getLogger(__name__)
KEYBOARD_POLL_MS = 20
WATCH_POLL_MS = 500
HIDDEN_SCOPE_CLEANUP_MS = 500

_Event = TypeVar("_Event")


def _watch_failure(event: WatchFailed) -> str:
    return (
        f"Folder watch unavailable for {event.root} ({event.message}); "
        "periodic refresh remains active."
    )


def _drain(events: queue.SimpleQueue[_Event]) -> Iterator[_Event]:
    while not events.empty():
        yield events.get()


class LauncherController(QObject):
    def __init__(self, data_dir: Path, *, hooks: bool = True):
        super().__init__()
        self.data_dir = data_dir
        self.hooks_requested = hooks
        self.settings_file = SettingsFile(data_dir / "settings.json")
        self.settings = self.settings_file.load()
        if not self.settings_file.path.exists():
            self.settings_file.save(self.settings)
        self.store = CatalogStore(data_dir / "catalog.sqlite3")
        self.actions = ItemActions(self.store)
        self.startup = StartupShortcut(data_dir)
        self.hooks_enabled = hooks and os.name == "nt"
        self.keyboard = None
        self.monitor = None
        self.keyboard_events: queue.SimpleQueue[KeyboardIntent] = queue.SimpleQueue()
        self.watch_events: queue.SimpleQueue = queue.SimpleQueue()
        self.favorite_pending = False
        self._closed = False

        self.window = LauncherWindow()
        self.tasks = BackgroundTasks(self)
        self.tray = TrayMenu(self, autostart_available=os.name == "nt")
        self.status = StatusBoard(self.window.set_activity, self.tray.icon)
        self.search = SearchSession(
            self.window, SearchService(self.store), self.tasks, self.effective_settings, self
        )
        self.indexing = IndexScheduler(
            self.tasks,
            CatalogIndexer(self.store),
            self.store,
            self.effective_settings,
            self._explorer_session_open,
            self,
        )
        self.updater = GitUpdater(self)
        self.updater.availability_changed.connect(self._update_available)
        self.updater.pull_finished.connect(self._update_finished)
        self._update_catalog_count()
        self._connect_window()
        self._connect_tray()
        self._connect_indexing()
        self.keyboard_timer = self._poll(self._drain_keyboard, KEYBOARD_POLL_MS)
        self.watch_timer = self._poll(self._drain_watch_events, WATCH_POLL_MS)
        self._ui_tick = time.monotonic()
        self.ui_timer = self._poll(self._check_ui_delay, 100)
        self.tray.show()
        self._start_keyboard()
        self._start_monitor()
        self.updater.start_watching()
        QTimer.singleShot(0, self.indexing.request_full)

    # Wiring -------------------------------------------------------------------

    def _connect_window(self) -> None:
        window = self.window
        window.query_changed.connect(self.search.text_changed)
        window.open_requested.connect(self.search.request_open)
        window.reveal_requested.connect(self.reveal_item)
        window.favorite_requested.connect(self.favorite_item)
        window.exclude_requested.connect(self.exclude_folder)
        window.settings_requested.connect(self.edit_settings)
        window.rescan_requested.connect(self.rescan)
        window.hidden.connect(self._after_hide)
        window.restart_requested.connect(self.restart)
        window.about_requested.connect(self.show_about)
        self.search.open_requested.connect(self.open_item)

    def _connect_tray(self) -> None:
        tray = self.tray
        tray.search_requested.connect(self.window.present)
        tray.refresh_requested.connect(self.rescan)
        tray.settings_requested.connect(self.edit_settings)
        tray.status_requested.connect(self.show_index_status)
        tray.clear_history_requested.connect(self.clear_history)
        tray.quit_requested.connect(QApplication.instance().quit)
        tray.autostart_toggled.connect(self._toggle_autostart_from_tray)
        tray.opening.connect(self._sync_autostart)
        tray.restart_requested.connect(self.restart)
        tray.update_requested.connect(self.check_for_updates)
        tray.about_requested.connect(self.show_about)

    def _connect_indexing(self) -> None:
        self.indexing.started.connect(self._index_started)
        self.indexing.progress.connect(self._index_progress)
        self.indexing.completed.connect(self._index_completed)
        self.indexing.failed.connect(self._index_failed)

    def _poll(self, callback, interval_ms: int) -> QTimer:
        timer = QTimer(self)
        timer.timeout.connect(callback)
        timer.start(interval_ms)
        return timer

    def effective_settings(self) -> Settings:
        """The user's settings, with PathScout's own data directory always excluded."""
        own_data = str(self.store.path.parent)
        return replace(self.settings, excluded_paths=(*self.settings.excluded_paths, own_data))

    def _explorer_session_open(self) -> bool:
        return self.window.isVisible() and self.window.scope is not None

    def _configured_roots(self) -> list[Path]:
        roots = application_roots() if self.settings.index_apps else ()
        return [source.root for source in catalog_sources(self.settings, roots)]

    # Keyboard -----------------------------------------------------------------

    def _start_keyboard(self) -> None:
        self.status.clear_topic(ProblemTopic.KEYBOARD)
        if not self.hooks_enabled:
            return
        from pathscout.platform.keyboard import KeyboardListener

        self.keyboard = KeyboardListener(
            self.keyboard_events,
            interval_ms=self.settings.double_ctrl_ms,
            explorer_typing=self.settings.explorer_typing,
        )
        self.keyboard.start()

    def _stop_keyboard(self) -> None:
        if self.keyboard is None:
            return
        self.keyboard.stop()
        self.keyboard.join(timeout=1)
        self.keyboard = None

    def _drain_keyboard(self) -> None:
        for intent in _drain(self.keyboard_events):
            if isinstance(intent, ShowRequested):
                logger.info(
                    "Show requested: source=%s dispatch_ms=%.1f visible=%s active=%s",
                    intent.source,
                    (time.monotonic() - intent.requested_at) * 1000,
                    self.window.isVisible(),
                    self.window.isActiveWindow(),
                )
                self._toggle_launcher()
            elif isinstance(intent, ExplorerTyping):
                self._search_from_explorer(intent)
            elif isinstance(intent, InputUnavailable):
                self.status.show_problem(ProblemTopic.KEYBOARD, intent.message, intent.message)

    def _check_ui_delay(self) -> None:
        now = time.monotonic()
        elapsed_ms = (now - self._ui_tick) * 1000
        self._ui_tick = now
        if elapsed_ms > 250:
            logger.warning(
                "UI event loop delayed: %.0f ms search_running=%s indexing=%s",
                elapsed_ms,
                self.search.running is not None,
                self.indexing.busy,
            )

    def _toggle_launcher(self) -> None:
        if self.window.isVisible() and self.window.isActiveWindow():
            self.window.hide()
        else:
            self.window.present()

    def _search_from_explorer(self, typing: ExplorerTyping) -> None:
        if (
            self.window.isVisible()
            and self.window.explorer_mode
            and self.window.scope == typing.folder
            and self.window.explorer_view == typing.view
        ):
            self.window.query.insert(typing.text)
            return
        self.window.present(
            text=typing.text, scope=typing.folder, near=typing.window, explorer_view=typing.view
        )
        self.indexing.request_folder(typing.folder)

    # Folder watching ----------------------------------------------------------

    def _start_monitor(self) -> None:
        if os.name != "nt":
            return
        from pathscout.platform.watcher import ChangeMonitor

        settings = self.effective_settings()
        exclusions = settings.exclusions()
        roots = tuple(
            root for root in self._configured_roots() if exclusions.allows(root, directory=True)
        )
        self.monitor = ChangeMonitor(roots, self.watch_events, exclusions=exclusions)
        self.monitor.start()

    def _stop_monitor(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None

    def _drain_watch_events(self) -> None:
        for event in _drain(self.watch_events):
            if isinstance(event, FileChange) and self._is_catalog_path(event.path):
                self.indexing.mark_file_dirty(event.path)
            elif isinstance(event, FolderChange) and self._is_catalog_path(event.folder):
                self.indexing.mark_folder_dirty(event.folder)
            elif isinstance(event, WatchFailed):
                self.status.show_problem(
                    ProblemTopic.FOLDER_WATCH, _watch_failure(event), event.root
                )
            elif isinstance(event, WatchRestored):
                self.status.clear_problem(ProblemTopic.FOLDER_WATCH, event.root)
                # Changes during the outage were never reported.
                self.indexing.mark_folder_dirty(event.root)
        self.indexing.run_pending()

    def _is_catalog_path(self, path: Path) -> bool:
        """PathScout's own catalog and log writes must not trigger repairs."""
        return not is_within(path, self.store.path.parent)

    # Indexing results -----------------------------------------------------------

    def _index_started(self, job: IndexJob) -> None:
        full = job.kind == IndexJobKind.FULL
        self.status.set_activity("Refreshing local catalog…" if full else "Refreshing folder…")

    def _index_progress(self, count: int) -> None:
        self.status.set_activity(f"Refreshing catalog… {count:,} entries scanned")

    def _index_completed(self, report: ScanReport) -> None:
        self._update_catalog_count()
        errors = f"Last refresh: {len(report.errors)} scan errors" if report.errors else ""
        self.status.set_activity(errors)
        if not report.cancelled:
            self.search.refresh()

    def _index_failed(self, message: str) -> None:
        self.status.set_activity(f"Index failed: {message}")

    def _update_catalog_count(self) -> None:
        self.status.set_catalog_count(self.indexing.catalog_count)
        self.window.set_catalog_count(self.indexing.catalog_count)

    def rescan(self) -> None:
        self.window.set_scope(None)
        self.indexing.request_full()

    def _after_hide(self) -> None:
        self.search.window_hidden()
        scope = self.window.scope
        if self._closed or scope is None:
            return
        if any(is_within(scope, root) for root in self._configured_roots()):
            self.indexing.run_pending()
            return
        # An Explorer scope outside every source must not stay in the catalog.
        QTimer.singleShot(HIDDEN_SCOPE_CLEANUP_MS, self._remove_hidden_scope)

    def _remove_hidden_scope(self) -> None:
        if not self._closed and not self.window.isVisible():
            self.indexing.request_full()

    def show_index_status(self) -> None:
        report = self.indexing.last_report
        if report is None:
            QMessageBox.information(
                self.window, "PathScout index status", "The initial scan has not completed yet."
            )
            return
        text = (
            f"{self.indexing.catalog_count:,} catalog entries\n"
            f"Last scan: {report.entries:,} entries in {report.elapsed:.2f}s\n"
            f"Excluded/skipped: {report.skipped:,}\nErrors: {len(report.errors)}"
        )
        if problems := self.status.problem_text():
            text += f"\n\nProblems:\n{problems}"
        dialog = QMessageBox(
            QMessageBox.Icon.Information, "PathScout index status", text, parent=self.window
        )
        if report.errors:
            dialog.setDetailedText("\n".join(report.errors))
        dialog.exec()
        dialog.deleteLater()

    # Item actions -------------------------------------------------------------

    def open_item(self, hit: SearchHit, history_key: str) -> None:
        action = partial(self.actions.open, hit.item.path, history_key)
        if self.window.explorer_view is not None and self.window.scope is not None:
            action = partial(
                self.actions.open,
                hit.item.path,
                history_key,
                explorer=(*self.window.explorer_view, self.window.scope),
            )
        self.window.hide()
        self._run_action(action)

    def reveal_item(self, hit: SearchHit) -> None:
        self.window.hide()
        self._run_action(partial(self.actions.reveal, hit.item.path))

    def favorite_item(self, hit: SearchHit) -> None:
        """Toggle once; repeated requests wait until the stored state is visible again."""
        if self.favorite_pending:
            return
        self.favorite_pending = True
        self.tasks.submit(
            TaskCategory.ACTION,
            partial(self.actions.favorite, hit.item.path, not hit.item.pinned),
            self._favorite_done,
            self._favorite_failed,
        )

    def _favorite_done(self, _result: None) -> None:
        self.favorite_pending = False
        self.search.refresh()

    def _favorite_failed(self, message: str) -> None:
        self.favorite_pending = False
        self._action_failed(message)

    def clear_history(self) -> None:
        self._run_action(self.store.clear_history, on_success=self.search.refresh)

    def _run_action(self, work, *, on_success=None) -> None:
        def succeeded(_result) -> None:
            if on_success is not None:
                on_success()

        self.tasks.submit(TaskCategory.ACTION, work, succeeded, self._action_failed)

    def _action_failed(self, message: str) -> None:
        QMessageBox.warning(self.window, "PathScout", message)

    def exclude_folder(self, folder: Path) -> None:
        """Let the user exclude the result's folder or one of its parents."""
        candidates = exclusion_candidates(folder, self._configured_roots())
        choices = [FolderChoice(path, self.store.count_within(path)) for path in candidates]
        dialog = ExcludeFolderDialog(choices, self.window)
        chosen = dialog.chosen_folder() if dialog.exec() else None
        dialog.deleteLater()
        if chosen is None:
            return
        excluded = tuple(dict.fromkeys((*self.settings.excluded_paths, str(chosen))))
        try:
            self.apply_settings(replace(self.settings, excluded_paths=excluded))
        except OSError as exc:
            QMessageBox.warning(self.window, "Cannot save settings", str(exc))

    # Settings -----------------------------------------------------------------

    def edit_settings(self) -> None:
        self._sync_autostart()
        dialog = SettingsDialog(
            self.settings,
            self.window,
            save=self.apply_settings,
            startup_enabled=self.tray.autostart.isChecked(),
            set_startup=self._set_autostart if os.name == "nt" else None,
            startup_state=self.tray.autostart.isChecked,
        )
        dialog.exec()
        dialog.deleteLater()

    def apply_settings(self, settings: Settings) -> None:
        """Save first (raises OSError), then restart only what the change affects.

        Once saved, the settings stay accepted; a failing restart is reported as a
        problem instead of leaving the dialog open over a half-applied change.
        """
        self.settings_file.save(settings)
        previous, self.settings = self.settings, settings
        logger.info(
            "Settings saved: search_backend=%s indexing_changed=%s input_changed=%s",
            settings.search_backend,
            previous.indexing_key != settings.indexing_key,
            previous.input_key != settings.input_key,
        )
        try:
            self._apply_changes(previous, settings)
        except Exception as exc:
            logger.exception("Saved settings could not be fully applied")
            QMessageBox.warning(
                self.window,
                "Settings saved",
                f"Some changes take effect only after restarting PathScout: {exc}",
            )
        self.search.refresh()

    def _apply_changes(self, previous: Settings, settings: Settings) -> None:
        if previous.input_key != settings.input_key:
            self._stop_keyboard()
            self._start_keyboard()
        if previous.indexing_key != settings.indexing_key:
            self._restart_indexing()
        elif previous.rescan_seconds != settings.rescan_seconds:
            self.indexing.restart_periodic_timer()

    def _restart_indexing(self) -> None:
        self._stop_monitor()
        self.watch_events = queue.SimpleQueue()
        self.status.clear_topic(ProblemTopic.FOLDER_WATCH)
        self._start_monitor()
        self.window.set_scope(None)
        self.indexing.restart()

    # Autostart ----------------------------------------------------------------

    def _sync_autostart(self) -> None:
        """Show the actual Startup shortcut state; it is the only source of truth."""
        try:
            self.tray.autostart.setChecked(self.startup.is_enabled())
        except OSError as exc:
            self.status.show_problem(ProblemTopic.AUTOSTART, str(exc))
            return
        self.status.clear_problem(ProblemTopic.AUTOSTART)

    def _set_autostart(self, enabled: bool) -> None:
        try:
            self.startup.set_enabled(enabled)
        finally:
            self._sync_autostart()

    def _toggle_autostart_from_tray(self, enabled: bool) -> None:
        try:
            self._set_autostart(enabled)
        except OSError as exc:
            QMessageBox.warning(self.window, "Cannot change autostart", str(exc))

    # About, updates and restart ----------------------------------------------

    def show_about(self) -> None:
        dialog = AboutDialog(self.data_dir, self.window)
        dialog.exec()
        dialog.deleteLater()

    def check_for_updates(self) -> None:
        if not self.updater.supported:
            QMessageBox.information(
                self.window,
                "Updates",
                "PathScout updates itself when it runs from a git checkout and git is "
                "installed. Update installed packages with pip.",
            )
            return
        if self.updater.pulling:
            return
        answer = QMessageBox.question(
            self.window,
            "Check for updates",
            f"Download and apply the latest version into\n{self.updater.root}?\n\n"
            "Local changes are never overwritten; git refuses instead.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.status.set_activity("Updating PathScout…")
            self.updater.pull()

    def _update_available(self, available: bool) -> None:
        self.tray.set_update_available(available)
        if available:
            self.tray.icon.showMessage(
                "PathScout", "An update is available. Install it from the tray menu."
            )

    def _update_finished(self, succeeded: bool, changed: bool, output: str) -> None:
        self.status.set_activity("")
        details = output or "No output from git."
        if not succeeded:
            QMessageBox.warning(self.window, "Update failed", details)
            return
        if not changed:
            QMessageBox.information(self.window, "Updates", "PathScout is up to date.")
            return
        answer = QMessageBox.question(
            self.window, "Update installed", f"{details}\n\nRestart PathScout now?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.restart()

    def restart(self) -> None:
        """Start a replacement that waits for this process's lock, then quit."""
        arguments = ["-m", "pathscout", "--replace", "--data-dir", str(self.data_dir)]
        if self.window.isVisible():
            arguments.append("--show")
        if not self.hooks_requested:
            arguments.append("--no-hooks")
        self._stop_keyboard()  # Never run two global keyboard hooks at once.
        if not QProcess.startDetached(sys.executable, arguments, str(PACKAGE_PARENT)):
            self._start_keyboard()
            QMessageBox.warning(self.window, "PathScout", "PathScout could not be restarted.")
            return
        QApplication.instance().quit()

    # Shutdown -----------------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        self.keyboard_timer.stop()
        self.watch_timer.stop()
        self.ui_timer.stop()
        self.search.close()
        self.indexing.close()
        self._stop_keyboard()
        self._stop_monitor()
        self.updater.stop()
        self.tasks.close()
        self.window.delegate.icons.close()
        self.tray.hide()
        self.window.hide()
