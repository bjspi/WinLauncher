"""Persistent diagnostics, migration, focus requests and UI responsiveness."""

from __future__ import annotations

import importlib
import logging
import os
import sqlite3
import tempfile
import threading
import time
import unicodedata
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QLockFile, QTimer
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from pathscout import __main__ as entry
from pathscout.catalog.exclusions import ExclusionPolicy
from pathscout.catalog.model import CatalogItem, ItemKind, normalize, path_key
from pathscout.catalog.priority import PriorityPolicy
from pathscout.catalog.store import CatalogStore
from pathscout.search.native_engine import priority_spec, query_spec
from pathscout.search.query import SearchQuery
from pathscout.search.ranking import Ranker
from pathscout.settings import Settings, SettingsFile
from pathscout.ui import icons

# Qt must keep running while Rust searches: most timer ticks stay prompt and none
# stalls like a held GIL would.
RESPONSIVE_TICK_S = 0.1
PROMPT_TICK_SHARE = 0.95
STALL_LIMIT_S = 1.0
FAKE_ICON_HANDLE = 0x1234


class RuntimeDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_default_home_and_migration_preserve_config_wal_and_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "home" / ".PathScout"
            data.mkdir(parents=True)
            legacy = root / "local" / "PathScout"
            legacy.mkdir(parents=True)
            settings = Settings(roots=(), max_results=77)
            SettingsFile(legacy / "settings.json").save(settings)
            with (
                patch.object(Path, "home", return_value=root / "home"),
                patch.dict(os.environ, LOCALAPPDATA=str(root / "local")),
                closing(sqlite3.connect(legacy / "catalog.sqlite3")) as connection,
            ):
                self.assertEqual(entry._default_data_directory(), data)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE favorites (path TEXT)")
                connection.execute("INSERT INTO favorites VALUES ('saved favorite')")
                connection.commit()
                entry._migrate_legacy_data(data.resolve())
                self.assertEqual(SettingsFile(data / "settings.json").load(), settings)
                with closing(sqlite3.connect(data / "catalog.sqlite3")) as copied:
                    self.assertEqual(
                        copied.execute("SELECT * FROM favorites").fetchall(), [("saved favorite",)]
                    )
                updated = Settings(roots=(), max_results=100)
                SettingsFile(data / "settings.json").save(updated)
                entry._migrate_legacy_data(data.resolve())
                self.assertEqual(SettingsFile(data / "settings.json").load(), updated)
                self.assertEqual(SettingsFile(legacy / "settings.json").load(), settings)
                self.assertTrue((legacy / "catalog.sqlite3").is_file())
                custom = root / "custom"
                custom.mkdir()
                entry._migrate_legacy_data(custom)
                self.assertFalse(list(custom.iterdir()))

    def test_migration_refuses_a_running_legacy_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "PathScout"
            legacy.mkdir()
            (legacy / "settings.json").write_text("{}")
            data = root / ".PathScout"
            data.mkdir()
            ownership = QLockFile(str(legacy / "instance.lock"))
            self.assertTrue(ownership.tryLock(0))
            try:
                with (
                    patch.object(entry, "_default_data_directory", return_value=data),
                    patch.dict(os.environ, LOCALAPPDATA=str(root)),
                    self.assertRaisesRegex(OSError, "Close the PathScout instance"),
                ):
                    entry._migrate_legacy_data(data.resolve())
                self.assertFalse((data / "settings.json").exists())
            finally:
                ownership.unlock()

    def test_log_rotation_is_bounded_and_flushes_unicode(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(logging.getLogger(), "handlers", []),
            patch.object(logging.getLogger(), "level", logging.INFO),
            patch.object(entry, "LOG_BYTES", 350),
            patch.object(entry, "LOG_BACKUPS", 2),
        ):
            data = Path(temporary)
            listener = entry._configure_logging(data)
            try:
                for index in range(30):
                    logging.info("Rotation record %d: %s", index, "Grüße" * 10)
                logging.warning("Final diagnostic: Straße")
            finally:
                listener.stop()
                for handler in listener.handlers:
                    handler.close()
            self.assertEqual(len(list(data.glob("pathscout.log*"))), 3)
            latest = (data / "pathscout.log").read_text(encoding="utf-8")
            self.assertIn("Final diagnostic: Straße", latest)
            self.assertIn("MainThread", latest)

    @unittest.skipUnless(os.name == "nt", "Windows shell icons")
    def test_slow_shell_lookup_leaves_ui_responsive_and_caches_result(self):
        entered, release = threading.Event(), threading.Event()
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(0xFF00FF00)

        def slow_lookup(*_):
            entered.set()
            release.wait(3)
            return FAKE_ICON_HANDLE

        cache = icons.ResultIcons()
        changed = []
        cache.changed.connect(lambda: changed.append(True))
        item = CatalogItem(Path("C:/example/slow.exe"), ItemKind.APP)
        with (
            patch.object(icons, "_shell_icon", side_effect=slow_lookup) as load,
            patch.object(icons, "_image_from_icon", return_value=image) as convert,
        ):
            try:
                fallback = cache.icon_for(item)
                self.assertTrue(entered.wait(1))
                ticks = []
                QTimer.singleShot(0, lambda: ticks.append(True))
                QTest.qWait(25)
                self.assertTrue(ticks, "A shell lookup blocked the GUI event loop")
                self.assertFalse(changed)
                self.assertEqual(cache.icon_for(item).cacheKey(), fallback.cacheKey())
                release.set()
                for _ in range(100):
                    QTest.qWait(10)
                    if changed:
                        break
                self.assertTrue(changed)
                self.assertNotEqual(cache.icon_for(item).cacheKey(), fallback.cacheKey())
                load.assert_called_once()
                # The loader thread hands over the handle; the GUI thread makes the image.
                convert.assert_called_once_with(FAKE_ICON_HANDLE)
            finally:
                release.set()
                cache.close()
                # A closed cache ignores late icons; the shared loader frees their handles.
                self.assertNotIn(id(cache), cache._loader._requesters)

    def test_qt_can_type_and_cancel_while_rust_search_runs(self):
        try:
            native = importlib.import_module("pathscout_native")
        except ImportError:
            self.skipTest("Optional Rust extension is not installed")
        native.use_unicode(unicodedata.unidata_version)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CatalogStore(root / "catalog.sqlite3")
            path = root / ("a_b_" * 20 + ".txt")
            row = (
                str(path),
                path_key(path),
                "file",
                normalize(path.name),
                normalize(str(path)),
                "txt",
                0,
                0.0,
                0,
                0,
                0.0,
                0,
                False,
                False,
            )
            query = SearchQuery(terms=("ab" * 20,))
            spec = query_spec(query, Ranker(query), 10)
            rules = priority_spec(PriorityPolicy())
            columns = ",".join(f":r{i}" for i in range(14))
            sql = f"WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<999999) SELECT {columns} FROM n"
            parameters = {f"r{i}": value for i, value in enumerate(row)}
            flag = native.CancelFlag()
            entered = threading.Event()
            policy = ExclusionPolicy(markers=())

            def allows(path, directory, application):
                entered.set()
                return policy.allows(Path(path), directory=directory, application=application)

            edit, loop, timer = QLineEdit(), QEventLoop(), QTimer()
            ticks = [time.perf_counter()]
            cancelled_at = []
            with ThreadPoolExecutor(1) as pool:
                work = pool.submit(
                    native.rank_sqlite, str(store.path), sql, parameters, spec, rules, allows, flag
                )

                def pulse():
                    ticks.append(time.perf_counter())
                    if entered.is_set() and not cancelled_at:
                        edit.insert("next query")
                        cancelled_at.append(time.perf_counter())
                        flag.set()
                    if work.done() or time.perf_counter() - ticks[0] > 5:
                        flag.set()
                        loop.quit()

                timer.timeout.connect(pulse)
                timer.start(5)
                try:
                    loop.exec()
                    self.assertEqual(work.result(timeout=3), [])
                finally:
                    flag.set()
                    timer.stop()
            self.assertEqual(edit.text(), "next query")
            self.assertTrue(cancelled_at, "Qt could not run Python while Rust was searching")
            self.assertLess(time.perf_counter() - cancelled_at[0], RESPONSIVE_TICK_S)
            # A search holding the GIL would stall Qt for the whole search; on shared CI
            # machines single ticks may still lag, so only most ticks must be prompt.
            gaps = sorted(b - a for a, b in zip(ticks, ticks[1:], strict=False))
            self.assertLess(gaps[int(len(gaps) * PROMPT_TICK_SHARE)], RESPONSIVE_TICK_S)
            self.assertLess(gaps[-1], STALL_LIMIT_S)

    @unittest.skipUnless(os.name == "nt", "Windows foreground activation")
    def test_foreground_retry_detaches_and_does_not_chase_another_app(self):
        from pathscout.platform import windows

        foreground = {"hwnd": 10, "attached": False}

        def activate(hwnd):
            if foreground["attached"]:
                foreground["hwnd"] = hwnd

        def attach(current, other, enabled):
            foreground["attached"] = enabled
            return True

        api = Mock()
        api.GetForegroundWindow.side_effect = lambda: foreground["hwnd"]
        api.GetWindowThreadProcessId.return_value = 2
        api.SetForegroundWindow.side_effect = activate
        api.AttachThreadInput.side_effect = attach
        with (
            patch.object(windows, "user32", api),
            patch.object(windows.kernel32, "GetCurrentThreadId", return_value=1),
        ):
            self.assertTrue(windows.activate_window(20))
            self.assertFalse(foreground["attached"])
            self.assertEqual(api.AttachThreadInput.call_count, 2)
            api.AttachThreadInput.reset_mock()
            api.GetForegroundWindow.side_effect = [10, 30, 30, 30]
            with (
                patch.object(windows, "foreground_details", return_value="process=other.exe"),
                self.assertLogs(windows.logger, level="WARNING") as log,
            ):
                self.assertFalse(windows.activate_window(20))
            api.AttachThreadInput.assert_not_called()
            self.assertIn("process=other.exe", log.output[0])
