"""A dead server or a cloud placeholder never holds WinLauncher for long."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from winlauncher.actions import ItemActions
from winlauncher.catalog import storage
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.search.ranking import SearchHit
from winlauncher.ui import icons, search_session

SHARE_FILE = Path(r"\\sleeping-nas\share\Reports\q3.xlsx")
PROBE_TIMEOUT_S = 0.05


class DeadServerTest(unittest.TestCase):
    """The probe stands in for a server that never answers until the test ends."""

    def setUp(self) -> None:
        released = threading.Event()
        self.addCleanup(released.set)

        def hanging(root: str) -> bool:
            if root.startswith("\\\\"):
                released.wait()
            return os.path.isdir(root)

        probe = patch.object(storage, "_DRIVES", storage.DriveProbe(PROBE_TIMEOUT_S, hanging))
        probe.start()
        self.addCleanup(probe.stop)

    def test_a_dead_server_is_waited_for_once_then_answered_at_once(self) -> None:
        started = time.monotonic()
        self.assertIs(storage.drive_state(SHARE_FILE), storage.DriveState.UNRESPONSIVE)
        self.assertGreaterEqual(time.monotonic() - started, PROBE_TIMEOUT_S * 0.9)
        # Its check still hangs, so the next question needs no new wait.
        started = time.monotonic()
        self.assertIs(storage.drive_state(SHARE_FILE.parent), storage.DriveState.UNRESPONSIVE)
        self.assertLess(time.monotonic() - started, PROBE_TIMEOUT_S / 2)
        self.assertIs(storage.drive_state(Path(tempfile.gettempdir())), storage.DriveState.PRESENT)
        with self.assertRaisesRegex(storage.DriveUnavailable, "does not respond"):
            storage.require_drive(SHARE_FILE)

    def test_files_on_a_dead_server_are_not_shown_as_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            deleted = Path(folder) / "deleted.txt"
            hits = [
                SearchHit(CatalogItem(SHARE_FILE, ItemKind.FILE), 100, "Fixture"),
                SearchHit(CatalogItem(deleted, ItemKind.FILE), 90, "Fixture"),
            ]
            self.assertEqual(search_session._missing_files(hits), {deleted})

    def test_actions_on_a_dead_server_fail_without_reaching_the_shell(self) -> None:
        actions = ItemActions(Mock())
        with patch("winlauncher.platform.shell.open_path") as open_path:
            with self.assertRaisesRegex(storage.DriveUnavailable, "does not respond"):
                actions.open(SHARE_FILE, "q3")
            open_path.assert_not_called()
        actions.store.record_launch.assert_not_called()

    def test_icons_of_programs_on_a_dead_server_come_from_their_extension(self) -> None:
        self.assertFalse(icons._reads_locally(str(SHARE_FILE.with_name("tool.exe"))))


class CloudPlaceholderTest(unittest.TestCase):
    def test_online_only_files_keep_their_own_icon_unread(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "tool.exe"
            program.touch()
            self.assertTrue(icons._reads_locally(str(program)))
            info = Mock(st_mode=program.stat().st_mode)
            for attribute in (
                storage.FILE_ATTRIBUTE_OFFLINE,
                storage.FILE_ATTRIBUTE_RECALL_ON_OPEN,
                storage.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            ):
                info.st_file_attributes = attribute
                with self.subTest(attribute=hex(attribute)):
                    self.assertTrue(storage.is_online_only(info))
                    with patch.object(icons.long_paths, "stat", return_value=info):
                        self.assertFalse(icons._reads_locally(str(program)))
            # A folder is only listed, so even an online-only one shows its own icon.
            info = Mock(st_mode=Path(folder).stat().st_mode, st_file_attributes=attribute)
            with patch.object(icons.long_paths, "stat", return_value=info):
                self.assertTrue(icons._reads_locally(folder))


if __name__ == "__main__":
    unittest.main()
