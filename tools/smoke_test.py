"""Exercise the real Qt/index/search/clipboard flow without global input capture.

Runs offscreen with an isolated data directory. `--native` additionally attaches
and detaches the real low-level keyboard hook without Explorer typing.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from winlauncher.catalog.model import ItemKind
from winlauncher.settings import DEFAULT_DOUBLE_CTRL_MS, Settings, SettingsFile
from winlauncher.ui.controller import LauncherController
from winlauncher.ui.settings import SettingsDialog
from winlauncher.ui.theme import STYLESHEET, app_icon

TIMEOUT_SECONDS = 15
FIXTURE_FILES = (
    "Project Report.txt",
    "Project Plan.md",
    "Projects/Project Notes.txt",
    "Project Budget.csv",
)


class QtErrors:
    """Collect exceptions raised inside Qt slots, which Qt would otherwise only print."""

    def __init__(self):
        self.errors: list[BaseException] = []
        self._previous = sys.excepthook

    def __enter__(self) -> QtErrors:
        sys.excepthook = lambda kind, value, traceback: self.errors.append(value)
        return self

    def __exit__(self, *exc_info) -> None:
        sys.excepthook = self._previous

    def raise_first(self) -> None:
        if self.errors:
            raise self.errors[0]


def create_application() -> QApplication:
    app = QApplication([])
    # Qt's offscreen plugin does not discover Windows system fonts automatically.
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("segoeui.ttf", "seguisb.ttf", "seguisym.ttf"):
        if (fonts / filename).is_file():
            QFontDatabase.addApplicationFont(str(fonts / filename))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    return app


def wait_until(app: QApplication, condition: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        app.processEvents()
        time.sleep(0.02)


def create_fixture(base: Path) -> tuple[Path, Path]:
    """Visible project files plus one file under a `.nobackup` marker."""
    root = base / "Work"
    for relative in FIXTURE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Smoke test fixture", encoding="utf8")
    private = root / "Private"
    private.mkdir()
    (private / ".nobackup").write_text("")
    (private / "Project Secret.txt").write_text("Excluded fixture")
    data = base / "state"
    SettingsFile(data / "settings.json").save(Settings(roots=(str(root),), index_apps=False))
    return root, data


def result_texts(controller: LauncherController) -> list[str]:
    results = controller.window.results
    return [results.item(row).text() for row in range(results.count())]


def check_search_and_exclusions(app: QApplication, controller: LauncherController) -> None:
    controller.window.present(text="project")
    wait_until(
        app,
        lambda: controller.indexing.last_report is not None and len(result_texts(controller)) >= 4,
        "the initial scan and search results",
    )
    assert not any("Secret" in text for text in result_texts(controller)), "Marker ignored"


def check_clipboard_and_favorite(app: QApplication, controller: LauncherController) -> None:
    window = controller.window
    window.copy_target()
    assert QApplication.clipboard().text() == str(window.selected_hit().item.path)
    controller.favorite_item(window.selected_hit())
    wait_until(app, lambda: window.selected_hit().item.pinned, "the favorite to appear")


def check_settings_and_filters(controller: LauncherController) -> None:
    dialog = SettingsDialog(
        controller.settings, controller.window, website_icons=controller.website_icons
    )
    dialog.save()
    assert dialog.result_settings == controller.settings, "Unchanged dialog altered settings"
    dialog.deleteLater()
    window, search = controller.window, controller.search
    window.set_kind(ItemKind.FILE)
    assert search.current_query().history_key == "project type:file"
    window.query.setText("project type:app")
    assert search.current_query().history_key == "project type:app"
    window.query.setText("project")
    window.set_kind(None)


def run_ui_flow(app: QApplication, screenshot: Path | None) -> int:
    with tempfile.TemporaryDirectory(prefix="winlauncher-ui-") as temporary:
        _, data = create_fixture(Path(temporary))
        controller = LauncherController(data, hooks=False)
        try:
            if os.name == "nt":
                assert controller.monitor is not None, "Disabling hooks disabled file watching"
            controller.tray.hide()
            check_search_and_exclusions(app, controller)
            check_clipboard_and_favorite(app, controller)
            check_settings_and_filters(controller)
            wait_until(app, lambda: len(result_texts(controller)) >= 4, "the restored results")
            if screenshot:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                assert controller.window.grab().save(str(screenshot)), "Screenshot not saved"
            return len(result_texts(controller))
        finally:
            controller.close()


def check_native_keyboard_hook() -> None:
    from winlauncher.platform.keyboard import KeyboardListener

    listener = KeyboardListener(
        queue.SimpleQueue(),
        interval_ms=DEFAULT_DOUBLE_CTRL_MS,
        explorer_typing=False,
        quick_switch=False,
    )
    listener.start()
    try:
        assert listener.ready.wait(5), "Keyboard listener did not initialize"
        assert listener._hook, "SetWindowsHookExW did not attach"
    finally:
        listener.stop()
        listener.join(timeout=3)
    assert not listener.is_alive(), "Keyboard listener did not stop"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument(
        "--native",
        action="store_true",
        help="Also attach/detach the native keyboard hook without capturing Explorer input",
    )
    args = parser.parse_args()
    app = create_application()
    with QtErrors() as qt_errors:
        count = run_ui_flow(app, args.screenshot)
    qt_errors.raise_first()
    print(
        f"Qt smoke test passed: {count} results, exclusions, clipboard, favorites and settings",
        flush=True,
    )
    if args.native:
        check_native_keyboard_hook()
        print("Native keyboard hook attached and detached successfully", flush=True)


if __name__ == "__main__":
    main()
