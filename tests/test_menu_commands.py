"""Own context-menu commands: command lines, settings, the editor and how they run."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from winlauncher.actions import ItemActions
from winlauncher.commands import DEFAULT_MENU_COMMANDS, MenuCommand
from winlauncher.platform import shell
from winlauncher.settings import Settings, SettingsFile, settings_from_json
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

    def test_invalid_commands_are_rejected(self):
        for arguments in (
            ("", "cmd.exe"),
            ("Title", "  "),
            ("Title", "cmd.exe\n/k"),
            ("Title", "cmd.exe", False, False),
            ("Title", "cmd.exe", "yes", False),
            (None, "cmd.exe"),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                MenuCommand(*arguments)

    def test_settings_roundtrip_and_defaults(self):
        settings = Settings(roots=(), menu_commands=(TERMINAL, EDITOR))
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
        dialog.menu_commands.remove_selected()
        self.assertEqual(dialog.read_settings().menu_commands, ())
        self.assertFalse(dialog.menu_commands.remove.isEnabled())
        dialog.menu_commands.add_command()
        with patch("winlauncher.ui.settings.QMessageBox.warning") as warning:
            dialog.save()
        warning.assert_called_once()
        self.assertIn("Command 1", warning.call_args.args[2])
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
