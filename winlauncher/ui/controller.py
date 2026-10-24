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

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from winlauncher import __version__
from winlauncher.actions import ExplorerTarget, ItemActions
from winlauncher.catalog.indexer import CatalogIndexer
from winlauncher.catalog.model import ScanReport, is_within
from winlauncher.catalog.sources import catalog_sources
from winlauncher.catalog.store import CatalogStore
from winlauncher.commands import MenuCommand
from winlauncher.install import mark_reinstall_pending
from winlauncher.platform.changes import FileChange, FolderChange, WatchFailed, WatchRestored
from winlauncher.platform.gesture import (
    ExplorerTyping,
    FolderJumpRequested,
    InputUnavailable,
    KeyboardIntent,
    ShowRequested,
    ViewKind,
)
from winlauncher.platform.git import release_changes
from winlauncher.platform.shell import folder_shortcuts, indexed_application_roots
from winlauncher.platform.startup import PACKAGE_PARENT, StartupShortcut
from winlauncher.platform.store_apps import store_app_folder
from winlauncher.platform.windows_tools import windows_tools_folder
from winlauncher.plugins.host import PluginHost
from winlauncher.plugins.registry import discover_plugins
from winlauncher.plugins.search import PluginHit
from winlauncher.releases import Release, install_command
from winlauncher.search.native_engine import native_availability, warm_search_index
from winlauncher.search.ranking import SearchHit
from winlauncher.search.service import SearchService
from winlauncher.settings import Appearance, Settings, SettingsFile
from winlauncher.ui import messages
from winlauncher.ui.about import AboutDialog
from winlauncher.ui.exclude_dialog import ExcludeFolderDialog, FolderChoice, exclusion_candidates
from winlauncher.ui.indexing import IndexJob, IndexJobKind, IndexScheduler
from winlauncher.ui.plugins import PluginIcons, PluginScheduler
from winlauncher.ui.result_rows import LauncherHit
from winlauncher.ui.search_session import SearchSession
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.status import ProblemTopic, StatusBoard
from winlauncher.ui.tasks import BackgroundTasks, TaskCategory
from winlauncher.ui.theme import colors, theme
from winlauncher.ui.tray import TrayMenu
from winlauncher.ui.update_dialog import UpdateCommandDialog
from winlauncher.ui.updater import ReleaseUpdater
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.widgets import InfoDialog, line_icon
from winlauncher.ui.window import LauncherWindow
from winlauncher.websites import WebsiteTarget

