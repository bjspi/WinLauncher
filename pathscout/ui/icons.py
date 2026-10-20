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
SLOW_LOOKUP_MS = 100
FILE_ATTRIBUTE_NORMAL = 0x80
logger = logging.getLogger(__name__)


class _ShellIconLoader(QObject):
    """One thread for the whole process asks the shell for icons.

    The thread lives as long as the process: with PySide6 6.12 on Python 3.11,
    ending a thread that emitted Qt signals can abort the process later. As a
    daemon it never delays quitting, even when a shell extension stalls. It hands
    over raw icon handles; the GUI thread turns them into images and routes each
    to the cache that asked, or frees it if that cache has closed meanwhile.
    """

    _loaded = Signal(object, str, object)  # Requester id, key, HICON (0 when there is none).

    def __init__(self):
        super().__init__()
        self._requests: queue.SimpleQueue = queue.SimpleQueue()
        self._requesters: dict[int, ResultIcons] = {}
        self._loaded.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=self._run, name="shell-icons", daemon=True).start()

    def register(self, requester: ResultIcons) -> None:
        self._requesters[id(requester)] = requester

    def unregister(self, requester: ResultIcons) -> None:
        self._requesters.pop(id(requester), None)

    def request(self, requester: ResultIcons, key: str, path: str, own_icon: bool) -> None:
        self._requests.put((id(requester), key, path, own_icon))

    def _run(self) -> None:
        while True:
            requester, key, path, own_icon = self._requests.get()
            started = time.perf_counter()
            try:
                handle = _shell_icon(path, own_icon)
            except Exception:
                logger.exception("Shell icon lookup failed")
                handle = 0
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed > SLOW_LOOKUP_MS:
                logger.warning("Slow shell icon lookup: %.0f ms (own icon=%s)", elapsed, own_icon)
            self._loaded.emit(requester, key, handle)

    def _deliver(self, requester: int, key: str, handle: int) -> None:
        image = _image_from_icon(handle)  # Also frees the handle.
        if (cache := self._requesters.get(requester)) is not None:
            cache._remember(key, image)


_loader: _ShellIconLoader | None = None


def _shared_loader() -> _ShellIconLoader:
    """The process's icon loader, created on first use by the GUI thread."""
    global _loader
    if _loader is None:
        _loader = _ShellIconLoader()
    return _loader


class ResultIcons(QObject):
    """Files share one icon per extension; applications and special folders have their own.

    Every other folder shares the standard folder icon, so painting never waits.
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        provider = QFileIconProvider()
        self._folder = provider.icon(QFileIconProvider.IconType.Folder)
        self._file = provider.icon(QFileIconProvider.IconType.File)
        self._cache: OrderedDict[str, QIcon] = OrderedDict()
        self._pending: set[str] = set()
        self._loader: _ShellIconLoader | None = None
        self._closed = False

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
                if self._loader is None:
                    self._loader = _shared_loader()
                    self._loader.register(self)
                self._loader.request(self, key, str(item.path), own_icon)
            return fallback
        self._cache.move_to_end(key)
        return icon if not icon.isNull() else fallback

    def close(self) -> None:
        """Ignore icons still on their way; the shared loader keeps running."""
        self._closed = True
        if self._loader is not None:
            self._loader.unregister(self)

    def _remember(self, key: str, image: QImage) -> None:
        if self._closed:
            return
        self._pending.discard(key)
        # A failed lookup is cached as a null icon; `icon_for` shows the fallback then.
        self._cache[key] = QIcon(QPixmap.fromImage(image)) if not image.isNull() else QIcon()
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        self.changed.emit()


def _shell_icon(path: str, own_icon: bool) -> int:
    """The large shell icon handle of `path` itself, or of any file with its extension.

    The caller owns the handle; 0 means the shell has no icon.
    """
    import pythoncom
    from win32comext.shell import shell, shellcon

    pythoncom.CoInitialize()
    try:
        flags = shellcon.SHGFI_ICON | shellcon.SHGFI_LARGEICON
        if not own_icon:
            flags |= shellcon.SHGFI_USEFILEATTRIBUTES
            path = "file" + os.path.splitext(path)[1]
        _, info = shell.SHGetFileInfo(path, FILE_ATTRIBUTE_NORMAL, flags)
        return info[0]
    finally:
        pythoncom.CoUninitialize()


def _image_from_icon(handle: int) -> QImage:
    """An image of the icon, which is destroyed afterwards; a null image for 0."""
    if not handle:
        return QImage()
    import win32gui

    try:
        return QImage.fromHICON(handle)
    finally:
        win32gui.DestroyIcon(handle)
