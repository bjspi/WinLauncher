"""Quick Switch for Open and Save dialogs, with every Win32 call mocked.

No real dialog is opened and no keystroke is injected.
"""

from __future__ import annotations

import os
import queue
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, call, patch

from pathscout.actions import ItemActions
from pathscout.platform.gesture import ExplorerTyping, FolderJumpRequested, ViewKind
from pathscout.settings import Settings, SettingsFile, settings_from_json

WINDOWS = os.name == "nt"


class TemporaryFolderTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pathscout-quick-switch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


@unittest.skipUnless(WINDOWS, "Windows file dialogs")
class DialogFolderTest(TemporaryFolderTest):
    def test_address_text_yields_the_path_after_any_localized_label(self):
        from pathscout.platform.file_dialog import folder_from_address

        cases = {
            r"Address: C:\Users\Anna\Documents": Path(r"C:\Users\Anna\Documents"),
            r"Adresse: D:\Daten\Projekte 2024": Path(r"D:\Daten\Projekte 2024"),
            "Adresse : C:\\": Path("C:\\"),
            r"Adres: \\server\share\Reports": Path(r"\\server\share\Reports"),
            r"Address: C:\Users\Björn\Übersicht  ": Path(r"C:\Users\Björn\Übersicht"),
        }
        for text, folder in cases.items():
            with self.subTest(text=text):
                self.assertEqual(folder_from_address(text), folder)

    def test_virtual_folders_have_no_address_path(self):
        from pathscout.platform.file_dialog import folder_from_address

        for text in ("Address: This PC", "Adresse: Dieser PC", r"Address: Libraries\Music", ""):
            with self.subTest(text=text):
                self.assertIsNone(folder_from_address(text))

    def test_dialog_folder_must_exist(self):
        from pathscout.platform import file_dialog

        missing = self.root / "gone"
        for text, expected in ((f"Adresse: {self.root}", self.root), (f"Address: {missing}", None)):
            with (
                patch.object(file_dialog, "_address_toolbar", return_value=7),
                patch.object(file_dialog, "window_text", return_value=text),
            ):
                self.assertEqual(file_dialog.dialog_folder(1), expected)
        with patch.object(file_dialog, "_address_toolbar", return_value=None):
            self.assertIsNone(file_dialog.dialog_folder(1))
        with (
            patch.object(file_dialog, "_address_toolbar", return_value=7),
            patch.object(file_dialog, "window_text", side_effect=OSError("hung")),
        ):
            self.assertIsNone(file_dialog.dialog_folder(1))


def _dialog(folder: Path | None, typed: str = "draft.txt") -> Mock:
    from pathscout.platform.file_dialog import FileDialog

    dialog = Mock(spec=FileDialog)
    dialog.hwnd = 1
    dialog.folder.return_value = folder
    dialog.file_name.return_value = typed
    dialog.exists.return_value = True
    return dialog


