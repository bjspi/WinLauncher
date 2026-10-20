"""Read and drive the common Open and Save dialogs of other programs (Quick Switch).

`Shell.Application` lists only Explorer windows, and a dialog's `IFileDialog` is
reachable only inside the process that shows it. PathScout therefore treats a
dialog the way a user does: it reads the folder from the address bar and
navigates by entering a path into the file-name box and pressing the default
button. Only window messages that Windows marshals between processes are used
(WM_GETTEXT, WM_SETTEXT, WM_COMMAND); no code or keystrokes are injected.
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from pathlib import Path

from pathscout.platform.windows import (
    FILE_VIEW_CLASS,
    ULONG_PTR,
    activate_window,
    class_name,
    descendants,
    user32,
)

logger = logging.getLogger(__name__)

# Window messages
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_NEXTDLGCTL = 0x0028
WM_COMMAND = 0x0111
DM_GETDEFID = 0x0400  # WM_USER: the dialog's default push button.
DC_HASDEFID = 0x534B  # High word of a DM_GETDEFID answer that names a button.
BN_CLICKED = 0
IDOK = 1
SMTO_ABORTIFHUNG = 0x0002
MESSAGE_TIMEOUT_MS = 500  # A hung dialog fails the action instead of blocking a worker.

# The breadcrumb toolbar's window text is "<localized label>: <folder path>",
# e.g. "Address: C:\Users" or "Adresse: C:\Users", also with the edit box closed.
ADDRESS_PARENT_CLASS = "Breadcrumb Parent"
ADDRESS_TOOLBAR_CLASS = "ToolbarWindow32"
ADDRESS_BAND_CLASS = "Address Band Root"
# Open dialogs keep the file-name box as control cmb13 (dlgs.h); Save dialogs nest
# it deeper with another id, so the first visible edit outside the address bar
# and the file list is the fallback.
FILE_NAME_COMBO_ID = 1148
EDIT_CLASS = "Edit"
NOT_FILE_NAME_CONTAINERS = (ADDRESS_BAND_CLASS, FILE_VIEW_CLASS)

NAVIGATION_TIMEOUT_SECONDS = 3.0
NAVIGATION_POLL_SECONDS = 0.05
# A drive letter or a UNC prefix starts the path; labels never contain either.
_PATH_START = re.compile(r"[A-Za-z]:\\|\\\\[^\\]")


def folder_from_address(text: str) -> Path | None:
    """The absolute folder in an address text, None for virtual folders such as This PC.

    The label before the path is localized, so the path is found by its own shape.
    """
    match = _PATH_START.search(text)
    if match is None:
        return None
    folder = Path(text[match.start() :].strip())
    return folder if folder.is_absolute() else None


def dialog_folder(dialog: int) -> Path | None:
    """The existing filesystem folder `dialog` shows, read from its address bar."""
    toolbar = _address_toolbar(dialog)
    if toolbar is None:
        return None
    try:
        folder = folder_from_address(window_text(toolbar))
    except OSError:
        return None
    return folder if folder is not None and folder.is_dir() else None


def _address_toolbar(dialog: int) -> int | None:
    for child in descendants(dialog):
        if class_name(child) == ADDRESS_PARENT_CLASS:
            return user32.FindWindowExW(child, None, ADDRESS_TOOLBAR_CLASS, None) or None
    return None


def send_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
    """SendMessage with a timeout; raise OSError if the window is gone or hung."""
    result = ULONG_PTR()
    if not user32.SendMessageTimeoutW(
        hwnd, message, wparam, lparam, SMTO_ABORTIFHUNG, MESSAGE_TIMEOUT_MS, ctypes.byref(result)
    ):
        raise OSError("The file dialog does not respond.")
    return result.value


def window_text(hwnd: int) -> str:
    """The text of a control in another process; WM_GETTEXT is marshaled by Windows."""
    length = send_message(hwnd, WM_GETTEXTLENGTH)
    buffer = ctypes.create_unicode_buffer(length + 1)
    send_message(hwnd, WM_GETTEXT, len(buffer), ctypes.addressof(buffer))
    return buffer.value


def set_window_text(hwnd: int, text: str) -> None:
    buffer = ctypes.create_unicode_buffer(text)
    if not send_message(hwnd, WM_SETTEXT, 0, ctypes.addressof(buffer)):
        raise OSError("The file dialog refused the file name.")


class FileDialog:
    """One Open or Save dialog of another program, addressed by its window handle."""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self._edit: int | None = None

    def folder(self) -> Path | None:
        return dialog_folder(self.hwnd)

    def exists(self) -> bool:
        return bool(user32.IsWindow(self.hwnd))

    def file_name(self) -> str:
        return window_text(self._file_name_edit())

    def set_file_name(self, text: str) -> None:
        set_window_text(self._file_name_edit(), text)

    def confirm(self) -> None:
        """Do what Enter does: send the default button's command to the dialog.

        It is posted rather than sent, so a message box the dialog might open
        cannot block PathScout; `wait_for_folder` observes the outcome.
        """
        answer = send_message(self.hwnd, DM_GETDEFID)
        button = answer & 0xFFFF if answer >> 16 == DC_HASDEFID else IDOK
        command = (BN_CLICKED << 16) | button
        user32.PostMessageW(self.hwnd, WM_COMMAND, command, user32.GetDlgItem(self.hwnd, button))

    def wait_for_folder(self, folder: Path) -> bool:
        """Whether the dialog shows `folder` within the navigation timeout.

        A folder picker ("Select Folder") accepts the folder and closes instead.
        """
        deadline = time.monotonic() + NAVIGATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not self.exists():
                return False
            if self.folder() == folder:
                return True
            time.sleep(NAVIGATION_POLL_SECONDS)
        logger.info("File dialog did not show the folder in time: dialog=%#x", self.hwnd)
        return False

    def focus_file_name(self) -> None:
        """Move the dialog's focus to the file-name box, selecting its text.

        WM_NEXTDLGCTL lets the dialog set its own focus; SetFocus cannot reach
        a window of another thread's input queue.
        """
        send_message(self.hwnd, WM_NEXTDLGCTL, self._file_name_edit(), True)

    def activate(self) -> None:
        activate_window(self.hwnd)

    def _file_name_edit(self) -> int:
        if self._edit is None:
            self._edit = _find_file_name_edit(self.hwnd)
        if self._edit is None:
            raise OSError("This dialog has no file name box.")
        return self._edit


def _find_file_name_edit(dialog: int) -> int | None:
    combo = user32.GetDlgItem(dialog, FILE_NAME_COMBO_ID)
    if combo:
        edit = next((w for w in descendants(combo) if class_name(w) == EDIT_CLASS), None)
        if edit:
            return edit
    for window in descendants(dialog):
        if (
            class_name(window) == EDIT_CLASS
            and user32.IsWindowVisible(window)
            and not _inside_any(window, dialog, NOT_FILE_NAME_CONTAINERS)
        ):
            return window
    return None


def _inside_any(window: int, top_level: int, classes: tuple[str, ...]) -> bool:
    parent = user32.GetParent(window)
    while parent and parent != top_level:
        if class_name(parent) in classes:
            return True
        parent = user32.GetParent(parent)
    return False


def open_folder(dialog: FileDialog, folder: Path, *, file_name: str | None = None) -> None:
    """Show `folder` in `dialog`, then enter `file_name` or put back what the user had typed.

    A folder path in the file-name box plus the default button navigates instead
    of confirming; restoring the typed name matters most in Save dialogs.
    """
    typed = dialog.file_name()
    if dialog.folder() != folder:
        dialog.set_file_name(str(folder))
        dialog.confirm()
        dialog.wait_for_folder(folder)
        if not dialog.exists():
            logger.info("File dialog closed on navigation: dialog=%#x", dialog.hwnd)
            return
    dialog.set_file_name(typed if file_name is None else file_name)
    dialog.activate()
    if file_name is not None:
        dialog.focus_file_name()
    logger.info("File dialog navigated: dialog=%#x folder=%s", dialog.hwnd, folder)


def choose_file(dialog: FileDialog, path: Path) -> None:
    """Show the file's folder and enter its name without confirming; Enter stays the user's."""
    open_folder(dialog, path.parent, file_name=path.name)
