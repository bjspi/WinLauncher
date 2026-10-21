"""Windows tools and settings become shortcuts named in the display language."""

from __future__ import annotations

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from winlauncher.platform import shell, windows_tools
from winlauncher.ui import icons


def _utf16(*texts: str) -> bytes:
    return b"\x00\x00".join(text.encode("utf-16-le") for text in texts)


def _index(*entries: str) -> ET.Element:
    return ET.fromstring(f"<PCSettings>{''.join(entries)}</PCSettings>")


def _entry(page: str, description: str, *, paths: bool = False, file_name: str = "") -> str:
    identity = (
        f"<SettingPaths><Path><PageID>{page}</PageID></Path></SettingPaths>"
        if paths
        else f"<PageID>{page}</PageID>"
    )
    return (
        f"<SearchableContent><Filename>{file_name}</Filename>"
        f"<SettingIdentity>{identity}</SettingIdentity>"
        f"<SettingInformation><Description>{description}</Description></SettingInformation>"
        "</SearchableContent>"
    )


def _console(visual: str, strings: str, nodes: str = "") -> bytes:
    return (
        "<?xml version='1.0'?><MMC_ConsoleFile>"
        f"<VisualAttributes>{visual}</VisualAttributes>"
        f"<ScopeTree><Nodes>{nodes}</Nodes></ScopeTree>"
        f"<StringTables><StringTable><Strings>{strings}</Strings></StringTable></StringTables>"
        "</MMC_ConsoleFile>"
    ).encode()


class SettingsIndexTest(unittest.TestCase):
    def test_links_pair_each_page_id_with_the_link_after_it(self):
        library = _utf16(
            "SettingsPageDisplay",
            "ms-settings:display",
            "ms-settings:screenrotation",
            "{025A5937-A6BE-4686-A844-36FE4BEC8B6D}",
            "ms-settings:powersleep",
            "SettingsPageDisplay",
            "ms-settings:later",
            "Some other text",
            "ms-settings:unrelated",
        )
        self.assertEqual(
            windows_tools.settings_links(library),
            {
                "SettingsPageDisplay": "ms-settings:display",
                "{025A5937-A6BE-4686-A844-36FE4BEC8B6D}": "ms-settings:powersleep",
            },
        )

    def test_entries_take_their_title_from_windows_and_need_a_linked_page(self):
        index = _index(
            _entry("SettingsPageDisplay", "@display/title"),
            _entry("SettingsPageMouse", "@mouse/title", paths=True),
            _entry("SettingsPageUnknown", "@unknown/title"),
            _entry("SettingsPageDisplay", "@missing"),
            _entry("SettingsPageDisplay", "@sentence"),
        )
        titles = {
            "@display/title": "Bildschirmauflösung ändern",
            "@mouse/title": "Mauszeiger",
            "@unknown/title": "Nirgends",
            "@sentence": "x" * (windows_tools.MAX_SETTING_TITLE + 1),
        }
        links = {
            "SettingsPageDisplay": "ms-settings:display",
            "SettingsPageMouse": "ms-settings:mouse",
        }

        entries = list(windows_tools.settings_entries([index], links, titles.get))

        self.assertEqual(
            entries,
            [
                ("Bildschirmauflösung ändern", "ms-settings:display"),
                ("Mauszeiger", "ms-settings:mouse"),
            ],
        )

    def test_pages_only_keeps_the_entries_windows_names_after_their_page(self):
        index = _index(
            _entry("SettingsPageMouse", "@page", file_name="AAA_SettingsPageMouse"),
            _entry("SettingsPageMouse", "@variant", file_name="AAA_SettingsPageMouse-2"),
            _entry("SettingsPageMouse", "@setting", file_name="Mouse_PointerSize"),
            _entry("SettingsPageMouse", "@other", file_name="AAA_SettingsPageMouseExtra"),
        )
        titles = {"@page": "Maus", "@variant": "Mauseinstellungen", "@setting": "Zeigergröße"}
        titles["@other"] = "Andere Seite"
        links = {"SettingsPageMouse": "ms-settings:mousetouchpad"}

        entries = windows_tools.settings_entries([index], links, titles.get, pages_only=True)

        self.assertEqual([title for title, _ in entries], ["Maus", "Mauseinstellungen"])


