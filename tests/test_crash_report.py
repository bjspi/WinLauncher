"""Native crashes reach the log at the next start; survived faults leave nothing behind."""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from winlauncher.crash_report import CRASH_FILE, CrashRecorder
from winlauncher.platform.startup_timing import LAST_PHASE, PROCESS_PHASE, StartupTimings

PROJECT = Path(__file__).resolve().parents[1]
# A real access violation on a worker thread, where plugins run. Not `faulthandler._sigsegv()`:
# it raises a signal, and Windows keeps those handlers per thread, so it records nothing.
CRASHING_RUN = """
import ctypes, pathlib, sys, threading
from winlauncher.crash_report import CrashRecorder

def plugin_code_that_crashes():
    ctypes.c_int.from_address(0).value

CrashRecorder(pathlib.Path(sys.argv[1])).start()
worker = threading.Thread(target=plugin_code_that_crashes)
worker.start()
worker.join()
"""


class CrashRecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.recorder = CrashRecorder(self.data)
        self.addCleanup(self.recorder.stop)

    def test_the_next_start_logs_the_stack_of_a_real_crash(self) -> None:
        crashed = subprocess.run(
            [sys.executable, "-c", CRASHING_RUN, str(self.data)],
            cwd=PROJECT,
            capture_output=True,
        )
        self.assertNotEqual(crashed.returncode, 0)
        with self.assertLogs("winlauncher.crash_report", logging.ERROR) as logs:
            self.recorder.start()
        self.assertIn("without a clean exit", logs.output[0])
        self.assertIn("plugin_code_that_crashes", logs.output[0])

    def test_a_reported_crash_is_reported_once(self) -> None:
        (self.data / CRASH_FILE).write_text("Fatal Python error", encoding="utf-8")
        with self.assertLogs("winlauncher.crash_report", logging.ERROR):
            self.recorder.start()
        self.recorder.stop()
        with self.assertNoLogs("winlauncher.crash_report", logging.ERROR):
            self.recorder.start()

    def test_a_clean_exit_forgets_what_the_run_survived(self) -> None:
        self.recorder.start()
        (self.data / CRASH_FILE).write_text("A fault Windows handled", encoding="utf-8")
        self.recorder.stop()
        self.assertFalse((self.data / CRASH_FILE).exists())

    def test_an_unusable_crash_file_never_stops_the_start(self) -> None:
        (self.data / CRASH_FILE).mkdir()
        with self.assertLogs("winlauncher.crash_report", logging.WARNING):
            self.recorder.start()
        self.recorder.stop()
        self.assertTrue((self.data / CRASH_FILE).is_dir())


class StartupTimingsTest(unittest.TestCase):
    def test_the_report_names_every_phase_from_process_creation_on(self) -> None:
        timings = StartupTimings()
        timings.mark("plugins")
        with self.assertLogs("winlauncher.platform.startup_timing", logging.INFO) as logs:
            timings.report()
        line = logs.output[0]
        for field in (PROCESS_PHASE, "plugins", LAST_PHASE, "total", "uptime"):
            self.assertIn(f"{field}=", line)


if __name__ == "__main__":
    unittest.main()
