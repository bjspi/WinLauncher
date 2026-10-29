"""What one launcher row shows: a catalog hit, a website or a plugin's item to open.

`result_row` is the only place that distinguishes them for text. Icons and
actions that exist for one kind only dispatch on `ResultRow.hit`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from PySide6.QtCore import QLocale

from winlauncher.catalog.model import CatalogItem
from winlauncher.plugins.search import PluginHit
from winlauncher.search.query import SearchQuery
from winlauncher.search.ranking import SearchHit
from winlauncher.websites import WebsiteTarget

LauncherHit: TypeAlias = SearchHit | WebsiteTarget | PluginHit
WEBSITE_REASON = "Opens in your default browser"
MISSING_LABEL = "Deleted"


@dataclass(frozen=True, slots=True)
class ResultRow:
    hit: LauncherHit
    name: str
    location: str  # Muted text beside or below the name.
    target: str  # The path or URL; copied, and identifies the row across refreshes.
    target_noun: str  # "Path", "URL" or "Link", for messages about `target`.
    detail: str
    tooltip: str
    accessible_text: str
    metadata: str = ""  # Size or date, shown when the query filters by them.
    pinned: bool = False
    # In an Explorer search: the entry lies directly in the searched folder.
    direct: bool = False
    # The first subfolder match after the direct entries; a line separates them.
    starts_subfolders: bool = False
    missing: bool = False  # The file is gone; the catalog has not noticed yet.
    opens_in: str = ""  # Where a website or plugin item opens: "browser", "Telegram".

    @property
    def file_hit(self) -> SearchHit | None:
        """The catalog hit, for actions that only files and folders have."""
        return self.hit if isinstance(self.hit, SearchHit) else None


def result_row(
    hit: LauncherHit, query: SearchQuery | None, scope: Path | None, *, missing: bool = False
) -> ResultRow:
    """Describe `hit`; locations are relative to `scope` in an Explorer search."""
    if isinstance(hit, WebsiteTarget):
        return _website_row(hit)
    if isinstance(hit, PluginHit):
        return _plugin_row(hit)
    row = _file_row(hit, query, scope)
    return _as_missing(row) if missing else row


def _website_row(website: WebsiteTarget) -> ResultRow:
    return ResultRow(
        hit=website,
        name=website.name,
        location=f"{website.keyword}  ·  {website.url}",
        target=website.url,
        target_noun="URL",
        detail=f"{website.url}\n{WEBSITE_REASON}",
        tooltip=f"{website.url}\n{WEBSITE_REASON}",
        accessible_text=f"{website.name}, website, keyword {website.keyword}\n{website.url}",
        opens_in="browser",
    )


def _plugin_row(hit: PluginHit) -> ResultRow:
    item = hit.item
    tooltip = "\n".join(part for part in (item.target, f"Opens in {hit.label}", hit.reason) if part)
    return ResultRow(
        hit=hit,
        name=hit.title,
        location=item.subtitle,
        target=item.target or f"{hit.plugin_id}:{item.key}",
        target_noun="Link",
        detail=tooltip,
        tooltip=tooltip,
        accessible_text=f"{hit.title}, {hit.label}\n{item.subtitle}".rstrip(),
        opens_in=hit.label,
    )


@dataclass(frozen=True, slots=True)
class _FilePlace:
    """Where a file row says its item is: muted location and full description."""

    location: str
    description: str


def _file_place(hit: SearchHit, scope: Path | None) -> _FilePlace:
    path = hit.item.path
    if hit.label is None:
        return _FilePlace(_location(path.parent, scope), str(path))
    # The label hides the target's own name, so the full target path is shown instead.
    return _FilePlace(str(path), f"{hit.label}\n{path}")


def _file_row(hit: SearchHit, query: SearchQuery | None, scope: Path | None) -> ResultRow:
    """A catalog or navigation hit; actions always use the real target path."""
    item = hit.item
    place = _file_place(hit, scope)
    metadata = _metadata(item, query)
    size = QLocale().formattedDataSize(item.size)
    favorite = ", favorite" if item.pinned else ""
    details = f"\n{metadata}" if metadata else ""
    modified = format_modified(item.modified)
    return ResultRow(
        hit=hit,
        name=hit.title,
        location=place.location,
        target=str(item.path),
        target_noun="Path",
        detail=f"{place.description}\n{size}  ·  Modified {modified}  ·  {hit.reason}",
        tooltip=f"{place.description}\n{hit.reason}\nRelevance: {hit.score:.1f}",
        accessible_text=f"{hit.title}, {item.kind.value}{favorite}{details}\n{item.path}",
        metadata=metadata,
        pinned=item.pinned,
        direct=scope is not None and item.path.parent == scope,
    )


def _as_missing(row: ResultRow) -> ResultRow:
    """A found file that no longer exists: shown dimmed and labeled, never opened."""
    location = f"{MISSING_LABEL}  ·  {row.location}" if row.location else MISSING_LABEL
    return replace(
        row,
        missing=True,
        location=location,
        tooltip=f"{row.target}\nNo longer exists",
        accessible_text=f"{MISSING_LABEL}: {row.accessible_text}",
    )


def _location(parent: Path, scope: Path | None) -> str:
    """In an Explorer search, the subfolder below the searched folder; else the folder."""
    if scope is None:
        return str(parent)
    if parent == scope:
        return ""
    try:
        return str(parent.relative_to(scope))
    except ValueError:
        return str(parent)


def _metadata(item: CatalogItem, query: SearchQuery | None) -> str:
    if query is None:
        return ""
    parts = []
    if query.min_size is not None or query.max_size is not None:
        parts.append(QLocale().formattedDataSize(item.size))
    if query.modified_after is not None or query.modified_before is not None:
        parts.append(f"Modified {format_modified(item.modified)}")
    return " · ".join(parts)


def format_modified(timestamp: float) -> str:
    if not timestamp:
        return "Unknown"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "Unknown"  # Outside the range Windows can represent.
