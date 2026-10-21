"""Persistent diagnostics, migration, focus requests and UI responsiveness."""

from __future__ import annotations

import importlib
import logging
import os
import tempfile
import threading
import time
import unicodedata
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ci_timing import PROMPT_LIMIT_S
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QSslSocket
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from winlauncher import __main__ as entry
from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.model import CatalogItem, ItemKind, normalize, path_key
from winlauncher.catalog.priority import PriorityPolicy
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.native_engine import priority_spec, query_spec
from winlauncher.search.query import SearchQuery
from winlauncher.search.ranking import Ranker
from winlauncher.ui import icons

FAKE_ICON_HANDLE = 0x1234


class RuntimeDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_data_lives_in_the_home_directory_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.object(Path, "home", return_value=home):
                self.assertEqual(entry._default_data_directory(), home / ".WinLauncher")

    @unittest.skipUnless(os.name == "nt", "Schannel is Windows' TLS")
    def test_website_icons_use_windows_tls_not_an_openssl_dll_from_path(self):
        entry._use_windows_tls()
        self.assertEqual(QSslSocket.activeBackend(), entry.WINDOWS_TLS_BACKEND)

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
            self.assertEqual(len(list(data.glob("winlauncher.log*"))), 3)
            latest = (data / "winlauncher.log").read_text(encoding="utf-8")
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
            native = importlib.import_module("winlauncher_native")
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
            started = time.perf_counter()
            cancelled_at = []
            cancelled_while_running = []
            with ThreadPoolExecutor(1) as pool:
                work = pool.submit(
                    native.rank_sqlite, str(store.path), sql, parameters, spec, rules, allows, flag
                )

                def pulse():
                    if entered.is_set() and not cancelled_at:
                        edit.insert("next query")
                        cancelled_while_running.append(not work.done())
                        cancelled_at.append(time.perf_counter())
                        flag.set()
                    if work.done() or time.perf_counter() - started > PROMPT_LIMIT_S:
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
            # A search holding the GIL would let Qt run only after it finished.
            self.assertEqual(cancelled_while_running, [True], "Qt was blocked during the search")
            self.assertLess(time.perf_counter() - cancelled_at[0], PROMPT_LIMIT_S)

    @unittest.skipUnless(os.name == "nt", "Windows foreground activation")
    def test_foreground_retry_detaches_and_does_not_chase_another_app(self):
        from winlauncher.platform import windows

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
