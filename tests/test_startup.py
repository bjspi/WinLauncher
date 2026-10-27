"""Exercise the real entry point and its per-directory single-instance activation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


def child(data: Path) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from winlauncher import __main__ as entry
    from winlauncher.ui.window import LauncherWindow

    app = QApplication([])
    original = LauncherWindow.present

    def present(window, **kwargs):
        original(window, **kwargs)
        if (data / "ready").exists():
            (data / "activated").touch()
            QTimer.singleShot(0, app.quit)

    QTimer.singleShot(0, lambda: (data / "ready").touch())
    QTimer.singleShot(12000, app.quit)
    with (
        patch.object(entry, "QApplication", return_value=app),
        patch.object(entry, "_migrate_launcher", return_value=False),
        patch.object(LauncherWindow, "present", present),
    ):
        if os.environ.get("WINLAUNCHER_TEST_START_BARRIER"):
            (data / f"waiting-{os.getpid()}").touch()
            deadline = time.monotonic() + 8
            while not (data / "start").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("Startup barrier not released")
                time.sleep(0.01)
        background = (
            ["--background"] if os.environ.get("WINLAUNCHER_TEST_FOREGROUND") != "1" else []
        )
        sys.argv = ["winlauncher", *background, "--no-hooks", "--data-dir", str(data)]
        result = entry.main()
    raise SystemExit(result)


class StartupTest(unittest.TestCase):
    def test_simultaneous_starts_elect_one_primary(self):
        from winlauncher.settings import Settings, SettingsFile

        with tempfile.TemporaryDirectory(prefix="winlauncher-startup-race-") as temporary:
            data = Path(temporary)
            SettingsFile(data / "settings.json").save(
                Settings(roots=(), index_apps=False, index_windows_tools=False)
            )
            # Foreground starts: the losing process must ask the winner to show itself.
            env = dict(
                os.environ,
                QT_QPA_PLATFORM="offscreen",
                WINLAUNCHER_TEST_START_BARRIER="1",
                WINLAUNCHER_TEST_FOREGROUND="1",
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--child", str(data)],
                    cwd=PROJECT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            try:
                deadline = time.monotonic() + 8
                while len(list(data.glob("waiting-*"))) < 2:
                    self.assertLess(
                        time.monotonic(), deadline, "Startup children did not reach barrier"
                    )
                    time.sleep(0.02)
                (data / "start").touch()
                for process in processes:
                    stdout, stderr = process.communicate(timeout=12)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    self.assertNotIn("Traceback", stderr)
                self.assertTrue((data / "activated").exists())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)

    def test_second_process_activates_existing_instance(self):
        from winlauncher.settings import Settings, SettingsFile

        with tempfile.TemporaryDirectory(prefix="winlauncher-startup-") as temporary:
            data = Path(temporary)
            SettingsFile(data / "settings.json").save(
                Settings(roots=(), index_apps=False, index_windows_tools=False)
            )
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--child", str(data)],
                cwd=PROJECT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 8
                while not (data / "ready").exists() and process.poll() is None:
                    self.assertLess(time.monotonic(), deadline, "First instance did not start")
                    time.sleep(0.05)
                self.assertIsNone(process.poll(), "First instance exited before activation")
                background = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "winlauncher",
                        "--background",
                        "--no-hooks",
                        "--data-dir",
                        str(data),
                    ],
                    cwd=PROJECT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                self.assertEqual(background.returncode, 0, background.stderr)
                time.sleep(0.5)
                self.assertFalse(
                    (data / "activated").exists(), "A background start must not show the launcher"
                )
                second = subprocess.run(
                    [sys.executable, "-m", "winlauncher", "--no-hooks", "--data-dir", str(data)],
                    cwd=PROJECT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                self.assertEqual(second.returncode, 0, second.stderr)
                stdout, stderr = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertTrue(
                    (data / "activated").exists(), "Second instance did not activate UI"
                )
                self.assertNotIn("Traceback", stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        sys.path.insert(0, str(PROJECT))
        child(Path(sys.argv[2]))
    else:
        unittest.main()