@unittest.skipUnless(WINDOWS, "Windows file dialogs")
class FileNameSequenceTest(TemporaryFolderTest):
    def test_folder_navigates_through_the_file_name_box_and_restores_the_typed_name(self):
        from pathscout.platform.file_dialog import open_folder

        target = self.root / "invoices"
        dialog = _dialog(self.root)
        open_folder(dialog, target)
        self.assertEqual(
            dialog.mock_calls,
            [
                call.file_name(),
                call.folder(),
                call.set_file_name(str(target)),
                call.confirm(),
                call.wait_for_folder(target),
                call.exists(),
                call.set_file_name("draft.txt"),
                call.activate(),
            ],
        )

    def test_file_shows_its_folder_and_enters_its_name_without_confirming(self):
        from pathscout.platform.file_dialog import choose_file

        report = self.root / "invoices" / "report.pdf"
        dialog = _dialog(self.root)
        choose_file(dialog, report)
        self.assertEqual(dialog.confirm.call_count, 1)  # Only to navigate to the folder.
        self.assertEqual(
            dialog.mock_calls[-3:],
            [call.set_file_name("report.pdf"), call.activate(), call.focus_file_name()],
        )
        # Already in the folder: nothing is confirmed at all.
        dialog = _dialog(report.parent)
        choose_file(dialog, report)
        dialog.confirm.assert_not_called()
        dialog.set_file_name.assert_called_once_with("report.pdf")

    def test_a_dialog_that_closes_on_confirm_is_left_alone(self):
        from pathscout.platform.file_dialog import open_folder

        dialog = _dialog(self.root)
        dialog.exists.return_value = False  # A folder picker accepted the folder.
        open_folder(dialog, self.root / "chosen")
        dialog.set_file_name.assert_called_once_with(str(self.root / "chosen"))
        dialog.activate.assert_not_called()

    def test_confirm_sends_the_default_buttons_command_like_enter(self):
        from pathscout.platform import file_dialog
        from pathscout.platform.file_dialog import (
            DC_HASDEFID,
            DM_GETDEFID,
            IDOK,
            WM_COMMAND,
            FileDialog,
        )
        from pathscout.platform.windows import user32

        for answer, button in (((DC_HASDEFID << 16) | 2, 2), (0, IDOK)):
            with (
                patch.object(file_dialog, "send_message", return_value=answer) as send,
                patch.object(user32, "GetDlgItem", return_value=55),
                patch.object(user32, "PostMessageW") as post,
            ):
                FileDialog(9).confirm()
            send.assert_called_once_with(9, DM_GETDEFID)
            post.assert_called_once_with(9, WM_COMMAND, button, 55)  # BN_CLICKED is zero.

    def test_file_name_box_is_found_in_open_and_save_dialogs(self):
        from pathscout.platform import file_dialog
        from pathscout.platform.file_dialog import FILE_NAME_COMBO_ID, _find_file_name_edit
        from pathscout.platform.windows import user32

        # handle: (class, parent, visible)
        windows = {
            10: ("Address Band Root", 1, True),
            11: ("Edit", 10, True),  # The address bar while it is edited.
            20: ("SHELLDLL_DefView", 1, True),
            21: ("Edit", 20, True),  # Renaming in the file list.
            30: ("FloatNotifySink", 1, True),
            31: ("ComboBox", 30, True),
            32: ("Edit", 31, True),  # A Save dialog's file-name box.
            40: ("ComboBoxEx32", 1, True),
            41: ("ComboBox", 40, True),
            42: ("Edit", 41, True),  # An Open dialog's file-name box (cmb13).
        }

        def below(child: int, ancestor: int) -> bool:
            parent = windows[child][1]
            while parent != ancestor and parent in windows:
                parent = windows[parent][1]
            return parent == ancestor

        for combo, edit in ((40, 42), (0, 32)):
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        file_dialog,
                        "descendants",
                        side_effect=lambda hwnd: [c for c in sorted(windows) if below(c, hwnd)],
                    )
                )
                stack.enter_context(
                    patch.object(file_dialog, "class_name", side_effect=lambda h: windows[h][0])
                )
                stack.enter_context(
                    patch.object(user32, "GetParent", side_effect=lambda h: windows[h][1])
                )
                stack.enter_context(
                    patch.object(user32, "IsWindowVisible", side_effect=lambda h: windows[h][2])
                )
                stack.enter_context(
                    patch.object(
                        user32,
                        "GetDlgItem",
                        side_effect=lambda _dialog, id, combo=combo: (
                            combo if id == FILE_NAME_COMBO_ID else 0
                        ),
                    )
                )
                self.assertEqual(_find_file_name_edit(1), edit)


