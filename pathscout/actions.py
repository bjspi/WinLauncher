"""Actions use case: launch items and maintain personal ranking signals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pathscout.catalog.store import CatalogStore
from pathscout.platform import shell


@dataclass(frozen=True, slots=True)
class ExplorerTarget:
    """The Explorer tab a folder result opens in, instead of a new window."""

    window: int
    tab: int
    folder: Path

    def entry_for(self, path: Path) -> Path | None:
        """The entry of `folder` that is or contains `path`; None outside `folder`."""
        try:
            relative = path.relative_to(self.folder)
        except ValueError:
            return None
        return self.folder / relative.parts[0] if relative.parts else None


class ItemActions:
    def __init__(self, store: CatalogStore):
        self.store = store

    def open(self, path: Path, query_key: str, *, explorer: ExplorerTarget | None = None) -> None:
        if explorer is not None and path.is_dir():
            from pathscout.platform.explorer import navigate_to

            navigate_to(path, explorer.window, explorer.tab, explorer.folder)
        else:
            shell.open_path(path)
        # Only successful shell invocations count as usage.
        self.store.record_launch(path, query_key)

    def choose_in_dialog(self, path: Path, query_key: str, dialog: int) -> None:
        """Quick Switch: a folder opens in the file dialog; a file becomes its file name.

        The dialog is never confirmed for a file, so a Save dialog cannot overwrite it.
        """
        from pathscout.platform.file_dialog import FileDialog, choose_file, open_folder

        target = FileDialog(dialog)
        if path.is_dir():
            open_folder(target, path)
        else:
            choose_file(target, path)
        self.store.record_launch(path, query_key)

    def show_in_dialog(self, folder: Path, dialog: int) -> None:
        """Ctrl+G: show `folder` in the file dialog, keeping the typed file name."""
        from pathscout.platform.file_dialog import FileDialog, open_folder

        if folder.is_dir():  # Explorer's folder may have been removed since it was read.
            open_folder(FileDialog(dialog), folder)

    def hand_back_to_dialog(self, text: str, dialog: int) -> None:
        """Quick Switch: the user left the bar; what they typed goes into the file-name box."""
        from pathscout.platform.file_dialog import FileDialog

        FileDialog(dialog).hand_back(text)

    def reveal(self, path: Path) -> None:
        shell.show_in_folder(path)

    def select_in_explorer(self, path: Path, explorer: ExplorerTarget) -> None:
        """Select `path`, or the subfolder that contains it, in the Explorer tab."""
        entry = explorer.entry_for(path)
        if entry is not None:
            from pathscout.platform.explorer import select_item

            select_item(entry, explorer.window, explorer.tab, explorer.folder)

    def favorite(self, path: Path, enabled: bool) -> None:
        self.store.set_favorite(path, enabled)
