"""Exercise the real host, safe migration, and restart/update ownership on Windows."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import venv
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from winlauncher.platform import launcher

PROJECT = Path(__file__).resolve().parents[1]
WINDOWS_X64 = os.name == "nt" and launcher.sysconfig.get_platform() == "win-amd64"
HOST_EXIT_TIMEOUT_MS = 10000

PROBE = """
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from PySide6.QtWidgets import QApplication

app = QApplication([])
image = ctypes.create_unicode_buffer(32768)
ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image))
child = Path(sys.argv[1]).with_suffix('.child.json')
code = 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"python":sys.executable,"prefix":sys.prefix}))'
result = subprocess.run([sys.executable, '-c', code, str(child)])
Path(sys.argv[1]).write_text(json.dumps({
    'image': image.value, 'python': sys.executable, 'prefix': sys.prefix,
    'base_prefix': sys.base_prefix, 'arguments': sys.argv[2:],
    'launcher_environment': os.environ.get('__PYVENV_LAUNCHER__'),
    'child_exit': result.returncode,
}), encoding='utf-8')
raise SystemExit(23)
"""


@unittest.skipUnless(WINDOWS_X64, "The prebuilt host is for Windows x64")
class LoaderTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-host Grüße ")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.bin = self.folder / "bin"
        self.patch_env = patch.dict(
            os.environ, XDG_BIN_HOME=str(self.bin), QT_QPA_PLATFORM="offscreen"
        )
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        path_update = patch.object(launcher, "_add_to_user_path")
        self.path_update = path_update.start()
        self.addCleanup(path_update.stop)

    def test_install_is_idempotent_and_preserves_foreign_commands(self):
        host = launcher.install_launcher()
        runtime = launcher.read_host(host)
        self.assertEqual(runtime, launcher._runtime())
        initial = host.stat().st_mtime_ns
        self.assertEqual(launcher.install_launcher(), host)
        self.assertEqual(host.stat().st_mtime_ns, initial)
        self.assertEqual(launcher.application_command(["--show"]), [str(host), "--show"])
        self.path_update.assert_called_with(self.bin)
        host.write_bytes(b"An unrelated command")
        with self.assertRaisesRegex(FileExistsError, "unrelated"):
            launcher.install_launcher()
        self.assertEqual(host.read_bytes(), b"An unrelated command")

    def test_old_pip_shim_is_replaced_but_other_installations_are_preserved(self):
        self.bin.mkdir()
        host = self.bin / launcher.HOST_NAME
        host.write_bytes(b"#!" + str(Path(sys.executable)).encode() + b"\n")
        with zipfile.ZipFile(host, "a") as archive:
            archive.writestr("__main__.py", "from winlauncher.__main__ import main\nmain()\n")
        self.assertEqual(launcher.install_launcher(), host)
        other = replace(launcher._runtime(), package=self.folder)
        launcher.write_host(host, other)
        before = host.read_bytes()
        with self.assertRaisesRegex(FileExistsError, "Another WinLauncher"):
            launcher.install_launcher()
        self.assertEqual(host.read_bytes(), before)

    def test_template_integrity_is_checked_before_installation(self):
        template = self.folder / "template.exe"
        template.write_bytes(launcher.TEMPLATE.read_bytes() + b"changed")
        template.with_suffix(".json").write_bytes(
            launcher.TEMPLATE.with_suffix(".json").read_bytes()
        )
        with patch.object(launcher, "TEMPLATE", template):
            with self.assertRaisesRegex(OSError, "integrity"):
                launcher.install_launcher()
        self.assertFalse(self.bin.exists())

    def test_real_host_preserves_process_identity_venv_arguments_qt_and_subprocesses(self):
        environment = self.folder / "environment with spaces"
        venv.EnvBuilder(system_site_packages=True).create(environment)
        (self.folder / "_host_probe.py").write_text(textwrap.dedent(PROBE), encoding="utf-8")
        runtime = replace(
            launcher._runtime(),
            python=environment / "Scripts/pythonw.exe",
            package=self.folder,
            module="_host_probe",
        )
        host = self.folder / launcher.HOST_NAME
        launcher.write_host(host, runtime)
        output = self.folder / "result.json"
        arguments = ["Grüße 日本語", 'a "quoted" argument', "trailing\\", ""]
        result = subprocess.run([str(host), str(output), *arguments], timeout=20)
        self.assertEqual(result.returncode, 23)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(Path(report["image"]), host)
        self.assertEqual(Path(report["python"]), runtime.python)
        self.assertEqual(Path(report["prefix"]), environment)
        self.assertEqual(Path(report["base_prefix"]), Path(sys.base_prefix))
        self.assertEqual(report["arguments"], arguments)
        self.assertIsNone(report["launcher_environment"])
        self.assertEqual(report["child_exit"], 0)
        child = json.loads(output.with_suffix(".child.json").read_text())
        self.assertEqual(Path(child["python"]), runtime.python)
        self.assertEqual(Path(child["prefix"]), environment)

    def test_running_host_can_be_updated_and_previous_image_is_cleaned_after_exit(self):
        (self.folder / "_waiting_host.py").write_text(
            "import pathlib,sys,time\n"
            "ready=pathlib.Path(sys.argv[1]); ready.touch()\n"
            "while not ready.with_suffix('.stop').exists(): time.sleep(0.02)\n",
            encoding="utf-8",
        )
        runtime = replace(launcher._runtime(), package=self.folder, module="_waiting_host")
        host = self.folder / launcher.HOST_NAME
        launcher.write_host(host, runtime)
        ready = self.folder / "ready"
        process = subprocess.Popen([str(host), str(ready)])
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline, "Host did not start")
                time.sleep(0.02)
            updated = replace(runtime, build="the next host build")
            temporary = self.folder / "replacement.exe"
            launcher.write_host(temporary, updated)
            launcher._publish(temporary, host)
            self.assertEqual(launcher.read_host(host), updated)
            self.assertIsNone(process.poll())
            self.assertTrue(list(self.folder.glob("winlauncher-*.old.exe")))
            ready.with_suffix(".stop").touch()
            self.assertEqual(process.wait(timeout=10), 0)
            with patch.object(launcher, "_belongs_here", return_value=True):
                launcher._clean_previous(self.folder)
            self.assertFalse(list(self.folder.glob("winlauncher-*.old.exe")))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_executable_has_stable_winlauncher_metadata_and_icon(self):
        import win32api
        from PySide6.QtGui import QImage

        path = str(launcher.TEMPLATE.resolve())
        version = win32api.GetFileVersionInfo(path, "\\StringFileInfo\\040904b0\\FileDescription")
        self.assertEqual(version, "WinLauncher")
        self.assertEqual(
            win32api.GetFileVersionInfo(path, "\\StringFileInfo\\040904b0\\FileVersion"), "1.0.0"
        )
        module = win32api.LoadLibraryEx(path, 0, launcher.LOAD_LIBRARY_AS_DATAFILE)
        try:
            groups = win32api.EnumResourceNames(module, 14)
            self.assertEqual(len(groups), 1)
            group = win32api.LoadResource(module, 14, groups[0])
            sizes = set()
            for offset in range(6, len(group), 14):
                width, height, _, _, _, _, _, resource = struct.unpack_from(
                    "<BBBBHHIH", group, offset
                )
                dimensions = (width or 256, height or 256)
                sizes.add(dimensions)
                image = QImage.fromData(win32api.LoadResource(module, 3, resource))
                self.assertEqual((image.width(), image.height()), dimensions)
            self.assertEqual(
                sizes, {(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)}
            )
        finally:
            win32api.FreeLibrary(module)


@unittest.skipUnless(WINDOWS_X64, "The prebuilt host is for Windows x64")
class LoaderMigrationTest(unittest.TestCase):
    def test_user_path_preserves_entries_and_handles_expanded_existing_folder(self):
        import winreg

        folder = Path.home() / ".local" / "bin"
        registry = MagicMock()
        with (
            patch("winreg.CreateKey", return_value=registry),
            patch("winreg.QueryValueEx", return_value=("C:\\Existing", winreg.REG_EXPAND_SZ)),
            patch("winreg.SetValueEx") as write,
            patch("win32gui.SendMessageTimeout") as announce,
            patch.dict(os.environ, PATH="C:\\Existing"),
        ):
            launcher._add_to_user_path(folder)
            self.assertEqual(
                write.call_args.args[3:], (winreg.REG_EXPAND_SZ, str(folder) + ";C:\\Existing")
            )
            self.assertEqual(os.environ["PATH"], str(folder) + ";C:\\Existing")
            announce.assert_called_once()
        with (
            patch("winreg.CreateKey", return_value=registry),
            patch(
                "winreg.QueryValueEx",
                return_value=("%USERPROFILE%\\.local\\bin;C:\\Existing", winreg.REG_EXPAND_SZ),
            ),
            patch("winreg.SetValueEx") as write,
            patch("win32gui.SendMessageTimeout") as announce,
            patch.dict(os.environ, PATH=str(folder) + ";C:\\Existing"),
        ):
            launcher._add_to_user_path(folder)
            write.assert_not_called()
            announce.assert_not_called()

    def test_synchronous_update_process_failure_returns_and_keeps_reinstall_pending(self):
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication

        from winlauncher import __main__ as entry

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        process = MagicMock()
        process.state.return_value = QProcess.ProcessState.NotRunning
        process.error.return_value = QProcess.ProcessError.FailedToStart
        process.errorString.return_value = "Cannot start the Python interpreter"
        process.readAll.return_value.data.return_value = b""
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(entry, "QProcess", return_value=process) as process_class,
            patch.object(entry, "QEventLoop") as event_loop,
            patch.object(entry, "QTimer"),
            patch.object(entry, "show_message") as show,
        ):
            process_class.ProcessState = QProcess.ProcessState
            process_class.ProcessError = QProcess.ProcessError
            process_class.ProcessChannelMode = QProcess.ProcessChannelMode
            data = Path(temporary)
            (data / "reinstall-pending").write_text("1")
            entry._finish_update(PROJECT, data)
            event_loop.return_value.exec.assert_not_called()
            self.assertTrue((data / "reinstall-pending").exists())
            self.assertEqual(show.call_args.kwargs["details"], process.errorString())

    def test_legacy_start_finishes_update_migrates_autostart_and_restarts_in_host(self):
        import pywintypes
        import win32api
        import win32con
        import win32event

        from winlauncher.platform.startup import StartupShortcut
        from winlauncher.settings import Settings, SettingsFile

        with tempfile.TemporaryDirectory(prefix="winlauncher-migration-") as temporary:
            folder = Path(temporary).resolve()
            data, startup, command_bin = folder / "data", folder / "startup", folder / "bin"
            SettingsFile(data / "settings.json").save(
                Settings(roots=(), index_apps=False, index_windows_tools=False)
            )
            # Exercise the real QProcess update step without network or package changes.
            (data / "reinstall-pending").write_text("0", encoding="ascii")
            with patch.dict(os.environ, XDG_BIN_HOME=str(command_bin)):
                shortcut = StartupShortcut(folder / "custom autostart data", startup_folder=startup)
                shortcut.set_enabled(True)
            site = textwrap.dedent("""\
                import json, os, sys
                from pathlib import Path
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QApplication
                import win32api
                from winlauncher import install
                from winlauncher.platform import launcher, startup
                from winlauncher.ui.controller import LauncherController

                state = Path(os.environ["LOADER_TEST_STATE"])
                launcher._add_to_user_path = lambda folder: (state / "path-updated").touch()
                startup._user_startup_folder = lambda: state / "startup"
                startup._user_programs_folder = lambda: state / "programs"
                code = "import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({'python':sys.executable}))"
                install.reinstall_arguments = lambda: ["-c", code, str(state / "update.json")]
                original = LauncherController.__init__
                def initialize(self, *args, **kwargs):
                    original(self, *args, **kwargs)
                    index = len(list(state.glob("host-*.json")))
                    (state / f"host-{index}.json").write_text(json.dumps({
                        "image": win32api.GetModuleFileName(0), "python": sys.executable,
                        "prefix": sys.prefix, "argv": sys.argv, "pid": os.getpid(),
                    }))
                    QTimer.singleShot(150, self.restart if index == 0 else QApplication.instance().quit)
                LauncherController.__init__ = initialize
            """)
            (folder / "sitecustomize.py").write_text(site, encoding="utf-8")
            environment = dict(
                os.environ,
                QT_QPA_PLATFORM="offscreen",
                XDG_BIN_HOME=str(command_bin),
                LOADER_TEST_STATE=str(folder),
                PYTHONPATH=os.pathsep.join([str(folder), str(PROJECT)]),
            )
            process = subprocess.Popen(
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
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                    log_path = data / "winlauncher.log"
                    log = log_path.read_text(encoding="utf-8") if log_path.exists() else "No log"
                    self.fail("Legacy start did not exit:\n" + stdout + stderr + log)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                deadline = time.monotonic() + 30
                while not (folder / "host-1.json").exists() or (data / "instance.lock").exists():
                    if time.monotonic() > deadline:
                        log = (data / "winlauncher.log").read_text(encoding="utf-8")
                        self.fail("Migration/restart did not finish:\n" + stdout + stderr + log)
                    time.sleep(0.05)
                for index in range(2):
                    report = json.loads((folder / f"host-{index}.json").read_text())
                    self.assertEqual(Path(report["image"]), command_bin / launcher.HOST_NAME)
                    self.assertEqual(Path(report["python"]), launcher.windowless_python())
                    self.assertEqual(Path(report["prefix"]), Path(sys.prefix))
                    self.assertIn("--no-hooks", report["argv"])
                update = json.loads((folder / "update.json").read_text())
                self.assertEqual(Path(update["python"]), Path(sys.executable))
                self.assertFalse((data / "reinstall-pending").exists())
                self.assertTrue((folder / "path-updated").exists())
                import pythoncom

                from winlauncher.platform.shortcuts import load_shortcut

                pythoncom.CoInitialize()
                try:
                    link = load_shortcut(startup / "WinLauncher.lnk")
                    self.assertEqual(Path(link.GetPath(0)[0]), command_bin / launcher.HOST_NAME)
                    self.assertIn(
                        '"' + str(folder / "custom autostart data") + '"', link.GetArguments()
                    )
                    self.assertNotIn("-m winlauncher", link.GetArguments())
                    del link
                    link = load_shortcut(folder / "programs" / "WinLauncher.lnk")
                    self.assertEqual(Path(link.GetPath(0)[0]), command_bin / launcher.HOST_NAME)
                    self.assertEqual(link.GetArguments(), "--show")
                    del link
                finally:
                    pythoncom.CoUninitialize()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)
                # The instance lock is released before Windows releases the host image.
                for report_path in folder.glob("host-*.json"):
                    pid = json.loads(report_path.read_text())["pid"]
                    try:
                        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
                    except pywintypes.error as exc:
                        if exc.winerror != 87:  # A process that already exited has no handle.
                            raise
                        continue
                    try:
                        self.assertEqual(
                            win32event.WaitForSingleObject(handle, HOST_EXIT_TIMEOUT_MS),
                            win32event.WAIT_OBJECT_0,
                        )
                    finally:
                        handle.Close()


if __name__ == "__main__":
    unittest.main()
