"""Evaluate a proposed rule against the catalog without touching files or settings."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.model import APPLICATION_SOURCE_PREFIX, REGISTRY_SOURCE, ItemKind
from winlauncher.catalog.store import CatalogStore

PREVIEW_ENTRY_LIMIT = 2_000
YIELD_EVERY_ENTRIES = 256
YIELD_SECONDS = 0.001


@dataclass(frozen=True, slots=True)
class ExclusionMatch:
    """One affected catalog entry; inherited matches belong to an excluded folder."""

    path: Path
    kind: ItemKind
    inherited: bool


@dataclass(frozen=True, slots=True)
class ExclusionPreview:
    """Exact totals, with a bounded sample to keep the preview tree responsive."""

    matches: tuple[ExclusionMatch, ...]
    files: int
    folders: int

    @property
    def total(self) -> int:
        return self.files + self.folders


def preview_exclusion(
    store: CatalogStore,
    pattern: str,
    cancelled: Event,
    *,
    limit: int = PREVIEW_ENTRY_LIMIT,
) -> ExclusionPreview | None:
    """Check this rule alone using search's folder and application semantics.

    The catalog already omits previously excluded trees. Marker probes are disabled:
    this is a read-only catalog preview, including entries whose disks are offline.
    Cancellation discards partial results. Totals include matches beyond `limit`.
    """
    if cancelled.is_set():
        return None
    policy = ExclusionPolicy(patterns=(pattern.strip(),), markers=())
    matches: list[ExclusionMatch] = []
    files = folders = 0
    with store.connect() as connection:
        rows = connection.execute("SELECT path, kind, root FROM entries ORDER BY path_key")
        for position, row in enumerate(rows):
            if cancelled.is_set():
                return None
            if position % YIELD_EVERY_ENTRIES == 0:
                time.sleep(YIELD_SECONDS)
            path = Path(row["path"])
            kind = ItemKind(row["kind"])
            directory = kind == ItemKind.FOLDER
            source = row["root"]
            application = source == REGISTRY_SOURCE or source.startswith(APPLICATION_SOURCE_PREFIX)
            if policy.allows(path, directory=directory, application=application):
                continue
            if directory:
                folders += 1
            else:
                files += 1
            if len(matches) < limit:
                inherited = not policy.allows_contents(path.parent)
                matches.append(ExclusionMatch(path, kind, inherited))
    if cancelled.is_set():
        return None
    return ExclusionPreview(tuple(matches), files, folders)
