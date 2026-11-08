"""Own context-menu commands: command lines, settings, the editor and how they run."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from winlauncher.actions import ItemActions
from winlauncher.commands import DEFAULT_MENU_COMMANDS, MenuCommand, shortcut_clash
from winlauncher.platform import shell
from winlauncher.search.commands import search_commands
from winlauncher.search.query_parser import parse_query
from winlauncher.settings import Settings, SettingsFile, settings_from_json
from winlauncher.ui.commands import CommandList, command_icon
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.website_icons import WebsiteIcons

TERMINAL = MenuCommand("Terminal", 'wt.exe -d "{folder}"')
EDITOR = MenuCommand("Editor", 'code "{path}"', folders=True, files=True)


class MenuCommandTest(unittest.TestCase):
    def test_placeholders_name_the_result_and_its_folder(self):
        folder = Path("D:/Work/A&B")
        file = folder / "notes {name}.txt"
        command = MenuCommand("All", 'tool "{path}" "{folder}" {name} {other}', files=True)
        self.assertEqual(
            command.command_line(file, folder),
            f'tool "{file}" "{folder}" notes {{name}}.txt {{other}}',
        )
        self.assertEqual(
            TERMINAL.command_line(folder, folder), f'wt.exe -d "{Path("D:/Work/A&B")}"'
        )

    def test_a_drive_root_never_ends_in_a_backslash_before_a_quote(self):
        root = Path("D:/")
        self.assertEqual(TERMINAL.command_line(root, root), 'wt.exe -d "D:\\."')

    def test_environment_variables_expand_but_not_inside_paths(self):
        with patch.dict(os.environ, {"WL_TOOLS": "C:\\Tools"}):
            command = MenuCommand("Tool", '"%WL_TOOLS%\\run.exe" "{path}" %WL_MISSING% 100%')
            folder = Path("D:/%WL_TOOLS%")
            self.assertEqual(
                command.command_line(folder, folder),
                f'"C:\\Tools\\run.exe" "{folder}" %WL_MISSING% 100%',
            )

    def test_the_icon_is_the_chosen_file_or_the_programs(self):
        with patch.dict(os.environ, {"WL_ICONS": "C:\\Icons"}):
            chosen = MenuCommand("Tool", "cmd.exe", icon=' "%WL_ICONS%\\claude.ico" ')
            self.assertEqual(chosen.icon_file(), Path("C:/Icons/claude.ico"))
        with patch("winlauncher.commands.shutil.which", return_value="C:\\Tools\\wt.exe") as which:
            self.assertEqual(TERMINAL.icon_file(), Path("C:/Tools/wt.exe"))
            quoted = MenuCommand("Quoted", '"C:\\Program Files\\Tool\\tool.exe" "{path}"')
            quoted.icon_file()
        self.assertEqual(
            [call.args[0] for call in which.call_args_list],
            ["wt.exe", "C:\\Program Files\\Tool\\tool.exe"],
        )
        with patch("winlauncher.commands.shutil.which", return_value=None):
            self.assertIsNone(TERMINAL.icon_file())

    def test_invalid_commands_are_rejected(self):
        for arguments in (
            ("", "cmd.exe"),
            ("Title", "  "),
            ("Title", "cmd.exe\n/k"),
            ("Title", "cmd.exe", False, False),
            ("Title", "cmd.exe", "yes", False),
            (None, "cmd.exe"),
            ("Title", "cmd.exe", True, False, "a.ico\nb"),
            ("Title", "cmd.exe", True, False, None),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                MenuCommand(*arguments)

    def test_settings_roundtrip_and_defaults(self):
        with_icon = MenuCommand("Claude", 'wt.exe -d "{folder}" cmd /k claude', icon="C:\\c.ico")
        settings = Settings(roots=(), menu_commands=(TERMINAL, EDITOR, with_icon))
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)
        self.assertEqual(settings_from_json({"roots": []}).menu_commands, DEFAULT_MENU_COMMANDS)
        self.assertEqual(settings_from_json({"menu_commands": []}).menu_commands, ())
        self.assertEqual(
            [command.title for command in DEFAULT_MENU_COMMANDS],
            ["Open in Windows Terminal", "Open Command Prompt here"],
        )
        self.assertTrue(all(c.folders and not c.files for c in DEFAULT_MENU_COMMANDS))
        folder = Path("D:/A B")
        self.assertEqual(
            [c.command_line(folder, folder) for c in DEFAULT_MENU_COMMANDS],
            [f'wt.exe -d "{folder}"', f'cmd.exe /k pushd "{folder}"'],
        )
        for value in (
            None,
            ["cmd.exe"],
            [{"title": "X"}],
            [{"title": "X", "command": "cmd.exe", "folders": False, "files": False}],
            [{"title": "X", "command": "cmd.exe", "shell": True}],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_from_json({"menu_commands": value})

    def test_explorer_settings_roundtrip_and_validation(self):
        tool = replace(TERMINAL, explorer_search=True, search_keywords=("wt", "terminal"))
        settings = Settings(
            roots=(), menu_commands=(tool,), explorer_tools_first=False, explorer_tools_fuzzy=True
        )
        with tempfile.TemporaryDirectory() as directory:
            file = SettingsFile(Path(directory) / "settings.json")
            file.save(settings)
            self.assertEqual(file.load(), settings)
        loaded = settings_from_json({"menu_commands": [{"title": "Terminal", "command": "wt"}]})
        self.assertFalse(loaded.menu_commands[0].explorer_search)
        self.assertEqual(loaded.menu_commands[0].search_keywords, ())
        self.assertTrue(loaded.explorer_tools_first)
        self.assertFalse(loaded.explorer_tools_fuzzy)
        for changes in (
            {"explorer_search": "yes"},
            {"explorer_search": True, "folders": False, "files": True},
            {"search_keywords": "wt"},
            {"search_keywords": [None]},
            {"search_keywords": [" "]},
            {"search_keywords": ["wt\nterminal"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(TERMINAL, **changes)
        with self.assertRaises(ValueError):
            settings_from_json({"explorer_tools_first": "yes"})
        with self.assertRaises(ValueError):
            settings_from_json({"explorer_tools_fuzzy": "yes"})
        loaded = settings_from_json(
            {
                "menu_commands": [
                    {
                        "title": "Terminal",
                        "command": "wt",
                        "explorer_search": True,
                        "search_keywords": [" wt ", "terminal"],
                    }
                ],
            }
        )
        self.assertEqual(loaded.menu_commands[0].search_keywords, ("wt", "terminal"))
        self.assertIsInstance(hash(loaded.menu_commands[0]), int)

    def test_explorer_tools_match_titles_aliases_and_all_words(self):
        folder = Path("D:/A folder containing unmatchedword")
        terminal = replace(TERMINAL, explorer_search=True, search_keywords=("wt",))
        claude = MenuCommand(
            "Claude Code", 'wt.exe -d "{folder}" cmd /k claude', explorer_search=True
        )
        commands = (TERMINAL, terminal, claude)
        for text, expected in (
            ("wt", [terminal]),
            ("TERMINAL", [terminal]),
            ("wt term", [terminal]),
            ("clde", []),
            ("claude code", [claude]),
            ("claude unmatchedword", []),
            ("unmatchedword", []),
            ("", []),
        ):
            with self.subTest(text=text):
                hits = search_commands(commands, parse_query(text), folder)
                self.assertEqual([hit.command for hit in hits], expected)
                self.assertTrue(all(hit.folder == folder for hit in hits))
        accented = MenuCommand("Öffnen", "x", explorer_search=True)
        self.assertEqual(
            search_commands((accented,), parse_query("offnen"), folder)[0].command, accented
        )

    def test_explorer_tools_rank_exact_aliases_and_keep_ties_in_configuration_order(self):
        folder = Path("D:/Project")
        fuzzy = MenuCommand("Wartung", "a", explorer_search=True)
        alias = MenuCommand("Terminal", "b", explorer_search=True, search_keywords=("wt",))
        duplicate = replace(alias, command="c")
        hits = search_commands((fuzzy, alias, duplicate), parse_query("wt"), folder, fuzzy=True)
        self.assertEqual([hit.command for hit in hits], [alias, duplicate, fuzzy])
        self.assertEqual([hit.position for hit in hits], [1, 2, 0])

    def test_tools_require_contiguous_title_or_alias_matches_unless_fuzzy_is_enabled(self):
        folder = Path("D:/Project")
        claude = MenuCommand(
            "Open in Claude", "claude", explorer_search=True, search_keywords=("assistant",)
        )
        for text, matches in (
            ("Cla", True),
            ("CLAUDE", True),
            ("laud", True),
            ("cla ass", True),
            ("cud", False),
            ("oic", False),
            ("asst", False),
            ("cla unknown", False),
        ):
            with self.subTest(text=text):
                hits = search_commands((claude,), parse_query(text), folder)
                self.assertEqual(bool(hits), matches)
        for text in ("cud", "oic", "asst"):
            with self.subTest(fuzzy_text=text):
                hits = search_commands((claude,), parse_query(text), folder, fuzzy=True)
                self.assertEqual([hit.command for hit in hits], [claude])


class MenuCommandRunTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-commands-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "Project"
        self.folder.mkdir()
        self.file = self.folder / "notes.txt"
        self.file.write_text("fixture")

    def test_commands_start_in_the_folder_or_the_files_folder(self):
        actions = ItemActions(Mock())
        with patch("winlauncher.actions.shell.run_command") as run:
            actions.run_command(EDITOR, self.folder)
            actions.run_command(EDITOR, self.file)
        self.assertEqual(
            [call.args for call in run.call_args_list],
            [
                (f'code "{self.folder}"', self.folder),
                (f'code "{self.file}"', self.folder),
            ],
        )
        actions.store.record_launch.assert_not_called()

    def test_explorer_commands_keep_the_captured_folder_and_never_use_its_parent(self):
        actions = ItemActions(Mock())
        command = MenuCommand("All", 'tool "{path}" "{folder}" "{name}"')
        missing = self.folder / "gone {name} & spaces"
        with patch("winlauncher.actions.shell.run_command") as run:
            actions.run_folder_command(command, missing)
        run.assert_called_once_with(f'tool "{missing}" "{missing}" "{missing.name}"', missing)
        actions.store.record_launch.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows processes")
    def test_missing_explorer_folder_is_reported_without_starting_a_process(self):
        with patch("winlauncher.platform.shell.subprocess.Popen") as popen:
            with self.assertRaises(FileNotFoundError):
                ItemActions(Mock()).run_folder_command(TERMINAL, self.folder / "missing")
        popen.assert_not_called()

    def test_open_with_a_missing_file_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            shell.open_with(self.folder / "missing.txt")

    @unittest.skipUnless(os.name == "nt", "Windows consoles")
    def test_console_programs_get_their_own_console(self):
        with patch("winlauncher.platform.shell.subprocess.Popen") as popen:
            shell.run_command("cmd.exe", self.folder)
        popen.assert_called_once_with(
            "cmd.exe", cwd=self.folder, creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        with self.assertRaises(FileNotFoundError):
            shell.run_command("cmd.exe", self.folder / "missing")
        with self.assertRaisesRegex(OSError, "could not run no-such-program.exe"):
            shell.run_command("no-such-program.exe --flag", self.folder)

    @unittest.skipUnless(os.name == "nt", "Windows processes")
    def test_a_real_command_runs_in_the_folder(self):
        output = self.folder / "cwd.txt"
        command = MenuCommand("Where", f'cmd.exe /d /c cd > "{output.name}"')
        with patch("winlauncher.platform.shell.subprocess.Popen") as popen:
            ItemActions(Mock()).run_command(command, self.file)
        line, folder = popen.call_args.args[0], popen.call_args.kwargs["cwd"]
        # Without a new console, so the test shows no window.
        subprocess.run(line, cwd=folder, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        self.assertEqual(output.read_text().strip(), str(self.folder))


class MenuCommandUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.icons = WebsiteIcons()
        self.addCleanup(self.icons.close)

    def test_settings_edit_add_remove_save_and_cancel(self):
        original = Settings(roots=(), menu_commands=(TERMINAL,))
        saved = []
        dialog = SettingsDialog(original, save=saved.append, website_icons=self.icons)
        editor = dialog.menu_commands
        self.assertEqual(editor.show_for.currentText(), "Folders")
        editor.title.setText("Windows Terminal")
        editor.add_command()
        editor.title.setText(EDITOR.title)
        editor.command.setText(EDITOR.command)
        editor.show_for.setCurrentText("Folders and files")
        self.assertEqual(
            dialog.read_settings().menu_commands,
            (MenuCommand("Windows Terminal", TERMINAL.command), EDITOR),
        )
        self.assertIn("Editor", editor.list.item(1).text())
        editor.list.setCurrentRow(1)
        self.assertEqual(editor.show_for.currentText(), "Folders and files")
        dialog.reject()
        self.assertEqual(saved, [])
        dialog.deleteLater()
        dialog = SettingsDialog(original, save=saved.append, website_icons=self.icons)
        dialog.menu_commands.command.setText("cmd.exe")
        dialog.save()
        self.assertEqual(saved[0].menu_commands, (MenuCommand("Terminal", "cmd.exe"),))
        dialog.deleteLater()
        dialog = SettingsDialog(original, website_icons=self.icons)
        question = "winlauncher.ui.commands.ask"
        with patch(question, return_value=False) as asked:
            dialog.menu_commands.remove_selected()
        self.assertIn("Terminal", asked.call_args.args[1])
        self.assertEqual(dialog.read_settings().menu_commands, (TERMINAL,))
        with patch(question, return_value=True):
            dialog.menu_commands.remove_selected()
        self.assertEqual(dialog.read_settings().menu_commands, ())
        self.assertFalse(dialog.menu_commands.remove.isEnabled())
        dialog.menu_commands.add_command()
        self.assertTrue(dialog.menu_commands.list.item(0).icon().isNull())  # Not valid yet.
        with patch("winlauncher.ui.settings.message") as warning:
            dialog.save()
        warning.assert_called_once()
        self.assertIn("Command 1", warning.call_args.args[2])
        dialog.deleteLater()

    def test_shortcuts_are_chosen_from_the_free_modifiers_and_never_shared(self):
        with self.assertRaises(ValueError):
            MenuCommand("Claude", "claude", shortcut="Ctrl")
        claude = MenuCommand("Claude", "claude", shortcut="Alt")
        files = MenuCommand("Viewer", "viewer", folders=False, files=True, shortcut="Alt")
        self.assertIsNone(shortcut_clash((claude, files)))
        self.assertIn(
            "'Claude' and 'Codex'", shortcut_clash((claude, replace(claude, title="Codex")))
        )
        loaded = settings_from_json({"menu_commands": [{"title": "C", "command": "c"}]})
        self.assertEqual(loaded.menu_commands[0].shortcut, "")
        dialog = SettingsDialog(
            Settings(roots=(), menu_commands=(claude, TERMINAL)), website_icons=self.icons
        )
        editor = dialog.menu_commands
        self.assertEqual(editor.shortcut.currentData(), "Alt")
        editor.list.setCurrentRow(1)
        self.assertEqual(editor.shortcut.currentData(), "")
        editor.shortcut.setCurrentIndex(editor.shortcut.findData("Alt"))
        with self.assertRaisesRegex(ValueError, "both use Alt for folders"):
            dialog.read_settings()
        editor.shortcut.setCurrentIndex(editor.shortcut.findData("Shift"))
        self.assertEqual(
            dialog.read_settings().menu_commands, (claude, replace(TERMINAL, shortcut="Shift"))
        )
        dialog.deleteLater()

    def test_duplicate_copies_the_selected_entry_below_it(self):
        claude = MenuCommand("Claude", 'wt.exe -d "{folder}" cmd /k claude', icon="C:\\c.ico")
        dialog = SettingsDialog(
            Settings(roots=(), menu_commands=(claude, TERMINAL)), website_icons=self.icons
        )
        editor = dialog.menu_commands
        editor.duplicate_selected()
        self.assertEqual(editor.list.currentRow(), 1)
        editor.title.setText("Codex")
        editor.command.setText('wt.exe -d "{folder}" cmd /k codex')
        self.assertEqual(
            dialog.read_settings().menu_commands,
            (
                claude,
                MenuCommand("Codex", 'wt.exe -d "{folder}" cmd /k codex', icon="C:\\c.ico"),
                TERMINAL,
            ),
        )
        editor.list.setCurrentRow(-1)
        self.assertFalse(editor.duplicate.isEnabled())
        dialog.deleteLater()

    def test_explorer_options_edit_duplicate_and_disable_for_file_only_commands(self):
        tool = replace(TERMINAL, explorer_search=True, search_keywords=("wt", "terminal"))
        dialog = SettingsDialog(Settings(roots=(), menu_commands=(tool,)), website_icons=self.icons)
        self.addCleanup(dialog.deleteLater)
        editor = dialog.menu_commands
        self.assertTrue(editor.explorer_search.isChecked())
        self.assertEqual(editor.search_keywords.text(), "wt, terminal")
        self.assertTrue(editor.search_keywords.isEnabled())
        editor.duplicate_selected()
        editor.title.setText("Codex")
        editor.search_keywords.setText(" codex, coding, , ")
        duplicate = dialog.read_settings().menu_commands[1]
        self.assertTrue(duplicate.explorer_search)
        self.assertEqual(duplicate.search_keywords, ("codex", "coding"))
        editor.show_for.setCurrentText("Files")
        self.assertFalse(editor.explorer_search.isChecked())
        self.assertFalse(editor.explorer_search.isEnabled())
        self.assertFalse(editor.search_keywords.isEnabled())
        self.assertFalse(dialog.read_settings().menu_commands[1].explorer_search)
        editor.list.setCurrentRow(0)
        self.assertTrue(editor.explorer_search.isChecked())
        self.assertEqual(editor.search_keywords.text(), "wt, terminal")
        editor.show_for.setCurrentText("Folders and files")
        editor.explorer_search.setChecked(False)
        self.assertFalse(editor.search_keywords.isEnabled())
        self.assertTrue(dialog.read_settings().explorer_tools_first)
        dialog.explorer_tool_order.setCurrentText("Files first")
        self.assertFalse(dialog.read_settings().explorer_tools_first)
        self.assertFalse(dialog.explorer_tools_fuzzy.isChecked())
        dialog.explorer_tools_fuzzy.setChecked(True)
        self.assertTrue(dialog.read_settings().explorer_tools_fuzzy)
        saved = dialog.read_settings()
        reopened = SettingsDialog(saved, website_icons=self.icons)
        self.addCleanup(reopened.deleteLater)
        self.assertTrue(reopened.explorer_tools_fuzzy.isChecked())

    def test_visible_command_switching_keeps_search_toggle_and_editability_in_sync(self):
        enabled = replace(TERMINAL, explorer_search=True)
        disabled = replace(TERMINAL, title="Other terminal")
        editor = CommandList((enabled, disabled))
        self.addCleanup(editor.deleteLater)
        editor.show()
        self.addCleanup(editor.hide)
        self.app.processEvents()
        self.assertTrue(editor.search_keywords.isEnabled())
        QTest.keyClicks(editor.search_keywords, "wt")
        self.assertEqual(editor.commands()[0].search_keywords, ("wt",))

        editor.list.setCurrentRow(1)
        self.assertFalse(editor.explorer_search.isChecked())
        self.assertEqual(editor.explorer_search.position, 0.0)
        self.assertFalse(editor.search_keywords.isEnabled())
        editor.list.setCurrentRow(0)
        self.assertTrue(editor.explorer_search.isChecked())
        self.assertEqual(editor.explorer_search.position, 1.0)
        self.assertTrue(editor.search_keywords.isEnabled())
        QTest.keyClicks(editor.search_keywords, ", terminal")
        self.assertEqual(editor.commands()[0].search_keywords, ("wt", "terminal"))

        # Changing entries during a user animation must not leave the old
        # animation painting an enabled switch over a disabled command.
        QTest.mouseClick(editor.explorer_search, Qt.MouseButton.LeftButton)
        QTest.qWait(200)
        QTest.mouseClick(editor.explorer_search, Qt.MouseButton.LeftButton)
        editor.list.setCurrentRow(1)
        QTest.qWait(200)
        self.assertEqual(editor.explorer_search.position, 0.0)
        self.assertFalse(editor.search_keywords.isEnabled())

    @unittest.skipUnless(os.name == "nt", "Windows clipboard shutdown")
    def test_copying_an_explorer_command_keeps_python_shutdown_clean(self):
        """Assertions can pass before Qt crashes during interpreter shutdown.

        Keep the copied command in the clipboard until process exit: replacing
        it during test cleanup would conceal the original ownership failure.
        """
        script = """
