"""Alt+P shows the selected file beside the results, read off the GUI thread and bounded."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageIOHandler,
    QImageWriter,
    QPainter,
    QPdfWriter,
    QShortcut,
)
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from winlauncher.catalog import storage
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.search.ranking import SearchHit
from winlauncher.ui import preview
from winlauncher.ui.bar_placement import BarPlacement
from winlauncher.ui.preview import PANE_WIDTH, SETTLE_MS, PreviewRequest, load_preview
from winlauncher.ui.website_icons import WebsiteIcons
from winlauncher.ui.window import LauncherWindow

PANE = QSize(300, 200)
# WinLauncher's own process searches DLLs as its loader sets up (loader/src/main.rs).
LOADER_DLL_SEARCH = """
import ctypes, sys
from pathlib import Path
ctypes.windll.kernel32.SetDefaultDllDirectories(0x1000)
from PySide6.QtCore import QSize
from winlauncher.__main__ import _create_application
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.ui.preview import PreviewRequest, load_preview
app = _create_application()
item = CatalogItem(Path(sys.argv[1]), ItemKind.FILE)
print(load_preview(PreviewRequest(item, QSize(200, 200), dark=False)).image is not None)
"""


def read(item: CatalogItem, *, dark: bool = False) -> preview.Preview:
    return load_preview(PreviewRequest(item, PANE, dark))


class LoadPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="winlauncher-preview-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def file(self, name: str, data: bytes = b"") -> CatalogItem:
        path = self.root / name
        path.write_bytes(data)
        return CatalogItem(path, ItemKind.FILE, size=len(data))

    def test_images_are_decoded_at_the_pane_size(self) -> None:
        item = self.file("photo.png")
        picture = QImage(1200, 400, QImage.Format.Format_RGB32)
        picture.fill(QColor("teal"))
        picture.save(str(item.path))
        shown = read(item).image
        self.assertEqual(shown.size(), QSize(300, 100))

    def test_photos_stand_upright_by_their_exif_orientation(self) -> None:
        item = self.file("portrait.jpg")
        stored = QImage(1200, 800, QImage.Format.Format_RGB32)  # Landscape pixels…
        stored.fill(QColor("teal"))
        writer = QImageWriter(str(item.path))
        writer.setTransformation(QImageIOHandler.Transformation.TransformationRotate90)
        self.assertTrue(writer.write(stored))
        self.assertEqual(read(item).image.size(), QSize(133, 200))  # …shown portrait.

    def test_markdown_renders_and_code_is_colored_for_the_theme(self) -> None:
        notes = self.file("notes.md", b"# Title\n\n*point*\n")
        self.assertEqual(read(notes).markdown, "# Title\n\n*point*\n")
        code = self.file("tool.py", b"def run():\n    return 1\n")
        light, dark = read(code).html, read(code, dark=True).html
        self.assertIn("color:", light)
        self.assertNotEqual(light, dark)
        self.assertEqual(read(self.file("plain.txt", b"just text")).text, "just text")

    def test_a_pdf_shows_its_first_page_on_white_paper(self) -> None:
        item = self.file("report.pdf")
        writer = QPdfWriter(str(item.path))
        painter = QPainter(writer)
        painter.drawText(QRectF(0, 0, 4000, 400), "Report")
        painter.end()
        page = read(item).image
        self.assertIsNotNone(page)
        self.assertLessEqual(page.height(), PANE.height())
        self.assertEqual(page.pixelColor(page.width() - 1, page.height() - 1), QColor("white"))

    def test_pdf_pages_render_in_winlauncher_s_own_process(self) -> None:
        item = self.file("report.pdf")
        writer = QPdfWriter(str(item.path))
        QPainter(writer).end()
        run = subprocess.run(
            [sys.executable, "-c", LOADER_DLL_SEARCH, str(item.path)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=Path(__file__).parents[1],
        )
        self.assertEqual(run.stdout.strip(), "True", run.stderr)

    def test_text_shows_its_start_without_a_cut_character(self) -> None:
        start = "Grüße\n"
        item = self.file("notes.txt", (start + "ü").encode())
        # The limit falls inside the last character's two UTF-8 bytes.
        with patch.object(preview, "TEXT_BYTES", len(start.encode()) + 1):
            self.assertEqual(read(item).text, start)

    def test_binary_files_and_empty_folders_show_a_note(self) -> None:
        program = self.file("tool.exe", b"MZ\0\0")
        self.assertEqual(read(program).note, preview.NO_PREVIEW_NOTE)
        (self.root / "empty").mkdir()
        folder = CatalogItem(self.root / "empty", ItemKind.FOLDER)
        self.assertEqual(read(folder).note, preview.EMPTY_FOLDER_NOTE)

    def test_folders_list_folders_first_and_count_their_entries(self) -> None:
        folder = self.root / "project"
        (folder / "zebra").mkdir(parents=True)
        for name in ("b.txt", "A.md", "setup.exe"):
            (folder / name).touch()
        shown = read(CatalogItem(folder, ItemKind.FOLDER))
        names = [entry.path.name for entry in shown.entries]
        self.assertEqual(names, ["zebra", "A.md", "b.txt", "setup.exe"])
        self.assertEqual(shown.summary, "1 folder  ·  3 files")
        with (
            patch.object(preview, "FOLDER_SCAN_LIMIT", 2),
            patch.object(preview, "FOLDER_ENTRIES", 1),
        ):
            shown = read(CatalogItem(folder, ItemKind.FOLDER))
        self.assertEqual(len(shown.entries), 1)
        self.assertEqual(shown.summary, "More than 2 entries")

    def test_nothing_is_downloaded_or_waited_for(self) -> None:
        item = self.file("cloud.jpg", b"placeholder")
        online_only = Mock(st_size=10, st_file_attributes=storage.FILE_ATTRIBUTE_OFFLINE)
        with patch.object(preview.long_paths, "stat", return_value=online_only):
            self.assertEqual(read(item).note, preview.ONLINE_ONLY_NOTE)
        released = threading.Event()
        self.addCleanup(released.set)
        dead = storage.DriveProbe(0.01, lambda _root: released.wait())
        share = CatalogItem(Path(r"\\sleeping-nas\share\photo.jpg"), ItemKind.FILE)
        with patch.object(storage, "_DRIVES", dead):
            self.assertIn("does not respond", read(share).note)

    def test_huge_images_are_not_decoded(self) -> None:
        item = self.file("poster.png", b"0" * 64)
        with patch.object(preview, "MAX_IMAGE_BYTES", 10):
            self.assertEqual(read(item).note, preview.TOO_LARGE_NOTE)


class PreviewPaneFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = LauncherWindow(WebsiteIcons())
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.window.deleteLater)
        self.hits = [
            SearchHit(CatalogItem(Path(rf"C:\Work\note{index}.txt"), ItemKind.FILE), 9, "x")
            for index in range(2)
        ]
        self.window.present(text="note")
        self.window.set_results(self.hits, len(self.hits))

    def test_alt_p_and_f3_are_the_preview_keys(self) -> None:
        keys = {shortcut.key().toString() for shortcut in self.window.findChildren(QShortcut)}
        self.assertLessEqual({"Alt+P", "F3"}, keys)

    def test_the_preview_stays_closed_until_the_setting_allows_it(self) -> None:
        width = self.window.width()
        self.window.toggle_preview()  # Alt+P
        self.assertFalse(self.window.preview_open)
        self.assertEqual(self.window.width(), width)

    def test_the_preview_follows_the_selection_and_opens_as_it_was_left(self) -> None:
        self.window.set_preview_enabled(True)
        width = self.window.width()
        requests = QSignalSpy(self.window.preview_requested)
        self.window.toggle_preview()  # Alt+P
        self.assertTrue(self.window.preview_open)
        screen = self.window.screen().availableGeometry().width()
        self.assertEqual(self.window.width(), min(width + PANE_WIDTH, screen))
        QTest.keyClick(self.window.query, Qt.Key.Key_Down)
        QTest.qWait(SETTLE_MS * 3)
        self.assertEqual(requests.count(), 1)  # Only where the selection came to rest.
        self.assertEqual(requests.at(0)[0].item, self.hits[1].item)
        self.window.hide()
        self.window.present(text="note")
        self.assertTrue(self.window.preview_open)  # Left open, it opens again.
        self.window.toggle_preview()
        self.window.hide()
        self.window.present(text="note")
        self.assertFalse(self.window.preview_open)
        self.assertEqual(self.window.width(), width)

    def test_the_open_preview_is_remembered_across_starts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "launcher-bar.json"
            BarPlacement(file).remember_preview(True)
            self.assertTrue(BarPlacement(file).preview_open)
            BarPlacement(file).remember_preview(False)
            self.assertFalse(BarPlacement(file).preview_open)

    def test_the_explorer_bar_has_no_preview(self) -> None:
        self.window.set_preview_enabled(True)
        self.window.present(text="note", scope=Path(r"C:\Work"), near=(0, 0, 1200, 900))
        self.window.toggle_preview()  # Alt+P
        self.assertFalse(self.window.preview_open)


if __name__ == "__main__":
    unittest.main()
