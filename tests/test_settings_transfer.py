"""Settings exported to a file and read back, here or on another computer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from winlauncher.settings import Settings, SettingsFile, read_exported_settings


class SettingsTransferTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_exported_settings_read_back_unchanged(self):
        exported = Settings(roots=("D:\\Projects",), max_results=25, appearance="dark")
        path = self.folder / "WinLauncher settings.json"
        SettingsFile(path).save(exported)
        self.assertEqual(read_exported_settings(path), exported)

    def test_a_file_that_is_no_settings_file_is_refused(self):
        for content, expected in (
            (b"\xff\xfe not text", "not a WinLauncher settings file"),
            (b"{broken", "not a WinLauncher settings file"),
            (json.dumps(["a"]).encode(), "cannot use"),
            (json.dumps({"max_results": "many"}).encode(), "cannot use"),
        ):
            path = self.folder / "other.json"
            path.write_bytes(content)
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                read_exported_settings(path)
        with self.assertRaises(OSError):
            read_exported_settings(self.folder / "missing.json")


if __name__ == "__main__":
    unittest.main()