import ctypes
from pathlib import Path
from PySide6.QtWidgets import QApplication
from winlauncher.commands import MenuCommand
from winlauncher.search.commands import CommandHit
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.window import LauncherWindow

SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
app = QApplication([])
icons = WebsiteIcons()
window = LauncherWindow(icons)
command = MenuCommand("Terminal", 'wt.exe -d "{folder}"', explorer_search=True)
folder = Path.cwd()
window.set_results([CommandHit(command, folder, 0, 100)], 1)
assert window.copy_selected()
assert app.clipboard().text() == command.command_line(folder, folder)
assert not app.clipboard().mimeData().hasUrls()
window.close_focus_watch()
icons.close()
"""
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", script],
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_icons_come_from_the_chosen_file_or_the_program(self):
        with tempfile.TemporaryDirectory() as directory:
            chosen = Path(directory) / "tool.png"
            image = QImage(16, 16, QImage.Format.Format_ARGB32)
            image.fill(0xFF2277BB)
            image.save(str(chosen))
            original = Settings(roots=(), menu_commands=(TERMINAL,))
            dialog = SettingsDialog(original, website_icons=self.icons)
            editor = dialog.menu_commands
            editor.icon.setText(str(chosen))
            self.assertFalse(editor.list.item(0).icon().isNull())
            self.assertEqual(dialog.read_settings().menu_commands[0].icon, str(chosen))
            self.assertFalse(command_icon(dialog.read_settings().menu_commands[0]).isNull())
            dialog.deleteLater()
            webp = chosen.with_suffix(".webp")
            self.assertTrue(image.save(str(webp)))
            self.assertFalse(command_icon(MenuCommand("WebP", "x", icon=str(webp))).isNull())
        self.assertTrue(command_icon(MenuCommand("Gone", "x", icon=str(chosen))).isNull())
        missing = MenuCommand("Missing", "no-such-program-wl.exe")
        self.assertTrue(command_icon(missing).isNull())
        if os.name == "nt":
            self.assertFalse(command_icon(MenuCommand("Prompt", "cmd.exe")).isNull())


if __name__ == "__main__":
    unittest.main()
