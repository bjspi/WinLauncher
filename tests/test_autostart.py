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
from winlauncher.platform.startup import SHORTCUT_DESCRIPTION, StartupShortcut


@unittest.skipUnless(os.name == "nt", "Windows shortcuts require COM")
class AutostartTest(unittest.TestCase):
    def setUp(self):
        import pythoncom
        import win32com.client

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
        self.shell = win32com.client.Dispatch("WScript.Shell")
        # Cleanups run in reverse order: release COM objects before uninitializing.
        self.addCleanup(delattr, self, "shell")

    def test_create_read_update_and_remove_real_shortcut(self):
        self.assertFalse(self.startup.is_enabled())
        self.startup.set_enabled(True)
        self.assertTrue(self.startup.is_enabled())
        shortcut = self.shell.CreateShortcut(str(self.folder / "WinLauncher.lnk"))
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.exists():
            executable = Path(sys.executable)
        self.assertEqual(Path(shortcut.TargetPath), executable)
        self.assertEqual(shortcut.Description, SHORTCUT_DESCRIPTION)
        self.assertEqual(
            shortcut.Arguments,
            subprocess.list2cmdline(
                ["-m", "winlauncher", "--background", "--data-dir", str(self.data)]
            ),
        )
        self.assertTrue((Path(shortcut.WorkingDirectory) / "winlauncher" / "__main__.py").is_file())
        self.startup.set_enabled(True)
        self.assertEqual(list(self.folder.iterdir()), [self.folder / "WinLauncher.lnk"])
        self.startup.set_enabled(False)
        self.assertFalse(self.startup.is_enabled())
        self.startup.set_enabled(False)

    def test_unrelated_shortcut_is_never_replaced_or_deleted(self):
        path = self.folder / "WinLauncher.lnk"
        shortcut = self.shell.CreateShortcut(str(path))
        shortcut.TargetPath = sys.executable
        shortcut.Description = "Another application"
        shortcut.Save()
        before = path.read_bytes()
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.assertRaises(OSError):
                self.startup.set_enabled(enabled)
            self.assertEqual(path.read_bytes(), before)

    def test_migration_preserves_data_arguments_and_does_not_enable_absent_autostart(self):
        with patch.object(launcher, "_add_to_user_path"):
            host = launcher.install_launcher()
        if host is None:
            self.skipTest("The host is unavailable on this architecture")
        self.startup.migrate()
        self.assertFalse(self.startup.path.exists())
        path = self.startup.path
        shortcut = self.shell.CreateShortcut(str(path))
        shortcut.TargetPath = str(launcher.windowless_python())
        shortcut.Arguments = subprocess.list2cmdline(
            ["-m", "winlauncher", "--background", "--data-dir", str(self.data), "--no-hooks"]
        )
        shortcut.Description = SHORTCUT_DESCRIPTION
        shortcut.WorkingDirectory = str(self.folder)
        shortcut.Save()
        before = shortcut.Arguments
        del shortcut
        # This start uses another data directory: migration must keep the saved one.
        StartupShortcut(self.folder / "other data", startup_folder=self.folder).migrate()
        shortcut = self.shell.CreateShortcut(str(path))
        self.assertEqual(Path(shortcut.TargetPath), host)
        self.assertEqual(shortcut.Arguments, before.removeprefix("-m winlauncher "))
        self.assertEqual(Path(shortcut.WorkingDirectory), self.folder)
        self.assertTrue(self.startup.is_enabled())
        del shortcut
        unchanged = path.stat().st_mtime_ns
        self.startup.migrate()
        self.assertEqual(path.stat().st_mtime_ns, unchanged)
        self.startup.set_enabled(False)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
