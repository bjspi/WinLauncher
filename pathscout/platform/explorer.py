"""Resolve Explorer's active folder on a separate COM apartment."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pathscout.platform.file_dialog import dialog_folder, dialog_typing_view
from pathscout.platform.gesture import ViewKind
from pathscout.platform.windows import (
    activate_window,
    active_tab,
    explorer_file_view,
    foreground_explorer,
    foreground_file_dialog,
)

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.25
# IShellFolderView::SelectItem flags (SVSI_*): select only this item, scroll to it
# and give it the focus rectangle.
SVSI_SELECT = 0x01
SVSI_DESELECTOTHERS = 0x04
SVSI_ENSUREVISIBLE = 0x08
SVSI_FOCUSED = 0x10
SELECT_ONLY_AND_REVEAL = SVSI_SELECT | SVSI_DESELECTOTHERS | SVSI_ENSUREVISIBLE | SVSI_FOCUSED


@dataclass(frozen=True, slots=True)
class ExplorerContext:
    """A focused file view and its folder; for a dialog, `tab` is its file view.

    A dialog may show a folder without a path, such as Desktop or This PC; it
    still counts, because its search bar searches everywhere.
    """

    hwnd: int
    tab: int
    folder: Path | None
    kind: ViewKind = ViewKind.EXPLORER


FocusedView = tuple[ViewKind, tuple[int, int]]


def resolve_folder(shell_windows, hwnd: int, tab: int) -> Path | None:
    """Prefer the active tab's folder; refuse to guess between inactive tabs."""
    exact, fallback = set(), set()
    for window in shell_windows:
        folder, is_active_tab = _window_folder(window, hwnd, tab)
        if folder is None:
            continue
        fallback.add(folder)
        if is_active_tab:
            exact.add(folder)
    choices = exact or fallback
    return next(iter(choices)) if len(choices) == 1 else None


def _window_folder(window, hwnd: int, tab: int) -> tuple[Path | None, bool]:
    """The folder of one shell window belonging to `hwnd`, and whether it is `tab`."""
    import pythoncom
    from win32com.shell import shell

    try:
        if int(window.HWND) != hwnd:
            return None, False
        folder = Path(window.Document.Folder.Self.Path)
        if not folder.is_absolute():
            return None, False  # Virtual folders such as Home or This PC.
        provider = window._oleobj_.QueryInterface(pythoncom.IID_IServiceProvider)
        browser = provider.QueryService(shell.SID_STopLevelBrowser, pythoncom.IID_IOleWindow)
        return folder, int(browser.GetWindow()) == tab
    except (pythoncom.com_error, AttributeError, TypeError, ValueError):
        return None, False


def navigate_to(path: Path, hwnd: int, tab: int, original_folder: Path) -> None:
    """Navigate the captured Explorer tab, refusing stale or ambiguous targets."""
    import pythoncom

    pythoncom.CoInitialize()
    try:
        _captured_window(hwnd, tab, original_folder).Navigate2(str(path), 0)
        activate_window(hwnd)
        logger.info("Explorer navigated: window=%#x tab=%#x path=%s", hwnd, tab, path)
    finally:
        pythoncom.CoUninitialize()


def select_item(entry: Path, hwnd: int, tab: int, original_folder: Path) -> None:
    """Select and reveal `entry`, a child of the captured folder, in its Explorer tab.

    Explorer keeps its keyboard focus where it is, so the search bar stays usable.
    """
    import pythoncom

    pythoncom.CoInitialize()
    try:
        view = _captured_window(hwnd, tab, original_folder).Document
        item = view.Folder.ParseName(entry.name)
        if item is None:
            raise OSError(f"{entry.name} is not shown in this Explorer folder.")
        view.SelectItem(item, SELECT_ONLY_AND_REVEAL)
    finally:
        pythoncom.CoUninitialize()


