"""Actions use case: launch items and maintain personal ranking signals."""

from __future__ import annotations

from pathlib import Path

from pathscout.catalog.store import CatalogStore
from pathscout.platform import shell


class ItemActions:
    def __init__(self, store: CatalogStore):
        self.store = store

    def open(
        self, path: Path, query_key: str, *, explorer: tuple[int, int, Path] | None = None
    ) -> None:
        if explorer is not None and path.is_dir():
            from pathscout.platform.explorer import navigate_to

            navigate_to(path, *explorer)
        else:
            shell.open_path(path)
        # Only successful shell invocations count as usage.
        self.store.record_launch(path, query_key)

    def reveal(self, path: Path) -> None:
        shell.show_in_folder(path)

    def favorite(self, path: Path, enabled: bool) -> None:
        self.store.set_favorite(path, enabled)
