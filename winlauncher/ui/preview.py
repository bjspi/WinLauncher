"""File previews beside the results: images, PDF pages, text and folder contents.

The preview is opt-in (`Settings.file_preview`); Alt+P or F3 opens and closes
it, and the bar remembers which. `load_preview` runs on the `PREVIEW` worker and
reads at most a bounded part of a file: never a cloud
placeholder, which reading would download, and never from a drive that does not
answer. Qt reads images and, through its PDF plugin, a PDF's first page with the
same `QImageReader`. Other files show their first bytes when they hold text, and
otherwise a note; folders list their entries with the results' icons.
`PreviewPane` paints only what arrives for the item it shows
now, so a slow file never replaces a newer selection.
"""

from __future__ import annotations

import codecs
import functools
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from PySide6.QtCore import QLocale, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
)

from winlauncher.catalog import long_paths, storage
from winlauncher.catalog.indexer import classify_kind
from winlauncher.catalog.model import CatalogItem, ItemKind
from winlauncher.plugins.encoding import decode_text
from winlauncher.ui.icons import ResultIcons
from winlauncher.ui.result_rows import format_modified
from winlauncher.ui.theme import DARK, colors

PANE_WIDTH = 380
PANE_MIN_HEIGHT = 320
ENTRY_ICON_SIZE = 16
PANE_GAP = 8  # Between the results' shortcut column and the preview.
# Scrolling through results loads only the row the selection rests on.
SETTLE_MS = 120
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 100_000_000  # Decoding more would take seconds and much memory.
TEXT_BYTES = 64 * 1024
FOLDER_ENTRIES = 200  # Listed, folders first.
FOLDER_SCAN_LIMIT = 2000  # Read for sorting and counting; a huge folder stops here.
PDF_FORMAT = "pdf"  # Qt's image format name for its PDF plugin.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
# Colored code is HTML the GUI thread lays out; more lines than the pane shows would
# only cost time there.
CODE_LINES = 300
LIGHT_CODE_STYLE = "default"  # Pygments styles matching the light and dark themes.
DARK_CODE_STYLE = "github-dark"
CODE_FONT = "font-family: 'Cascadia Mono', 'Consolas'; font-size: 9pt;"
BINARY_MARK = b"\0"  # Text files never contain it; executables, archives and media do.
ONLINE_ONLY_NOTE = "Stored online only. WinLauncher does not download it for a preview."
TOO_LARGE_NOTE = "Too large for a preview."
EMPTY_FOLDER_NOTE = "Empty folder"
NO_PREVIEW_NOTE = "No preview for this kind of result."


@dataclass(frozen=True, slots=True)
class FolderEntry:
    path: Path
    is_folder: bool


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    """What to read: `size` is the picture area in physical pixels, `dark` the theme."""

    item: CatalogItem
    size: QSize
    dark: bool


@dataclass(frozen=True, slots=True)
class Preview:
    """What the pane shows for `path`: an image, text, folder entries, or a note on why not.

    Text is plain, Markdown to render, or code already colored as HTML. Without any
    of them, the preview is still loading. `summary` adds to the facts, such as how
    many folders and files a folder holds.
    """

    path: Path
    image: QImage | None = None
    text: str | None = None
    markdown: str | None = None
    html: str | None = None
    entries: tuple[FolderEntry, ...] | None = None
    summary: str = ""
    note: str = ""

    @property
    def document(self) -> bool:
        """Whether the pane shows rich text: rendered Markdown or colored code."""
        return self.markdown is not None or self.html is not None


def load_preview(request: PreviewRequest) -> Preview:
    """Read the requested item; runs on the `PREVIEW` worker."""
    item, size = request.item, request.size
    path = item.path
    if (state := storage.drive_state(path)) is not storage.DriveState.PRESENT:
        return Preview(path, note=storage.unavailable_message(path, state))
    try:
        if item.kind == ItemKind.FOLDER:
            return _folder_preview(path)
        info = long_paths.stat(path)
        if storage.is_online_only(info):
            return Preview(path, note=ONLINE_ONLY_NOTE)
        if path.suffix[1:].lower() in _image_formats():
            return _image_preview(path, size, info.st_size)
        return _text_preview(path, request.dark)
    except OSError as error:
        return Preview(path, note=f"Cannot read it: {error.strerror or error}")


@functools.cache
def _image_formats() -> frozenset[str]:
    """File extensions Qt reads as images, including `pdf` through its PDF plugin."""
    return frozenset(bytes(name).decode().lower() for name in QImageReader.supportedImageFormats())


