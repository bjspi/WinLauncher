"""Absolute Windows paths and environment/home shortcuts, without recursive indexing."""

from __future__ import annotations

import heapq
import os
import re
import stat
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from winlauncher.catalog import long_paths
from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.model import CatalogItem, ItemKind, normalize, path_key
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.candidates import Candidate
from winlauncher.search.query import SearchQuery
from winlauncher.search.query_parser import FOLDER_SEPARATORS
from winlauncher.search.ranking import Ranker, SearchHit
from winlauncher.settings import Settings

_VARIABLE = re.compile(r"%([^%]+)%")
PATH_SHORTCUT_REASON = "Path shortcut"
PATH_COMPLETION_REASON = "Path completion"


@dataclass(frozen=True, slots=True)
class _NavigationMatch:
    """A filesystem target and its navigation label, before search policy is applied."""

    item: CatalogItem
    reason: str
    alias: str | None = None


def search_paths(
    query: SearchQuery,
    settings: Settings,
    cancel: threading.Event | None,
    store: CatalogStore,
    shortcuts: Mapping[str, Path] | None = None,
) -> list[SearchHit]:
    """Resolve absolute paths and shortcuts on a worker, preserving search policy.

    `shortcuts` adds `%NAME%` folders beyond the environment variables.
    """
    shortcuts = shortcuts or {}
    exclusions, priorities = settings.exclusions(), settings.priorities()
    ranker = Ranker(replace(query, navigation=None, scope=None))

    def hits() -> Iterator[SearchHit]:
        for match in _path_items(query.navigation, shortcuts, exclusions, cancel):
            if cancel is not None and cancel.is_set():
                return
            item = match.item
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
                    match.reason + " · " + hit.reason
                    if ranking.reasons or hit.low_priority
                    else match.reason
                )
                yield replace(hit, reason=explanation, navigation_alias=match.alias)

    kept = heapq.nsmallest(
        settings.max_results,
        hits(),
        key=lambda h: (
            h.band,
            h.item.kind != ItemKind.FOLDER,
            (h.navigation_alias or h.item.name).casefold(),
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
    text: str,
    shortcuts: Mapping[str, Path],
    exclusions: ExclusionPolicy,
    cancel: threading.Event | None,
) -> Iterator[_NavigationMatch]:
    """Read an exact target or one directory; aliases describe only the shortcut itself."""
    if cancel is not None and cancel.is_set():
        return
    if text.startswith("%") and "%" not in text[1:]:
        yield from _folder_names(text[1:], shortcuts, cancel)
        return
    path = Path(_expand(text, shortcuts).replace("\\", os.sep)).expanduser()
    if not path.is_absolute():
        raise ValueError("The expanded shortcut must be an absolute path.")
    browsing = text.endswith(FOLDER_SEPARATORS)
    if not browsing and os.path.lexists(long_paths.os_path(path)):
        variable = _VARIABLE.fullmatch(text)
        alias = f"%{variable[1].upper()}%" if variable is not None else None
        yield _NavigationMatch(_item(path), PATH_SHORTCUT_REASON, alias)
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
                    yield _NavigationMatch(
                        _item(long_paths.ordinary_path(entry.path)), PATH_COMPLETION_REASON
                    )
                except FileNotFoundError:
                    continue  # A temporary file may disappear during enumeration.


def _folder_names(
    prefix: str, shortcuts: Mapping[str, Path], cancel: threading.Event | None
) -> Iterator[_NavigationMatch]:
    """Every `%NAME%` that starts with `prefix` and names an existing folder.

    Environment variables count only when their value is a folder, so values such
    as tokens are never shown. Names reaching the same folder appear once.
    """
    named = {name.upper(): Path(value) for name, value in os.environ.items()}
    named.update(
        (name.upper(), path) for name, path in shortcuts.items() if name.upper() not in named
    )
    seen = set()
    for name in sorted(named):
        if cancel is not None and cancel.is_set():
            return
        path = named[name]
        if not name.casefold().startswith(prefix.casefold()) or not path.is_absolute():
            continue
        key = path_key(path)
        if key not in seen and path.is_dir():
            seen.add(key)
            alias = f"%{name}%"
            yield _NavigationMatch(_item(path), f"Windows folder: {alias}", alias)


def _expand(text: str, shortcuts: Mapping[str, Path]) -> str:
    """Replace each `%NAME%` by its environment variable, else by a folder shortcut."""

    def value(match: re.Match[str]) -> str:
        name = match[1]
        if name in os.environ:
            return os.environ[name]
        if (folder := shortcuts.get(name.upper())) is not None:
            return str(folder)
        raise ValueError(f"Unknown environment variable or folder: %{name}%")

    return _VARIABLE.sub(value, text)


def _item(path: Path) -> CatalogItem:
    info = long_paths.stat(path)
    kind = ItemKind.FOLDER if stat.S_ISDIR(info.st_mode) else ItemKind.FILE
    return CatalogItem(
        path, kind, info.st_size, info.st_mtime, attributes=getattr(info, "st_file_attributes", 0)
    )
