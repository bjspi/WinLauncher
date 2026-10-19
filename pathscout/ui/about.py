"""About dialog: version, origin, license and where PathScout keeps its data."""

from __future__ import annotations

import platform
from html import escape
from pathlib import Path

import PySide6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout

from pathscout import __version__
from pathscout.platform.git import checkout_root, current_head, remote_url
from pathscout.ui.theme import app_icon

TAGLINE = "Fast local search for apps, files and folders on Windows."


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


class AboutDialog(QDialog):
    def __init__(self, data_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About PathScout")
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(56, 56))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        text = QLabel(
            f'<p style="font-size:15pt; font-weight:600; margin:0">PathScout {__version__}</p>'
            f"<p>{TAGLINE}</p>"
            f"<p>{_source_line()}<br>"
            f"Data: {escape(str(data_dir))}<br>"
            f"Python {platform.python_version()} · PySide6 {PySide6.__version__}"
            f" · Qt {qVersion()}</p>"
            "<p>Open source under the MIT License.</p>"
        )
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        row = QHBoxLayout()
        row.addWidget(icon)
        row.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(buttons)
        self.resize(480, 0)