def _captured_window(hwnd: int, tab: int, original_folder: Path):
    """The shell window of the Explorer tab a search started in, if it still shows its folder."""
    import win32com.client

    if active_tab(hwnd) != tab:
        raise OSError("The original Explorer tab is no longer active. Search again in Explorer.")
    exact, fallback = [], []
    for window in win32com.client.Dispatch("Shell.Application").Windows():
        folder, active = _window_folder(window, hwnd, tab)
        if folder != original_folder:
            continue
        fallback.append(window)
        if active:
            exact.append(window)
    choices = exact or fallback
    if len(choices) != 1:
        raise OSError(
            "The original Explorer folder is no longer available. Search again in Explorer."
        )
    return choices[0]


class ExplorerTracker(threading.Thread):
    """Keeps `context` current so the keyboard hook only reads a cached value.

    With `dialogs`, file lists of Open and Save dialogs count as well, and
    `explorer_folder` holds the folder of the most recently focused Explorer tab
    while a file dialog is in front, for Ctrl+G.
    """

    def __init__(self, on_failure: Callable[[str], None], *, dialogs: bool = True):
        super().__init__(name="explorer-context", daemon=True)
        self.context: ExplorerContext | None = None
        self.explorer_folder: Path | None = None
        self.dialogs = dialogs
        self.on_failure = on_failure
        self._last_explorer: tuple[int, int] | None = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            self._track(win32com.client.Dispatch("Shell.Application"))
        except Exception as exc:
            logger.exception("Explorer integration is unavailable")
            self.on_failure(f"Explorer type-to-search unavailable: {exc}")
        finally:
            self.context = None
            self.explorer_folder = None
            pythoncom.CoUninitialize()

    def _track(self, shell) -> None:
        import pythoncom

        while not self._stop_event.is_set():
            view = self._focused_view()
            self._forget_unless_focused(view)
            self.context = self._resolve(shell, view) if view else None
            if self.dialogs:
                self._follow_explorer(shell)
            pythoncom.PumpWaitingMessages()
            self._stop_event.wait(POLL_SECONDS)

    def _focused_view(self) -> FocusedView | None:
        if view := explorer_file_view():
            return ViewKind.EXPLORER, view
        if self.dialogs and (view := dialog_typing_view()):
            return ViewKind.FILE_DIALOG, view
        return None

    def _forget_unless_focused(self, view: FocusedView | None) -> None:
        """Drop a stale context before the slow lookup, so keys are never misrouted."""
        context = self.context
        if context and view != (context.kind, (context.hwnd, context.tab)):
            self.context = None

    @classmethod
    def _resolve(cls, shell, focused: FocusedView) -> ExplorerContext | None:
        kind, view = focused
        if kind is ViewKind.FILE_DIALOG:
            folder = dialog_folder(view[0])
            current = dialog_typing_view
        else:
            folder = cls._explorer_folder(shell, view)
            current = explorer_file_view
            if folder is None:
                return None  # An Explorer search needs its folder.
        # The user may have switched views while the folder was being resolved.
        return ExplorerContext(*view, folder, kind) if current() == view else None

    @staticmethod
    def _explorer_folder(shell, view: tuple[int, int]) -> Path | None:
        import pythoncom

        try:
            folder = resolve_folder(shell.Windows(), *view)
            return folder if folder is not None and folder.is_dir() else None
        except (pythoncom.com_error, OSError):
            logger.debug("Explorer context temporarily unavailable", exc_info=True)
            return None

    def _follow_explorer(self, shell) -> None:
        """Remember the last Explorer tab; read its folder again while a file dialog is in front.

        That tab may have navigated, or closed, since it had the focus.
        """
        context = self.context
        if context is not None and context.kind is ViewKind.EXPLORER:
            self._last_explorer = context.hwnd, context.tab
            self.explorer_folder = context.folder
        elif explorer := foreground_explorer():
            # Focus is in Explorer's address bar or tree; resolve once a dialog is in front.
            self._last_explorer, self.explorer_folder = explorer, None
        elif self._last_explorer is not None and foreground_file_dialog():
            self.explorer_folder = self._explorer_folder(shell, self._last_explorer)
