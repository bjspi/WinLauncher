"""Store apps from Windows' "All apps" list become shortcuts the catalog indexes."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from winlauncher.platform import shell, store_apps
from winlauncher.ui import icons

STUB_SHORTCUT = b"shortcut"


def _write_stub(path: Path, item: str) -> None:
    path.write_bytes(STUB_SHORTCUT + item.encode())


class StoreAppShortcutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-store-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "Store apps"

    def sync(self, *apps: tuple[str, str]) -> None:
        with (
            patch.object(store_apps, "_store_apps", return_value=list(apps)),
            patch.object(store_apps, "_write_shortcut", side_effect=_write_stub),
        ):
            store_apps._sync(self.folder)

    def names(self) -> list[str]:
        return sorted(path.name for path in self.folder.iterdir())

    def test_names_keep_the_app_name_and_drop_forbidden_characters(self):
        self.assertEqual(store_apps.shortcut_name("Telegram Desktop"), "Telegram Desktop.lnk")
        self.assertEqual(store_apps.shortcut_name('Notes: "Work" / Home?'), "Notes Work Home.lnk")
        self.assertEqual(store_apps.shortcut_name("Version 2. "), "Version 2.lnk")
        self.assertEqual(store_apps.shortcut_name("???"), "App.lnk")

    def test_sync_adds_new_apps_keeps_known_ones_and_removes_uninstalled_ones(self):
        self.sync(("Telegram Desktop", "telegram"), ("Calculator", "calculator"))
        self.assertEqual(self.names(), ["Calculator.lnk", "Telegram Desktop.lnk"])
        kept = self.folder / "Telegram Desktop.lnk"
        written = kept.stat().st_mtime_ns

        self.sync(("Telegram Desktop", "telegram"), ("Photos", "photos"))

        self.assertEqual(self.names(), ["Photos.lnk", "Telegram Desktop.lnk"])
        self.assertEqual(kept.stat().st_mtime_ns, written)  # Unchanged apps change nothing.

    def test_apps_with_the_same_name_get_one_shortcut(self):
        self.sync(("Notes", "first"), ("NOTES", "second"))
        self.assertEqual(self.names(), ["Notes.lnk"])
        self.assertEqual((self.folder / "Notes.lnk").read_bytes(), STUB_SHORTCUT + b"first")

    def test_the_folder_is_an_application_root_once_it_exists(self):
        base = self.folder.parent
        with patch.dict(os.environ, LOCALAPPDATA=str(base)):
            shortcuts = base / store_apps.FOLDER
            self.assertNotIn(shortcuts, shell.application_roots())
            shortcuts.mkdir(parents=True)
            self.assertIn(shortcuts, shell.application_roots())


@unittest.skipUnless(os.name == "nt", "Windows shell")
class WindowsStoreAppTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-store-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_real_store_apps_become_shortcuts_with_their_own_icon(self):
        store_apps.sync_store_apps(self.folder)
        shortcuts = list(self.folder.iterdir())
        self.assertTrue(all(path.suffix == store_apps.SHORTCUT_SUFFIX for path in shortcuts))
        for path in shortcuts[:3]:  # Runners may have only a few Store apps, or none.
            with self.subTest(app=path.stem):
                image = icons._image_from_icon(icons._shell_icon(str(path), True))
                self.assertFalse(image.isNull())

    def test_shortcut_icons_come_from_the_target(self):
        import pythoncom
        from win32comext.shell import shell as win_shell

        shortcut = self.folder / "Python.lnk"
        link = pythoncom.CoCreateInstance(
            win_shell.CLSID_ShellLink,
            None,
            pythoncom.CLSCTX_INPROC_SERVER,
            win_shell.IID_IShellLink,
        )
        link.SetPath(sys.executable)
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(shortcut), True)
        del link
        with patch.object(icons, "_shortcut_icon", wraps=icons._shortcut_icon) as from_target:
            handle = icons._shell_icon(str(shortcut), True)
        self.assertTrue(handle)
        self.assertTrue(from_target.call_args)
        icons._image_from_icon(handle)  # Frees the handle.


if __name__ == "__main__":
    unittest.main()
