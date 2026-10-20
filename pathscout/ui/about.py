"""About dialog: version, origin, license and where PathScout keeps its data."""

from __future__ import annotations

import platform
from html import escape
from pathlib import Path

import PySide6
from PySide6.QtCore import QUrl, qVersion
from PySide6.QtGui import QDesktopServices

from pathscout import __version__
from pathscout.platform.git import checkout_root, current_head, remote_url
from pathscout.ui.theme import app_icon
from pathscout.ui.widgets import InfoDialog

TAGLINE = "Fast local search for apps, files and folders on Windows."
ABOUT_WIDTH = 540  # Room for the runtime versions on one line.


def _source_line() -> str:
    root = checkout_root()
    if root is None:
        return "Installed package"
    head = current_head(root)
    revision = f" at {head[:10]}" if head else ""
    url = remote_url(root)
    if url and url.startswith("https://"):
        return f'Source checkout{revision} · <a href="{escape(url)}">{escape(url)}</a>'
    return f"Source checkout{revision} in {escape(str(root))}"


class AboutDialog(InfoDialog):
    def __init__(self, data_dir: Path, parent=None):
        super().__init__(f"PathScout {__version__}", TAGLINE, app_icon(), parent, width=ABOUT_WIDTH)
        self.setWindowTitle("About PathScout")
        self.data_dir = data_dir
        self.add_facts(
            [
                ("Source", _source_line()),
                ("Data folder", escape(str(data_dir))),
                (
                    "Runtime",
                    f"Python {platform.python_version()} · PySide6 {PySide6.__version__}"
                    f" · Qt {qVersion()}",
                ),
                ("License", "MIT, open source"),
            ]
        )
        self.add_button("Open data folder", self.open_data_folder)
        self.add_button("Close", self.accept, primary=True)

    def open_data_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))