def _image_preview(path: Path, size: QSize, file_size: int) -> Preview:
    if file_size > MAX_IMAGE_BYTES:
        return Preview(path, note=TOO_LARGE_NOTE)
    reader = QImageReader(long_paths.os_path(path))
    reader.setAutoTransform(True)  # Photos turned by their camera stand upright.
    full = reader.size()
    if full.isValid():
        if full.width() * full.height() > MAX_IMAGE_PIXELS:
            return Preview(path, note=TOO_LARGE_NOTE)
        fits = full.width() <= size.width() and full.height() <= size.height()
        if not size.isEmpty() and not fits:
            # Decoding at the shown size is fast and needs little memory.
            reader.setScaledSize(full.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return Preview(path, note=f"Cannot show this image: {reader.errorString()}")
    if bytes(reader.format()).decode() == PDF_FORMAT:
        image = _on_paper(image)
    return Preview(path, image=image)


def _on_paper(page: QImage) -> QImage:
    """A PDF page renders transparent; on white it reads like paper in any theme."""
    paper = QImage(page.size(), QImage.Format.Format_RGB32)
    paper.fill(Qt.GlobalColor.white)
    painter = QPainter(paper)
    painter.drawImage(0, 0, page)
    painter.end()
    return paper


def _text_preview(path: Path, dark: bool) -> Preview:
    """The file's first bytes as Markdown, colored code or plain text; binary files get none."""
    with open(long_paths.os_path(path), "rb") as file:
        start = file.read(TEXT_BYTES)
    if BINARY_MARK in start:
        return Preview(path, note=NO_PREVIEW_NOTE)
    text = _decode_start(start)
    if path.suffix.lower() in MARKDOWN_SUFFIXES:
        return Preview(path, markdown=text)
    if (html := _colored_code(path, text, dark)) is not None:
        return Preview(path, html=html)
    return Preview(path, text=text)


def _colored_code(path: Path, text: str, dark: bool) -> str | None:
    """`text` as HTML colored by Pygments for the file's language; None for plain text."""
    # Imported here: Pygments loads its lexers on the worker, never at start.
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_for_filename
    from pygments.lexers.special import TextLexer
    from pygments.util import ClassNotFound

    try:
        lexer = get_lexer_for_filename(path.name, stripnl=False)
    except ClassNotFound:
        return None
    if isinstance(lexer, TextLexer):
        return None
    style = DARK_CODE_STYLE if dark else LIGHT_CODE_STYLE
    formatter = HtmlFormatter(style=style, noclasses=True, nobackground=True)
    shown = "".join(text.splitlines(keepends=True)[:CODE_LINES])
    return highlight(shown, lexer, formatter)


def _decode_start(data: bytes) -> str:
    """The start of a file as text; a character cut off at the end is left out."""
    try:
        return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=False)
    except UnicodeDecodeError:
        return decode_text(data)


def _folder_preview(path: Path) -> Preview:
    """The folder's entries, folders first, each group by name, with their count."""
    with long_paths.scandir(path) as listing:
        entries = [
            FolderEntry(path / entry.name, entry.is_dir())
            for entry in islice(listing, FOLDER_SCAN_LIMIT + 1)
        ]
    if not entries:
        return Preview(path, note=EMPTY_FOLDER_NOTE)
    complete = len(entries) <= FOLDER_SCAN_LIMIT
    entries = sorted(entries[:FOLDER_SCAN_LIMIT], key=_folder_order)
    folders = sum(entry.is_folder for entry in entries)
    summary = (
        _counted(folders, "folder") + "  ·  " + _counted(len(entries) - folders, "file")
        if complete
        else f"More than {FOLDER_SCAN_LIMIT:,} entries"
    )
    return Preview(path, entries=tuple(entries[:FOLDER_ENTRIES]), summary=summary)


def _folder_order(entry: FolderEntry) -> tuple[bool, str]:
    return not entry.is_folder, entry.path.name.casefold()


