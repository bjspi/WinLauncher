"""Explicit Windows environment/home paths, without recursive indexing."""

from __future__ import annotations

import heapq
import ntpath
import os
import re
import stat
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from pathscout.catalog import long_paths
from pathscout.catalog.exclusions import ExclusionPolicy
from pathscout.catalog.model import CatalogItem, ItemKind, normalize
from pathscout.catalog.store import CatalogStore
from pathscout.search.candidates import Candidate
from pathscout.search.query import SearchQuery
from pathscout.search.ranking import Ranker, SearchHit
from pathscout.settings import Settings

# Suggest only directory aliases, never arbitrary environment values such as tokens.
DIRECTORY_VARIABLES = (
    "USERPROFILE",
    "TEMP",
    "TMP",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "WINDIR",
    "SYSTEMROOT",
    "PUBLIC",
)
_VARIABLE = re.compile(r"%([^%]+)%")


def search_paths(
    query: SearchQuery, settings: Settings, cancel: threading.Event | None, store: CatalogStore
) -> list[SearchHit]:
    exclusions, priorities = settings.exclusions(), settings.priorities()
    ranker = Ranker(replace(query, navigation=None, scope=None))

    def hits() -> Iterator[SearchHit]:
        for item, reason in _path_items(query.navigation, exclusions, cancel):
            if cancel is not None and cancel.is_set():
                return
            candidate = Candidate.from_item(item)
            priority = priorities.evaluate(candidate)
            if not ranker.eligible(candidate, priority):
                continue
            if not exclusions.allows(item.path, directory=item.kind == ItemKind.FOLDER):
                continue
            ranking = ranker.rank(candidate, priority)
            if ranking is not None:
                hit = ranking.hit(item)
                explanation = (
                    reason + " · " + hit.reason if ranking.reasons or hit.low_priority else reason
                )
                yield replace(hit, reason=explanation)

    kept = heapq.nsmallest(
        settings.max_results,
        hits(),
        key=lambda h: (
            h.band,
            h.item.kind != ItemKind.FOLDER,
            h.item.name.casefold(),
            str(h.item.path),
        ),
    )
    if cancel is not None and cancel.is_set():
        return []
    if kept:
        identities = tuple(hit.item.identity for hit in kept)
        with store.connect() as connection:
            placeholders = ",".join("?" for _ in identities)
            favorites = {
                row[0]
                for row in connection.execute(
                    f"SELECT path_key FROM favorites WHERE path_key IN ({placeholders})", identities
                )
            }
        kept = [
            replace(hit, item=replace(hit.item, pinned=hit.item.identity in favorites))
            for hit in kept
        ]
    return kept


def _path_items(
    text: str, exclusions: ExclusionPolicy, cancel: threading.Event | None
) -> Iterator[tuple[CatalogItem, str]]:
    if cancel is not None and cancel.is_set():
        return
    if text.startswith("%") and "%" not in text[1:]:
        prefix, seen = text[1:].casefold(), set()
        for name in DIRECTORY_VARIABLES:
            if cancel is not None and cancel.is_set():
                return
            value = os.environ.get(name)
            if name.casefold().startswith(prefix) and value:
                path = Path(value)
                if path.is_absolute() and path.is_dir() and path not in seen:
                    seen.add(path)
                    yield _item(path), f"Windows folder: %{name}%"
        return
    for name in _VARIABLE.findall(text):
        if name not in os.environ:
            raise ValueError(f"Unknown environment variable: %{name}%")
    path = Path(ntpath.expandvars(text).replace("\\", os.sep)).expanduser()
    if not path.is_absolute():
        raise ValueError("The expanded shortcut must be an absolute path.")
    browsing = text.endswith(("/", "\\"))
    if not browsing and os.path.lexists(long_paths.os_path(path)):
        yield _item(path), "Path shortcut"
        return
    parent, prefix = (path, "") if browsing else (path.parent, normalize(path.name))
    if not exclusions.allows(parent, directory=True) or not os.path.isdir(
        long_paths.os_path(parent)
    ):
        return
    with long_paths.scandir(parent) as entries:
        for entry in entries:
            if cancel is not None and cancel.is_set():
                return
            if normalize(entry.name).startswith(prefix):
                try:
                    yield _item(long_paths.ordinary_path(entry.path)), "Path completion"
                except FileNotFoundError:
                    continue  # A temporary file may disappear during enumeration.


def _item(path: Path) -> CatalogItem:
    info = long_paths.stat(path)
    kind = ItemKind.FOLDER if stat.S_ISDIR(info.st_mode) else ItemKind.FILE
    return CatalogItem(
        path, kind, info.st_size, info.st_mtime, attributes=getattr(info, "st_file_attributes", 0)
    )
