"""Windows shell icons for results, cached so painting never repeats shell lookups."""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QFileInfo
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFileIconProvider

from pathscout.catalog.model import CatalogItem, ItemKind

CACHE_SIZE = 512


class ResultIcons:
    """Folders share one icon, files share one per extension, applications have their own."""

    def __init__(self):
        self._provider = QFileIconProvider()
        self._folder = self._provider.icon(QFileIconProvider.IconType.Folder)
        self._cache: OrderedDict[str, QIcon] = OrderedDict()

    def icon_for(self, item: CatalogItem) -> QIcon:
        if item.kind == ItemKind.FOLDER:
            return self._folder
        key = str(item.path) if item.kind == ItemKind.APP else item.path.suffix.lower()
        icon = self._cache.get(key)
        if icon is None:
            icon = self._provider.icon(QFileInfo(str(item.path)))
            self._remember(key, icon)
        else:
            self._cache.move_to_end(key)
        return icon

    def _remember(self, key: str, icon: QIcon) -> None:
        self._cache[key] = icon
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