def _counted(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def item_facts(item: CatalogItem) -> str:
    """Size and date from the catalog, so they appear before anything is read."""
    modified = f"Modified {format_modified(item.modified)}"
    if item.kind == ItemKind.FOLDER:
        return f"{item.path.name}\n{modified}"
    return f"{item.path.name}\n{QLocale().formattedDataSize(item.size)}  ·  {modified}"


class PreviewPane(QFrame):
    """Shows the selected result; asks for its preview once the selection rests.

    `requested` carries a `PreviewRequest`; the owner loads it off the GUI thread
    and hands the result to `show_preview`.
    """

    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("preview")
        self.setFixedWidth(PANE_WIDTH)
        self.setAccessibleName("File preview")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(PANE_GAP, 4, 16, 0)
        layout.setSpacing(8)
        # The picture, or the note on why there is none, in a frame like the text's.
        self.picture = QLabel()
        self.picture.setObjectName("previewPicture")
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setWordWrap(True)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.text = QPlainTextEdit()
        self.text.setObjectName("code")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setAccessibleName("File contents")
        self.document = _DocumentView()
        self.document.setObjectName("previewDocument")
        self.document.setAccessibleName("File contents")
        self.document.setOpenLinks(False)  # A click in the preview never navigates.
        self.document.document().setDefaultStyleSheet(f"pre {{ {CODE_FONT} }}")
        self.entries = QListWidget()
        self.entries.setObjectName("previewEntries")
        self.entries.setAccessibleName("Folder contents")
        self.entries.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.entries.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.entries.setIconSize(QSize(ENTRY_ICON_SIZE, ENTRY_ICON_SIZE))
        # The results' icons: one per extension, loaded off the GUI thread.
        self.icons = ResultIcons(self)
        self.icons.changed.connect(self._refresh_icons)
        self.facts = QLabel()
        self.facts.setObjectName("previewFacts")
        self.facts.setWordWrap(True)
        layout.addWidget(self.picture, 1)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.document, 1)
        layout.addWidget(self.entries, 1)
        layout.addWidget(self.facts)
        self._item: CatalogItem | None = None
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._request)
        self._show(Preview(Path()), facts="")

    def show_item(self, item: CatalogItem | None) -> None:
        """Show `item`, or that a result such as a plugin item has no preview."""
        if item == self._item:
            return
        self._item = item
        self._settle.stop()
        if item is None:
            self._show(Preview(Path(), note=NO_PREVIEW_NOTE), facts="")
            return
        self._show(Preview(item.path), facts=item_facts(item))
        self._settle.start()

    def show_preview(self, preview: Preview) -> None:
        if self._item is None or preview.path != self._item.path:
            return  # The selection moved on while it loaded.
        self._show(preview, facts=item_facts(self._item))

    def _request(self) -> None:
        if self._item is not None:
            dark = colors() is DARK
            self.requested.emit(PreviewRequest(self._item, self._picture_size(), dark))

    def _picture_size(self) -> QSize:
        ratio = self.devicePixelRatioF()
        inner = self.picture.contentsRect().size()
        return QSize(round(inner.width() * ratio), round(inner.height() * ratio))

    def _show(self, preview: Preview, *, facts: str) -> None:
        if preview.image is not None:
            pixmap = QPixmap.fromImage(preview.image)
            pixmap.setDevicePixelRatio(self.devicePixelRatioF())
            self.picture.setPixmap(pixmap)
        else:
            self.picture.setText(preview.note)
        content = preview.text is not None or preview.document or preview.entries is not None
        self.picture.setVisible(not content)
        self.text.setPlainText(preview.text or "")
        self.text.setVisible(preview.text is not None)
        self._render(preview)
        self.document.setVisible(preview.document)
        self._list(preview.entries or ())
        self.entries.setVisible(preview.entries is not None)
        self.facts.setText("  ·  ".join(part for part in (facts, preview.summary) if part))

    def _render(self, preview: Preview) -> None:
        if preview.markdown is not None:
            self.document.setMarkdown(preview.markdown)
            self.document.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        else:
            self.document.setHtml(preview.html or "")
            self.document.setLineWrapMode(QTextBrowser.LineWrapMode.NoWrap)

    def _list(self, entries: tuple[FolderEntry, ...]) -> None:
        self.entries.clear()
        for entry in entries:
            row = QListWidgetItem(entry.path.name)
            row.setData(Qt.ItemDataRole.UserRole, _entry_item(entry))
            self.entries.addItem(row)
        self._refresh_icons()

    def _refresh_icons(self) -> None:
        for index in range(self.entries.count()):
            row = self.entries.item(index)
            row.setIcon(self.icons.icon_for(row.data(Qt.ItemDataRole.UserRole)))


class _DocumentView(QTextBrowser):
    """Rendered Markdown and colored code, without the images a document links to.

    Qt would read them on the GUI thread, local ones from disk; left out, they draw
    nothing instead of a broken-image sign.
    """

    def loadResource(self, kind: int, url: QUrl) -> object:
        if kind == QTextDocument.ResourceType.ImageResource:
            return _NO_IMAGE
        return super().loadResource(kind, url)


_NO_IMAGE = QImage(1, 1, QImage.Format.Format_ARGB32)
_NO_IMAGE.fill(Qt.GlobalColor.transparent)


def _entry_item(entry: FolderEntry) -> CatalogItem:
    """A folder entry as the catalog item the result icons expect."""
    return CatalogItem(entry.path, classify_kind(entry.path, entry.is_folder))