class ConsoleTitleTest(unittest.TestCase):
    def test_the_window_title_names_the_console(self):
        console = _console(
            '<String Name="ApplicationTitle" ID="3"/>',
            '<String ID="1" Refs="2">Konsolenstamm</String><String ID="3" Refs="1">Dienste</String>',
        )
        self.assertEqual(windows_tools.console_title(console), "Dienste")

    def test_without_a_window_title_the_opened_snap_in_names_it(self):
        nodes = (
            '<Node ID="1"><Nodes><Node ID="2"><String Name="Name" ID="5"/></Node></Nodes>'
            '<String Name="Name" ID="4"/></Node>'
        )
        strings = '<String ID="4">Konsolenstamm</String><String ID="5">WMI-Kontrolle</String>'
        for opened in ("1", "2"):  # The console root or the snap-in itself.
            with self.subTest(opened=opened):
                visual = f'<BookMark Name="RootNode" NodeID="{opened}"/>'
                console = _console("", strings, nodes).replace(
                    b"</VisualAttributes>",
                    b"</VisualAttributes><Views>" + visual.encode() + b"</Views>",
                )
                self.assertEqual(windows_tools.console_title(console), "WMI-Kontrolle")

    def test_unreadable_consoles_have_no_title(self):
        self.assertIsNone(windows_tools.console_title(b"\x00binary"))


class WindowsToolsFolderTest(unittest.TestCase):
    def test_the_folder_is_an_application_root_only_when_included(self):
        with tempfile.TemporaryDirectory(prefix="winlauncher-tools-") as base:
            with patch.dict(os.environ, LOCALAPPDATA=base):
                folder = Path(base) / windows_tools.FOLDER
                folder.mkdir(parents=True)
                self.assertNotIn(folder, shell.application_roots())
                self.assertIn(folder, shell.application_roots(windows_tools=True))
                self.assertEqual(shell.application_roots(apps=False, windows_tools=True), (folder,))


def _stub_kind(*titles: str):
    return lambda *_: [(title, lambda path: path.write_text("stub")) for title in titles]


class EnglishNamesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-tools-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def sync(self, folder: Path, known=None, **kinds) -> dict[str, set[str]]:
        stubs = {f"_{name}": _stub_kind(*titles) for name, titles in kinds.items()}
        with patch.multiple(windows_tools, **stubs):
            return windows_tools._sync_kinds(folder, [], True, known=known)

    def names(self, folder: Path) -> list[str]:
        return sorted(path.stem for path in folder.iterdir())

    def test_english_names_leave_out_those_the_display_language_already_has(self):
        localized = self.sync(
            self.folder,
            control_panel=("Energieoptionen", "SupportAssist"),
            settings_pages=("Mauszeiger",),
            tools=("Dienste (services.msc)",),
        )
        english = self.folder / windows_tools.ENGLISH_FOLDER
        self.sync(
            english,
            known=localized,
            control_panel=("Power Options", "SupportAssist"),
            settings_pages=("Mouse pointer",),
            tools=("Services (services.msc)",),
        )

        self.assertEqual(self.names(english / windows_tools.CONTROL_PANEL), ["Power Options"])
        self.assertEqual(self.names(english / windows_tools.SETTINGS), ["Mouse pointer"])
        self.assertEqual(self.names(english / windows_tools.TOOLS), ["Services (services.msc)"])

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_turning_english_names_off_removes_them(self):
        english = self.folder / windows_tools.ENGLISH_FOLDER / windows_tools.TOOLS
        english.mkdir(parents=True)
        with patch.object(windows_tools, "_sync_kinds", return_value={}):
            windows_tools.sync_windows_tools(self.folder, english=False)
        self.assertFalse(english.parent.exists())


@unittest.skipUnless(os.name == "nt", "Windows shell")
class WindowsToolShortcutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-tools-")
        cls.addClassCleanup(temporary.cleanup)
        cls.folder = Path(temporary.name)
        windows_tools.sync_windows_tools(cls.folder)

    def shortcuts(self, kind: str) -> list[Path]:
        return sorted((self.folder / kind).iterdir())

    def test_every_kind_gets_shortcuts(self):
        for kind in (windows_tools.CONTROL_PANEL, windows_tools.SETTINGS, windows_tools.TOOLS):
            with self.subTest(kind=kind):
                self.assertGreater(len(self.shortcuts(kind)), 10)

    def test_tools_keep_their_file_name_beside_the_display_name(self):
        names = [path.stem for path in self.shortcuts(windows_tools.TOOLS)]
        for file_name in ("(services.msc)", "(taskmgr)", "(regedit)"):
            with self.subTest(file_name=file_name):
                self.assertTrue(any(name.endswith(file_name) for name in names), names)

    def test_shortcuts_show_an_icon(self):
        for kind in (windows_tools.CONTROL_PANEL, windows_tools.SETTINGS, windows_tools.TOOLS):
            path = self.shortcuts(kind)[0]
            with self.subTest(shortcut=path.name):
                image = icons._image_from_icon(icons._shell_icon(str(path), True))
                self.assertFalse(image.isNull())


if __name__ == "__main__":
    unittest.main()
