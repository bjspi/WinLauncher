"""Results as clipboard and drag data that Explorer pastes and drops like its own files."""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QMimeData, QUrl

# Explorer reads this clipboard format to tell a cut (move on paste) from a copy.
PREFERRED_DROP_EFFECT = 'application/x-qt-windows-mime;value="Preferred DropEffect"'
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2


def file_mime_data(paths: Iterable[Path], *, cut: bool = False) -> QMimeData:
    """Files for the clipboard or a drag; Qt offers them to Windows as a file drop list."""
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    effect = DROPEFFECT_MOVE if cut else DROPEFFECT_COPY
    data.setData(PREFERRED_DROP_EFFECT, struct.pack("<I", effect))  # A little-endian DWORD.
    return data


def url_mime_data(url: str) -> QMimeData:
    """A web address, which browsers, mail programs and Explorer accept as a link."""
    data = QMimeData()
    data.setUrls([QUrl(url)])
    data.setText(url)
    return data
