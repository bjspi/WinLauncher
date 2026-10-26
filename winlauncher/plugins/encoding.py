"""Text written by plugin authors and their tools, in whatever encoding it came.

Plugin files are meant to be UTF-8: TOML requires it, and Python assumes it. Files
saved by older Windows editors are often Windows-1252 instead, and a plugin that
fails over an umlaut in a comment helps nobody. Text is therefore read as UTF-8,
with or without a byte order mark, and otherwise as Windows-1252, the code page of
Western European Windows. A Python file's own coding declaration comes first.
Tool output, such as git's or pip's, is decoded the same way.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

UTF8_ENCODINGS = ("utf-8", "utf-8-sig")
LEGACY_WINDOWS_ENCODING = "cp1252"


def decode_text(data: bytes) -> str:
    """`data` as UTF-8 if it is, else as Windows-1252; never raises."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(LEGACY_WINDOWS_ENCODING)
    except UnicodeDecodeError:  # One of the five bytes Windows-1252 leaves undefined.
        return data.decode("utf-8", errors="replace")


def read_text(file: Path) -> str:
    """A text file such as plugin.toml; raises OSError when it cannot be read."""
    return decode_text(file.read_bytes())


def read_python_source(file: Path) -> str:
    """A Python file, honoring a `# coding:` declaration, else like any text file.

    Raises OSError when the file cannot be read, and UnicodeDecodeError when it
    declares an encoding it does not use.
    """
    data = file.read_bytes()
    try:
        declared, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:  # A first line that is no UTF-8, such as a Windows-1252 comment.
        return decode_text(data)
    if declared in UTF8_ENCODINGS:  # Also what Python assumes without a declaration.
        return decode_text(data)
    return data.decode(declared)