logger = logging.getLogger(__name__)
UI_DELAY_POLL_MS = 100
UI_DELAY_WARNING_MS = 250
KEYBOARD_POLL_MS = 20
WATCH_POLL_MS = 500
HIDDEN_SCOPE_CLEANUP_MS = 500
DETAILS_HEIGHT = 120  # Scan errors and problems in the index status dialog.
# Explorer follows the result where arrow-key repeats stop, not every row passed.
EXPLORER_SELECT_DELAY_MS = 120

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
        theme().use(Appearance(self.settings.appearance))  # Before any window exists.
        self.store = CatalogStore(data_dir / "catalog.sqlite3")
        self.actions = ItemActions(self.store)
        self.startup = StartupShortcut(data_dir)
        self.hooks_enabled = hooks and os.name == "nt"
        self.keyboard = None
        self.monitor = None
        self.keyboard_events: queue.SimpleQueue[KeyboardIntent] = queue.SimpleQueue()
        self.watch_events: queue.SimpleQueue = queue.SimpleQueue()
        self.favorite_pending = False
        self.settings_dialog: SettingsDialog | None = None  # While Settings is open.
        self._closed = False

        # Shared by the result list and the Websites settings.
        self.website_icons = WebsiteIcons(self)
        found = discover_plugins(data_dir / "plugins")
        self.plugins = PluginHost(found.plugins, data_dir, found.problems)
        self.window = LauncherWindow(self.website_icons, PluginIcons(self.plugins, self))
        self.tasks = BackgroundTasks(self)
        self.tray = TrayMenu(self, autostart_available=os.name == "nt")
        self.status = StatusBoard(self.window.set_activity, self.tray.icon)
        self.search = SearchSession(
            self.window,
            SearchService(self.store, folder_shortcuts),
            self.tasks,
            self.effective_settings,
            self,
            self.plugins,
        )
        self.plugin_refresh = PluginScheduler(self.plugins, self.tasks, lambda: self.settings, self)
        self.plugin_refresh.items_changed.connect(self.search.refresh)
        self.indexing = IndexScheduler(
            self.tasks,
            CatalogIndexer(self.store),
            self.store,
            self.effective_settings,
            self._explorer_session_open,
            self,
        )
        self.updater = ReleaseUpdater(self)
        self.update_check_requested = False  # The user asked; report the answer.
        self.update_release: Release | None = None  # The release the running update installs.
        self.updater.checked.connect(self._update_checked)
        self.updater.availability_changed.connect(self._update_available)
        self.updater.update_finished.connect(self._update_finished)
        self._update_catalog_count()
        self._apply_window_settings(self.settings)
        self._connect_window()
        self._connect_tray()
        self._connect_indexing()
        self.explorer_hit: SearchHit | None = None  # Waiting for `explorer_select`.
        self.explorer_select = QTimer(self)
        self.explorer_select.setSingleShot(True)
        self.explorer_select.setInterval(EXPLORER_SELECT_DELAY_MS)
        self.explorer_select.timeout.connect(self._select_in_explorer)
        self.keyboard_timer = self._poll(self._drain_keyboard, KEYBOARD_POLL_MS)
        self.watch_timer = self._poll(self._drain_watch_events, WATCH_POLL_MS)
        self._ui_tick = time.monotonic()
        # Measures UI responsiveness while the launcher is visible; idle in the tray.
        self.ui_timer = self._poll(self._check_ui_delay, UI_DELAY_POLL_MS)
        self.ui_timer.stop()
        self.window.shown.connect(self._start_ui_delay_check)
        self._report_search_engine()
        self.tray.show()
        self._start_keyboard()
        self._start_monitor()
        self.updater.start_watching()
        QTimer.singleShot(0, self.window.warm_up_text)  # Once the event loop is idle.
        self._warm_search_index()
        QTimer.singleShot(0, self.indexing.request_full)
        self.plugin_refresh.start()

    # Wiring -------------------------------------------------------------------

    def _connect_window(self) -> None:
        window = self.window
        window.query_changed.connect(self.search.text_changed)
        window.open_requested.connect(self.search.request_open)
        window.reveal_requested.connect(self.reveal_item)
        window.favorite_requested.connect(self.favorite_item)
        window.explorer_selection.connect(self.follow_in_explorer)
        window.dialog_text_returned.connect(self._return_text_to_dialog)
        window.shell_menu_requested.connect(self.show_shell_menu)
        window.exclude_requested.connect(self.exclude_folder)
        window.command_requested.connect(self.run_menu_command)
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
        """The user's settings, with WinLauncher's own data directory always excluded."""
        own_data = str(self.store.path.parent)
        return replace(self.settings, excluded_paths=(*self.settings.excluded_paths, own_data))

    def _explorer_session_open(self) -> bool:
        return self.window.isVisible() and self.window.scope is not None

    def _configured_roots(self) -> list[Path]:
        roots = indexed_application_roots(self.settings)
        return [source.root for source in catalog_sources(self.settings, roots)]

    # Keyboard -----------------------------------------------------------------

    def _start_keyboard(self) -> None:
        self.status.clear_topic(ProblemTopic.KEYBOARD)
        if not self.hooks_enabled:
            return
        from winlauncher.platform.keyboard import KeyboardListener

        self.keyboard = KeyboardListener(
            self.keyboard_events,
            interval_ms=self.settings.double_ctrl_ms,
            explorer_typing=self.settings.explorer_typing,
            quick_switch=self.settings.quick_switch,
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
            elif isinstance(intent, FolderJumpRequested):
                self._jump_in_dialog(intent)
            elif isinstance(intent, InputUnavailable):
                self.status.show_problem(ProblemTopic.KEYBOARD, intent.message, intent.message)

    def _start_ui_delay_check(self) -> None:
        self._ui_tick = time.monotonic()
        self.ui_timer.start()

    def _report_search_engine(self) -> None:
        """Searching without the Rust engine works, but slowly and without `contains:`."""
        reason = native_availability().reason
        if reason:
            message = f"{reason} Searches are slower and contains: is unavailable."
            self.status.show_problem(ProblemTopic.SEARCH_ENGINE, message)

    def _check_ui_delay(self) -> None:
        now = time.monotonic()
        elapsed_ms = (now - self._ui_tick) * 1000
        self._ui_tick = now
        if elapsed_ms > UI_DELAY_WARNING_MS:
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
        # Explorer searches its folder; a file dialog searches everywhere, so the
        # user can reach any folder, such as the desktop, from wherever it opened.
        scope = typing.folder if typing.kind is ViewKind.EXPLORER else None
        if (
            self.window.isVisible()
            and self.window.explorer_mode
            and self.window.scope == scope
            and self.window.explorer_view == typing.view
            and self.window.view_kind is typing.kind
        ):
            self.window.query.insert(typing.text)
            return
        self.window.present(
            text=typing.text,
            scope=scope,
            near=typing.window,
            explorer_view=typing.view,
            view_kind=typing.kind,
        )
        if scope is not None:
            self.indexing.request_folder(scope)

    def _jump_in_dialog(self, jump: FolderJumpRequested) -> None:
        """Ctrl+G in a file dialog; a dialog that closed meanwhile is no error to show."""
        self.tasks.submit(
            TaskCategory.ACTION,
            partial(self.actions.show_in_dialog, jump.folder, jump.dialog),
            lambda _result: None,
            lambda message: logger.info("Quick Switch jump skipped: %s", message),
        )

    # Folder watching ----------------------------------------------------------

    def _start_monitor(self) -> None:
        if os.name != "nt":
            return
        from winlauncher.platform.watcher import ChangeMonitor

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
        """WinLauncher's own catalog and log writes must not trigger repairs."""
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
            # The prefetch pool runs one task at a time: the start list then uses the
            # fresh index, and the next keystroke never pays for reloading it.
            self._warm_search_index()
            self.search.refresh()

    def _warm_search_index(self) -> None:
        """Load the Rust engine's memory index for the current catalog in the background."""
        self.tasks.submit(
            TaskCategory.PREFETCH,
            partial(warm_search_index, self.store),
            lambda _report: None,
            lambda message: logger.info("Search index warm-up failed: %s", message),
        )

    def _index_failed(self, message: str) -> None:
        self.status.set_activity(f"Index failed: {message}")

    def _update_catalog_count(self) -> None:
        self.status.set_catalog_count(self.indexing.catalog_count)
        self.tray.set_catalog_count(self.indexing.catalog_count)
        self.window.set_catalog_count(self.indexing.catalog_count)

    def rescan(self) -> None:
        """Refresh catalog (tray and menu): the catalog and every enabled plugin."""
        self.window.set_scope(None)
        self.indexing.request_full()
        self.plugin_refresh.refresh_all()

    def _after_hide(self) -> None:
        self.search.window_hidden()
        self.explorer_select.stop()
        self.explorer_hit = None
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
        dialog = InfoDialog("Index status", "", line_icon("activity", colors().accent), self.window)
        if report is None:
            dialog.subtitle.setText("The first scan is still running.")
        else:
            dialog.subtitle.setText(
                f"The last scan took {report.elapsed:.2f} s."
                if not report.errors
                else f"The last scan took {report.elapsed:.2f} s and hit some errors."
            )
            dialog.add_facts(
                [
                    ("Catalog entries", f"{self.indexing.catalog_count:,}"),
                    ("Scanned in the last run", f"{report.entries:,}"),
                    ("Excluded or skipped", f"{report.skipped:,}"),
                    ("Errors", f"{len(report.errors):,}"),
                ]
            )
            if problems := self.status.problem_text():
                dialog.add_widget(_details("Problems", problems))
            if report.errors:
                dialog.add_widget(_details("Scan errors", "\n".join(report.errors)))
        dialog.add_helper("Refresh now", lambda: (dialog.accept(), self.rescan()))
        dialog.add_button("Close", dialog.accept, primary=True)
        dialog.exec()
        dialog.deleteLater()

    # Item actions -------------------------------------------------------------

    def open_item(self, hit: LauncherHit, history_key: str) -> None:
        if isinstance(hit, PluginHit):
            self.window.hide()
            config = self.plugins.config(hit.plugin_id, self.settings.plugins)
            self._run_action(partial(self.plugins.open, hit, config))
            return
        if isinstance(hit, WebsiteTarget):
            self.window.hide()
            if not QDesktopServices.openUrl(QUrl(hit.url)):
                self._action_failed("The website could not be opened in your default browser.")
            return
        if self.window.shows_missing(hit):
            # Opening would fail; refreshing the folder drops the entry from the catalog.
            self.window.report_missing(hit.item.name)
            self.indexing.request_folder(hit.item.path.parent)
            return
        dialog, explorer = self._dialog_target(), self._explorer_target()
        self.window.hide()
        if dialog is not None:
            work = partial(self.actions.choose_in_dialog, hit.item.path, history_key, dialog)
        else:
            work = partial(self.actions.open, hit.item.path, history_key, explorer=explorer)
        self._run_action(work)

    def follow_in_explorer(self, hit: SearchHit) -> None:
        self.explorer_hit = hit
        self.explorer_select.start()

    def _select_in_explorer(self) -> None:
        hit, self.explorer_hit = self.explorer_hit, None
        explorer = self._explorer_target()
        if hit is None or explorer is None or not self.window.isVisible():
            return
        self.tasks.submit(
            TaskCategory.ACTION,
            partial(self.actions.select_in_explorer, hit.item.path, explorer),
            lambda _result: None,
            # A preview that fails, e.g. after the user switched tabs, is no error to show.
            lambda message: logger.info("Explorer selection skipped: %s", message),
        )

    def _explorer_target(self) -> ExplorerTarget | None:
        """The Explorer tab the bar belongs to, if the search started in Explorer."""
        view, scope = self.window.explorer_view, self.window.scope
        if view is None or not scope or self.window.view_kind is not ViewKind.EXPLORER:
            return None
        return ExplorerTarget(*view, scope)

    def _return_text_to_dialog(self, text: str) -> None:
        if (dialog := self._dialog_target()) is None:
            return
        self.tasks.submit(
            TaskCategory.ACTION,
            partial(self.actions.hand_back_to_dialog, text, dialog),
            lambda _result: None,
            lambda message: logger.info("Typed text not returned to the dialog: %s", message),
        )

    def _dialog_target(self) -> int | None:
        """The Open or Save dialog the bar belongs to, if the search started in one."""
        return self.window.explorer_view[0] if self.window.serves_file_dialog else None

    def show_shell_menu(self, hit: SearchHit, extended: bool) -> None:
        """Windows' own context menu for a result; the bar closes if a command ran."""
        if os.name != "nt":
            return
        from winlauncher.platform.shell_menu import show_shell_menu

        try:
            ran = show_shell_menu(hit.item.path, extended=extended)
        except OSError as exc:
            self._action_failed(str(exc))
            return
        if ran:
            self.window.hide()

    def run_menu_command(self, hit: SearchHit, command: MenuCommand) -> None:
        self.window.hide()
        self._run_action(partial(self.actions.run_command, command, hit.item.path))

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
        """Forget all launches after the user confirmed it; favorites stay."""
        if not messages.ask(
            self.window,
            "Clear the search history?",
            "WinLauncher forgets which results you opened and for which searches, so they "
            "no longer appear first or when the bar opens. Favorites are kept. This cannot "
            "be undone.",
            "Clear history",
            kind=messages.WARNING,
        ):
            return
        logger.info("Search history cleared at the user's request")
        self._run_action(self.store.clear_history, on_success=self.search.refresh)

    def _run_action(self, work, *, on_success=None) -> None:
        def succeeded(_result) -> None:
            if on_success is not None:
                on_success()

        self.tasks.submit(TaskCategory.ACTION, work, succeeded, self._action_failed)

    def _action_failed(self, message: str) -> None:
        messages.message(self.window, "That did not work", message, messages.WARNING)

    def exclude_folder(self, path: Path) -> None:
        """Let the user exclude a result itself, or its folder or one of their parents."""
        generated = tuple(f for f in (store_app_folder(), windows_tools_folder()) if f)
        candidates = exclusion_candidates(path, self._configured_roots(), generated)
        choices = [FolderChoice(path, self.store.count_within(path)) for path in candidates]
        dialog = ExcludeFolderDialog(choices, self.window)
        accepted = dialog.exec()
        chosen, pattern = dialog.chosen_folder(), dialog.pattern
        dialog.deleteLater()
        if not accepted:
            return
        if pattern is not None:
            patterns = tuple(dict.fromkeys((*self.settings.excluded_patterns, pattern)))
            changed = replace(self.settings, excluded_patterns=patterns)
        elif chosen is not None:
            excluded = tuple(dict.fromkeys((*self.settings.excluded_paths, str(chosen))))
            changed = replace(self.settings, excluded_paths=excluded)
        else:
            return
        try:
            self.apply_settings(changed)
        except OSError as exc:
            messages.message(self.window, "Settings not saved", str(exc), messages.ERROR)
            return
        if self.settings_dialog is not None:  # Its next Save must keep the new rule.
            self.settings_dialog.add_exclusion(
                folder=chosen if pattern is None else None, pattern=pattern
            )

    # Settings -----------------------------------------------------------------

    def edit_settings(self) -> None:
        """Show Settings as a window of its own, so the search bar stays usable in front.

        A second request brings the open window forward instead of opening another.
        The bar stays on top of every window, so it steps aside: what opened last
        is in front, and the bar comes back over Settings when it is called again.
        """
        self.window.hide()
        if (dialog := self.settings_dialog) is not None:
            dialog.showNormal()
            dialog.raise_()
            dialog.activateWindow()
            return
        self._sync_autostart()
        dialog = self.settings_dialog = SettingsDialog(
            self.settings,
            save=self.apply_settings,
            startup_enabled=self.tray.autostart.isChecked(),
            set_startup=self._set_autostart if os.name == "nt" else None,
            startup_state=self.tray.autostart.isChecked,
            website_icons=self.website_icons,
            plugins=self.plugin_refresh,
            plugin_folder=str(self.data_dir / "plugins"),
        )
        dialog.finished.connect(self._settings_closed)
        dialog.show()
        dialog.activateWindow()

    def _settings_closed(self) -> None:
        if (dialog := self.settings_dialog) is not None:
            self.settings_dialog = None
            dialog.deleteLater()

    def apply_settings(self, settings: Settings) -> None:
        """Save first (raises OSError), then restart only what the change affects.

        Once saved, the settings stay accepted; a failing restart is reported as a
        problem instead of leaving the dialog open over a half-applied change.
        """
        self.settings_file.save(settings)
        previous, self.settings = self.settings, settings
        logger.info(
            "Settings saved: indexing_changed=%s input_changed=%s",
            previous.indexing_key != settings.indexing_key,
            previous.input_key != settings.input_key,
        )
        try:
            self._apply_changes(previous, settings)
        except Exception as exc:
            logger.exception("Saved settings could not be fully applied")
            messages.message(
                self.window,
                "Settings saved",
                f"Some changes take effect only after restarting WinLauncher: {exc}",
                messages.WARNING,
            )
        self.search.refresh()

    def _apply_changes(self, previous: Settings, settings: Settings) -> None:
        self._apply_window_settings(settings)
        if previous.input_key != settings.input_key:
            self._stop_keyboard()
            self._start_keyboard()
        if previous.indexing_key != settings.indexing_key:
            self._restart_indexing()
        elif previous.rescan_seconds != settings.rescan_seconds:
            self.indexing.restart_periodic_timer()
        if previous.plugins != settings.plugins:
            self.plugin_refresh.apply()

    def _apply_window_settings(self, settings: Settings) -> None:
        theme().use(Appearance(settings.appearance))
        self.window.set_auto_hide(settings.auto_hide, settings.auto_hide_seconds)
        self.window.set_menu_commands(settings.menu_commands)

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
            messages.message(self.window, "Autostart not changed", str(exc), messages.ERROR)

    # About, updates and restart ----------------------------------------------

    def show_about(self) -> None:
        dialog = AboutDialog(self.data_dir, self.window)
        dialog.exec()
        dialog.deleteLater()

    def check_for_updates(self) -> None:
        if self.updater.updating:
            return
        if not self.updater.supported:
            messages.message(
                self.window,
                "Git not found",
                "WinLauncher looks for new releases with git. Install git to get update notices.",
                messages.WARNING,
            )
            return
        if self.updater.latest is not None:
            self._offer_update(self.updater.latest)
            return
        self.update_check_requested = True
        self.status.set_activity("Checking for updates…")
        self.updater.check_now()

    def _update_checked(self, reachable: bool) -> None:
        if not self.update_check_requested:
            return
        self.update_check_requested = False
        self.status.set_activity("")
        if not reachable:
            messages.message(
                self.window,
                "Update check failed",
                "The release list could not be loaded. Try again later.",
                messages.WARNING,
            )
        elif self.updater.latest is None:
            messages.message(
                self.window,
                "WinLauncher is up to date",
                f"You are running the newest release, {__version__}.",
                messages.SUCCESS,
            )
        else:
            self._offer_update(self.updater.latest)

    def _offer_update(self, release: Release) -> None:
        """A checkout updates itself; other installations get the command to run."""
        if not self.updater.can_apply:
            UpdateCommandDialog(
                release, install_command(self.updater.installation, release), self.window
            ).exec()
            return
        root = self.updater.root
        answer = messages.show_message(
            self.window,
            f"WinLauncher {release.version} is available",
            f"You are running {__version__}. Update now? A restart finishes the update.",
            [("Cancel", messages.SECONDARY), ("Update", messages.PRIMARY)],
            messages.UPDATE,
            details=lambda: release_changes(root, release.tag) or "No changes to show.",
            details_labels=("Show changes", "Hide changes"),
            window_title="Update WinLauncher",
        )
        if answer == "Update":
            self._start_update(release, forced=False)

    def _start_update(self, release: Release, *, forced: bool) -> None:
        self.update_release = release
        self.status.set_activity("Updating WinLauncher…")
        if forced:
            self.updater.force_apply()
        else:
            self.updater.apply()

    def _update_available(self, _available: bool) -> None:
        latest = self.updater.latest
        self.tray.set_update_version(latest.version if latest else None)
        if latest is not None and not self.update_check_requested:
            self.tray.icon.showMessage(
                "WinLauncher",
                f"WinLauncher {latest.version} is available. "
                f"Choose “Update to {latest.version}” in the tray menu to install it.",
            )

    def _update_finished(self, succeeded: bool, changed: bool, output: str) -> None:
        self.status.set_activity("")
        release, forced = self.update_release, self.updater.forced
        version = release.version if release else "the new release"
        if not succeeded:
            self._update_failed(release, version, output or "No output from git.", forced=forced)
            return
        set_aside = "Local changes were set aside (git stash). " if forced else ""
        if not changed:
            messages.message(
                self.window,
                "WinLauncher is up to date",
                f"It already runs {version}. {set_aside}".strip(),
                messages.SUCCESS,
            )
            return
        # The new release's packages, including its Rust engine, install at the next start.
        mark_reinstall_pending(self.data_dir)
        restart = messages.show_message(
            self.window,
            f"Updated to {version}",
            f"{set_aside}WinLauncher finishes the update when it starts again. Restart now?",
            [("Later", messages.SECONDARY), ("Restart now", messages.PRIMARY)],
            messages.SUCCESS,
            window_title="Update WinLauncher",
        )
        if restart == "Restart now":
            self.restart()

    def _update_failed(
        self, release: Release | None, version: str, output: str, *, forced: bool
    ) -> None:
        """Explain the failure; git's output stays behind Details. Offer a forced update once."""
        offer_force = not forced and release is not None
        buttons = [("Close", messages.SECONDARY)]
        if offer_force:
            buttons.append(("Force update…", messages.DANGER))
        answer = messages.show_message(
            self.window,
            "Update failed",
            f"The update to {version} could not be installed, usually because files in the "
            "WinLauncher folder were changed locally.",
            buttons,
            messages.ERROR,
            details=output,
            window_title="Update WinLauncher",
        )
        if answer != "Force update…" or release is None:
            return
        if messages.ask(
            self.window,
            f"Force the update to {version}?",
            "Local changes to WinLauncher's files are kept aside (git stash) and can be "
            "restored from there; your settings are not affected.",
            "Force update",
            kind=messages.WARNING,
        ):
            self._start_update(release, forced=True)

    def restart(self) -> None:
        """Start a replacement that waits for this process's lock, then quit."""
        arguments = ["-m", "winlauncher", "--replace", "--data-dir", str(self.data_dir)]
        if self.window.isVisible():
            arguments.append("--show")
        if not self.hooks_requested:
            arguments.append("--no-hooks")
        self._stop_keyboard()  # Never run two global keyboard hooks at once.
        if not QProcess.startDetached(sys.executable, arguments, str(PACKAGE_PARENT)):
            self._start_keyboard()
            messages.message(
                self.window,
                "Restart failed",
                "WinLauncher could not be restarted. Quit it and start it again.",
                messages.ERROR,
            )
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
        self.plugin_refresh.close()
        self._stop_keyboard()
        self._stop_monitor()
        self.updater.stop()
        self.tasks.close()
        self.window.delegate.icons.close()
        self.window.close_focus_watch()
        self.website_icons.close()
        self.tray.hide()
        self.window.hide()
        if self.settings_dialog is not None:
            self.settings_dialog.reject()


def _details(title: str, text: str) -> QPlainTextEdit:
    """Read-only lines, such as scan errors, below a status dialog's facts."""
    box = QPlainTextEdit(text)
    box.setReadOnly(True)
    box.setAccessibleName(title)
    box.setPlaceholderText(title)
    box.setMaximumHeight(DETAILS_HEIGHT)
    return box