@unittest.skipUnless(WINDOWS, "Windows keyboard hook")
class KeyboardQuickSwitchTest(TemporaryFolderTest):
    def listener(self, **options):
        from pathscout.platform.keyboard import KeyboardListener

        listener = KeyboardListener(queue.SimpleQueue(), interval_ms=350, **options)
        listener.tracker.explorer_folder = self.root
        return listener

    def press(self, listener, key: int, *, ctrl_only=True, dialog: int | None = 77) -> bool:
        from pathscout.platform.windows import KeyboardData

        with (
            patch("pathscout.platform.keyboard.only_ctrl_held", return_value=ctrl_only),
            patch("pathscout.platform.keyboard.foreground_file_dialog", return_value=dialog),
        ):
            return listener._should_swallow(KeyboardData(vkCode=key), True)

    def release(self, listener, key: int) -> bool:
        from pathscout.platform.windows import KeyboardData

        return listener._should_swallow(KeyboardData(vkCode=key), False)

    def events(self, listener) -> list:
        return [listener.events.get() for _ in range(listener.events.qsize())]

    def test_ctrl_g_in_a_file_dialog_jumps_to_the_explorer_folder(self):
        from pathscout.platform.windows import VK_G

        listener = self.listener()
        self.assertTrue(self.press(listener, VK_G))
        self.assertTrue(self.press(listener, VK_G))  # Auto-repeat: swallowed, not repeated.
        self.assertTrue(self.release(listener, VK_G))
        self.assertEqual(self.events(listener), [FolderJumpRequested(77, self.root)])

    def test_ctrl_g_passes_through_everywhere_else(self):
        from pathscout.platform.windows import VK_G

        listener = self.listener()
        self.assertFalse(self.press(listener, VK_G, dialog=None))  # No file dialog in front.
        self.assertFalse(self.press(listener, VK_G, ctrl_only=False))  # Ctrl+Shift+G, AltGr+G.
        self.assertFalse(self.press(listener, ord("H")))
        listener.tracker.explorer_folder = None  # No Explorer folder known.
        self.assertFalse(self.press(listener, VK_G))
        self.assertFalse(self.press(self.listener(quick_switch=False), VK_G))
        self.assertFalse(self.release(listener, VK_G))
        self.assertEqual(self.events(listener), [])

    def test_typing_in_a_dialog_file_list_follows_the_quick_switch_setting(self):
        from pathscout.platform.explorer import ExplorerContext
        from pathscout.platform.windows import TypedKey

        for options, captured in (
            ({"explorer_typing": False}, True),
            ({"quick_switch": False}, False),
        ):
            listener = self.listener(**options)
            listener.tracker.context = ExplorerContext(5, 6, self.root, ViewKind.FILE_DIALOG)
            with (
                patch("pathscout.platform.keyboard.dialog_file_view", return_value=(5, 6)),
                patch("pathscout.platform.keyboard.explorer_file_view", return_value=None),
                patch("pathscout.platform.keyboard.typed_key", return_value=TypedKey("r")),
                patch("pathscout.platform.keyboard.window_rect", return_value=(0, 0, 9, 9)),
            ):
                self.assertEqual(self.press(listener, ord("R"), ctrl_only=False), captured)
            expected = ExplorerTyping("r", self.root, (0, 0, 9, 9), (5, 6), ViewKind.FILE_DIALOG)
            self.assertEqual(self.events(listener), [expected] if captured else [])

    def test_tracker_rereads_the_last_explorer_folder_while_a_dialog_is_in_front(self):
        from pathscout.platform import explorer
        from pathscout.platform.explorer import ExplorerContext, ExplorerTracker

        tracker = ExplorerTracker(on_failure=lambda message: None)
        tracker.context = ExplorerContext(1, 2, self.root)
        tracker._follow_explorer(shell=None)
        self.assertEqual(tracker.explorer_folder, self.root)
        moved = self.root / "moved"
        moved.mkdir()
        tracker.context = None
        with (
            patch.object(explorer, "foreground_explorer", return_value=None),
            patch.object(explorer, "foreground_file_dialog", return_value=77),
            patch.object(explorer, "resolve_folder", return_value=moved) as resolve,
        ):
            tracker._follow_explorer(shell=Mock())
        self.assertEqual(resolve.call_args.args[1:], (1, 2))
        self.assertEqual(tracker.explorer_folder, moved)
        # Explorer in front, focus outside its file list: unknown until a dialog asks.
        with patch.object(explorer, "foreground_explorer", return_value=(3, 4)):
            tracker._follow_explorer(shell=None)
        self.assertIsNone(tracker.explorer_folder)

    def test_tracker_resolves_a_focused_dialog_file_list(self):
        from pathscout.platform import explorer
        from pathscout.platform.explorer import ExplorerContext, ExplorerTracker

        with (
            patch.object(explorer, "explorer_file_view", return_value=None),
            patch.object(explorer, "dialog_file_view", return_value=(5, 6)),
            patch.object(explorer, "dialog_folder", return_value=self.root),
        ):
            tracker = ExplorerTracker(on_failure=lambda message: None)
            view = tracker._focused_view()
            self.assertEqual(view, (ViewKind.FILE_DIALOG, (5, 6)))
            context = ExplorerContext(5, 6, self.root, ViewKind.FILE_DIALOG)
            self.assertEqual(tracker._resolve(None, view), context)
            self.assertIsNone(ExplorerTracker(lambda message: None, dialogs=False)._focused_view())
        with (
            patch.object(explorer, "dialog_file_view", return_value=(5, 6)),
            patch.object(explorer, "dialog_folder", return_value=None),  # This PC.
        ):
            self.assertIsNone(tracker._resolve(None, view))


class DialogActionsTest(TemporaryFolderTest):
    def test_folders_open_in_the_dialog_and_files_become_its_file_name(self):
        store = Mock()
        actions = ItemActions(store)
        folder = self.root / "invoices"
        folder.mkdir()
        report = folder / "report.pdf"
        report.touch()
        with (
            patch("pathscout.platform.file_dialog.open_folder") as open_folder,
            patch("pathscout.platform.file_dialog.choose_file") as choose_file,
        ):
            actions.choose_in_dialog(folder, "inv", 33)
            actions.choose_in_dialog(report, "rep", 33)
            actions.show_in_dialog(folder, 33)
            actions.show_in_dialog(self.root / "gone", 33)
        self.assertEqual([c.args[1] for c in open_folder.call_args_list], [folder, folder])
        self.assertEqual(open_folder.call_args.args[0].hwnd, 33)
        self.assertEqual(choose_file.call_args.args[1], report)
        self.assertEqual(
            store.record_launch.call_args_list, [call(folder, "inv"), call(report, "rep")]
        )


class QuickSwitchSettingTest(TemporaryFolderTest):
    def test_setting_defaults_on_round_trips_and_restarts_input(self):
        settings = Settings(roots=())
        self.assertTrue(settings.quick_switch)
        disabled = replace(settings, quick_switch=False)
        self.assertNotEqual(settings.input_key, disabled.input_key)
        file = SettingsFile(self.root / "settings.json")
        file.save(disabled)
        self.assertFalse(file.load().quick_switch)
        self.assertTrue(settings_from_json({"roots": []}).quick_switch)
        with self.assertRaisesRegex(ValueError, "quick_switch must be a bool"):
            settings_from_json({"roots": [], "quick_switch": "yes"})


if __name__ == "__main__":
    unittest.main()
