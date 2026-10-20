"""Load shell icons off the GUI thread; painting always has an immediate fallback."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import QFileIconProvider

from pathscout.catalog.model import CatalogItem, ItemKind, path_key
from pathscout.platform.shell import special_folders

CACHE_SIZE = 512
logger = logging.getLogger(__name__)


class ResultIcons(QObject):
    """Files share one icon per extension; applications and special folders have their own.

    Every other folder shares the standard folder icon, so painting never waits.
    """

    changed = Signal()
    _loaded = Signal(str, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        provider = QFileIconProvider()
        self._folder = provider.icon(QFileIconProvider.IconType.Folder)
        self._file = provider.icon(QFileIconProvider.IconType.File)
        self._cache: OrderedDict[str, QIcon] = OrderedDict()
        self._pending: set[str] = set()
        self._requests: queue.SimpleQueue = queue.SimpleQueue()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._loaded.connect(self._remember, Qt.ConnectionType.QueuedConnection)

    def icon_for(self, item: CatalogItem) -> QIcon:
        folder = item.kind == ItemKind.FOLDER
        if folder and path_key(item.path) not in special_folders():
            return self._folder
        fallback = self._folder if folder else self._file
        own_icon = item.kind != ItemKind.FILE  # Applications and special folders.
        key = str(item.path) if own_icon else item.path.suffix.lower()
        icon = self._cache.get(key)
        if icon is None:
            if (
                os.name == "nt"
                and not self._closed
                and key not in self._pending
                and len(self._pending) < CACHE_SIZE
            ):
                self._pending.add(key)
                self._requests.put((key, str(item.path), own_icon))
                if self._worker is None:
                    # A stalled shell extension must not block the UI or application exit.
                    self._worker = threading.Thread(
                        target=self._load, name="shell-icons", daemon=True
                    )
                    self._worker.start()
            return fallback
        self._cache.move_to_end(key)
        return icon if not icon.isNull() else fallback

    def close(self) -> None:
        self._closed = True
        self._requests.put(None)

    def _load(self) -> None:
        while (request := self._requests.get()) is not None and not self._closed:
            key, path, own_icon = request
            started = time.perf_counter()
            try:
                image = _shell_image(path, own_icon)
            except Exception:
                logger.exception("Shell icon lookup failed")
                image = QImage()
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed > 100:
                logger.warning("Slow shell icon lookup: %.0f ms (own icon=%s)", elapsed, own_icon)
            if not self._closed:
                self._loaded.emit(key, image)

    def _remember(self, key: str, image: QImage) -> None:
        if self._closed:
            return
        self._pending.discard(key)
        # A failed lookup is cached as a null icon; `icon_for` shows the fallback then.
        self._cache[key] = QIcon(QPixmap.fromImage(image)) if not image.isNull() else QIcon()
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        self.changed.emit()


def _shell_image(path: str, own_icon: bool) -> QImage:
    """The large shell icon of `path` itself, or of any file with its extension."""
    import pythoncom
    import win32gui
    from win32comext.shell import shell, shellcon

    pythoncom.CoInitialize()
    try:
        flags = shellcon.SHGFI_ICON | shellcon.SHGFI_LARGEICON
        if not own_icon:
            flags |= shellcon.SHGFI_USEFILEATTRIBUTES
            path = "file" + os.path.splitext(path)[1]
        _, info = shell.SHGetFileInfo(path, 0x80, flags)  # FILE_ATTRIBUTE_NORMAL
        handle = info[0]
        if not handle:
            return QImage()
        try:
            return QImage.fromHICON(handle)
        finally:
            win32gui.DestroyIcon(handle)
    finally:
        pythoncom.CoUninitialize()
