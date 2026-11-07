"""Catalog value objects and the two kinds of path normalization.

`path_key` is filesystem identity (Windows ordinal case-insensitivity). `normalize`
is search matching (Unicode NFC, case folding and no accents). They must never be mixed.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

FOLDER_SEPARATORS = ("\\", "/")  # Both separate path components on Windows.
REGISTRY_SOURCE = "registry:apps"
APPLICATION_SOURCE_PREFIX = "apps:"  # Start-menu sources; see `CatalogSource.id`.
"""Source id of applications discovered through the App Paths registry keys."""


def fold_case(value: str) -> str:
    """Unicode NFC plus full case folding: `Löhne` becomes `löhne`."""
    return unicodedata.normalize("NFC", value).casefold()


def normalize(value: str) -> str:
    """Search text: `fold_case` without accents, so `Löhne` and `lohne` both become `lohne`.

    Accents are the combining marks of the canonical decomposition; letters that
    only look alike, such as `ø` or `ł`, stay. Results spelled as typed rank a
    little higher (`SearchQuery.spellings`).
    """
    folded = fold_case(value)
    if folded.isascii():
        return folded
    letters = (c for c in unicodedata.normalize("NFD", folded) if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", "".join(letters))


def normalize_extension(extension: str) -> str:
    """`.TXT`, `txt` and `Txt` all become `txt`."""
    return normalize(extension).lstrip(".")


def file_stem(file_name: str) -> str:
    """The name without its extension; a leading or trailing dot is no extension.

    `PurePath.stem` treats a trailing dot differently since Python 3.14.
    """
    dot = file_name.rfind(".")
    return file_name[:dot] if 0 < dot < len(file_name) - 1 else file_name


def path_key(path: Path | str) -> str:
    """Keep filesystem identity separate from Unicode search normalization."""
    value = os.path.abspath(path).replace("\\", "/")
    upper = value.upper()
    if len(upper) != len(value):
        # Windows ordinal casing does not expand sharp-s or compatibility ligatures.
        upper = "".join(c.upper() if len(c.upper()) == 1 else c for c in value)
    return upper.rstrip("/") + "/"


def path_range(path: Path | str) -> tuple[str, str]:
    """A half-open key range covering one path and all descendants, using a SQL index."""
    prefix = path_key(path)
    return prefix, prefix[:-1] + "0"


def is_within(path: Path | str, parent: Path | str) -> bool:
    return path_key(path).startswith(path_key(parent))


def levels_below(path: Path | str, parent: Path | str) -> int:
    """How many folders deep `path` lies in `parent`: 0 for `parent` itself, 1 for an
    entry directly in it. `path` must lie within `parent`."""
    return path_key(path)[len(path_key(parent)) :].count("/")


FOLDER_DEPTH_RANGE = (1, 30)


@dataclass(frozen=True, slots=True)
class FolderDepth:
    """How deep the catalog lists a search folder: `levels` below it, nothing deeper.

    With 3 for D:\\Projects: Python, Python\\tinder_cli and what lies directly in
    tinder_cli are listed; the contents of tinder_cli\\src are not.
    """

    path: str
    levels: int


class ItemKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"
    APP = "app"


@dataclass(frozen=True, slots=True)
class CatalogItem:
    path: Path
    kind: ItemKind
    size: int = 0
    modified: float = 0
    launches: int = 0
    last_used: float = 0
    query_launches: int = 0
    pinned: bool = False
    attributes: int = 0

    @property
    def name(self) -> str:
        return file_stem(self.path.name) if self.kind == ItemKind.APP else self.path.name

    # The attributes below match the search's `Candidate`, so priority rules accept both.

    @property
    def identity(self) -> str:
        return path_key(self.path)

    @property
    def file_name(self) -> str:
        return self.path.name

    @property
    def extension(self) -> str:
        return normalize_extension(self.path.suffix)


@dataclass(frozen=True, slots=True)
class CatalogSource:
    """A directory tree the catalog owns; app-only sources keep only applications.

    The id is persisted in the `entries.root` column, so its format is a schema
    contract: `files:<path key>` or `apps:<path key>`.
    """

    root: Path
    apps_only: bool = False
    # Levels below `root` the catalog lists; None lists every level. Not part of `id`.
    depth: int | None = None

    @property
    def id(self) -> str:
        prefix = APPLICATION_SOURCE_PREFIX if self.apps_only else "files:"
        return prefix + path_key(self.root)

    def covers(self, other: CatalogSource) -> bool:
        """A full source covers every source below it that it reaches as deep; an
        app-only source only app sources.

        A source limited in depth leaves a source below it that reaches deeper, such
        as a theme folder inside D:\\Projects listed in full, to that source.
        """
        if other == self or not is_within(other.root, self.root):
            return False
        if self.apps_only and not other.apps_only:
            return False
        if self.depth is None:
            return True
        if other.depth is None:
            return False
        return levels_below(other.root, self.root) + other.depth <= self.depth

    def lists(self, path: Path) -> bool:
        """Whether `path`, inside `root`, lies within the source's depth."""
        return self.depth is None or levels_below(path, self.root) <= self.depth

    def descends_into(self, folder: Path) -> bool:
        """Whether the scan lists the contents of `folder`, inside `root`."""
        return self.depth is None or levels_below(folder, self.root) < self.depth

    def accepts(self, item: CatalogItem) -> bool:
        return not self.apps_only or item.kind == ItemKind.APP


@dataclass(frozen=True, slots=True)
class ScanReport:
    entries: int
    skipped: int
    errors: tuple[str, ...]
    elapsed: float
    cancelled: bool = False
