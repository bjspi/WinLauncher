"""Resolve Explorer's active folder on a separate COM apartment."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pathscout.platform.windows import activate_window, active_tab, explorer_file_view

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ExplorerContext:
    hwnd: int
    tab: int
    folder: Path


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
    import win32com.client

    pythoncom.CoInitialize()
    try:
        if active_tab(hwnd) != tab:
            raise OSError(
                "The original Explorer tab is no longer active. Search again in Explorer."
            )
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
        choices[0].Navigate2(str(path), 0)
        activate_window(hwnd)
        logger.info("Explorer navigated: window=%#x tab=%#x path=%s", hwnd, tab, path)
    finally:
        pythoncom.CoUninitialize()


class ExplorerTracker(threading.Thread):
    """Keeps `context` current so the keyboard hook only reads a cached value."""

    def __init__(self, on_failure: Callable[[str], None]):
        super().__init__(name="explorer-context", daemon=True)
        self.context: ExplorerContext | None = None
        self.on_failure = on_failure
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
            pythoncom.CoUninitialize()

    def _track(self, shell) -> None:
        import pythoncom

        while not self._stop_event.is_set():
            view = explorer_file_view()
            self._forget_unless_focused(view)
            self.context = self._resolve(shell, view) if view else None
            pythoncom.PumpWaitingMessages()
            self._stop_event.wait(POLL_SECONDS)

    def _forget_unless_focused(self, view: tuple[int, int] | None) -> None:
        """Drop a stale context before the slow COM lookup, so keys are never misrouted."""
        if self.context and view != (self.context.hwnd, self.context.tab):
            self.context = None

    @staticmethod
    def _resolve(shell, view: tuple[int, int]) -> ExplorerContext | None:
        import pythoncom

        try:
            folder = resolve_folder(shell.Windows(), *view)
            if folder is None or not folder.is_dir():
                return None
        except (pythoncom.com_error, OSError):
            logger.debug("Explorer context temporarily unavailable", exc_info=True)
            return None
        # The user may have switched views while COM was resolving the folder.
        return ExplorerContext(*view, folder) if explorer_file_view() == view else None
