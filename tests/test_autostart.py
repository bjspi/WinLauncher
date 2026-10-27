"""Exercise real Windows shortcuts in an isolated folder, never the user's Startup folder."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from winlauncher.platform import launcher
from winlauncher.platform.shortcuts import load_shortcut, write_target_shortcut
from winlauncher.platform.startup import SHORTCUT_DESCRIPTION, StartupShortcut


@unittest.skipUnless(os.name == "nt", "Windows shortcuts require COM")
class AutostartTest(unittest.TestCase):
    def setUp(self):
        import pythoncom

        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-startup-link-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, XDG_BIN_HOME=str(self.folder / "bin"))
        environment.start()
        self.addCleanup(environment.stop)
        self.data = self.folder / "data with spaces"
        self.startup = StartupShortcut(self.data, startup_folder=self.folder)
        pythoncom.CoInitialize()
        self.addCleanup(pythoncom.CoUninitialize)

    def test_create_read_update_and_remove_real_shortcut(self):
        self.assertFalse(self.startup.is_enabled())
        self.startup.set_enabled(True)
        self.assertTrue(self.startup.is_enabled())
        shortcut = load_shortcut(self.folder / "WinLauncher.lnk")
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.exists():
            executable = Path(sys.executable)
        self.assertEqual(Path(shortcut.GetPath(0)[0]), executable)
        self.assertEqual(shortcut.GetDescription(), SHORTCUT_DESCRIPTION)
        self.assertEqual(
            shortcut.GetArguments(),
            subprocess.list2cmdline(
                ["-m", "winlauncher", "--background", "--data-dir", str(self.data)]
            ),
        )
        self.assertTrue(
            (Path(shortcut.GetWorkingDirectory()) / "winlauncher" / "__main__.py").is_file()
        )
        self.startup.set_enabled(True)
        self.assertEqual(list(self.folder.iterdir()), [self.folder / "WinLauncher.lnk"])
        self.startup.set_enabled(False)
        self.assertFalse(self.startup.is_enabled())
        self.startup.set_enabled(False)

    def test_unrelated_shortcut_is_never_replaced_or_deleted(self):
        path = self.folder / "WinLauncher.lnk"
        write_target_shortcut(path, sys.executable, description="Another application")
        before = path.read_bytes()
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(OSError):
                self.startup.set_enabled(enabled)
            self.assertEqual(path.read_bytes(), before)

    def test_autostart_does_not_load_windows_script_host(self):
        with patch("win32com.client.Dispatch", side_effect=AssertionError("WScript loaded")):
            self.startup.set_enabled(True)
            self.assertTrue(self.startup.is_enabled())
            self.startup.set_enabled(False)
            self.assertFalse(self.startup.is_enabled())

    def test_unreadable_shortcut_is_never_replaced_or_deleted(self):
        before = b"Not a Windows shortcut"
        self.startup.path.write_bytes(before)
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(OSError):
                self.startup.set_enabled(enabled)
            self.assertEqual(self.startup.path.read_bytes(), before)

    def test_failed_save_keeps_the_original_shortcut_and_cleans_up(self):
        import pywintypes

        self.startup.set_enabled(True)
        before = self.startup.path.read_bytes()
        with patch(
            "winlauncher.platform.shortcuts._save",
            side_effect=pywintypes.com_error(-2147467259, "Save failed", None, None),
        ):
            with self.assertRaisesRegex(OSError, "Save failed"):
                self.startup.set_enabled(True)
        self.assertEqual(self.startup.path.read_bytes(), before)
        self.assertEqual(list(self.folder.iterdir()), [self.startup.path])

    def test_migration_preserves_data_arguments_and_does_not_enable_absent_autostart(self):
        with patch.object(launcher, "_add_to_user_path"):
            host = launcher.install_launcher()
        if host is None:
            self.skipTest("The host is unavailable on this architecture")
        self.startup.migrate()
        self.assertFalse(self.startup.path.exists())
        path = self.startup.path
        before = subprocess.list2cmdline(
            ["-m", "winlauncher", "--background", "--data-dir", str(self.data), "--no-hooks"]
        )
        write_target_shortcut(
            path,
            str(launcher.windowless_python()),
            before,
            description=SHORTCUT_DESCRIPTION,
            working_directory=str(self.folder),
        )
        # This start uses another data directory: migration must keep the saved one.
        with patch("win32com.client.Dispatch", side_effect=AssertionError("WScript loaded")):
            StartupShortcut(self.folder / "other data", startup_folder=self.folder).migrate()
        shortcut = load_shortcut(path)
        self.assertEqual(Path(shortcut.GetPath(0)[0]), host)
        self.assertEqual(shortcut.GetArguments(), before.removeprefix("-m winlauncher "))
        self.assertEqual(Path(shortcut.GetWorkingDirectory()), self.folder)
        self.assertEqual(shortcut.GetIconLocation(), (str(host), 0))
        self.assertTrue(self.startup.is_enabled())
        del shortcut
        unchanged = path.stat().st_mtime_ns
        self.startup.migrate()
        self.assertEqual(path.stat().st_mtime_ns, unchanged)
        self.startup.set_enabled(False)
        self.assertFalse(path.exists())

    def test_migration_leaves_unrelated_and_similarly_named_modules_untouched(self):
        with patch.object(launcher, "_add_to_user_path"):
            if launcher.install_launcher() is None:
                self.skipTest("The host is unavailable on this architecture")
        for description, arguments in (
            ("Another application", "-m winlauncher --background"),
            (SHORTCUT_DESCRIPTION, "-m winlauncher-other --background"),
        ):
            with self.subTest(description=description, arguments=arguments):
                write_target_shortcut(
                    self.startup.path, sys.executable, arguments, description=description
                )
                before = self.startup.path.read_bytes()
                self.startup.migrate()
                self.assertFalse(self.startup.is_enabled())
                self.assertEqual(self.startup.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
