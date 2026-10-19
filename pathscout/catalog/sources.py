"""Which directory trees the catalog owns, and which source owns a given path."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pathscout.catalog.model import CatalogSource, is_within, path_key
from pathscout.settings import Settings


def catalog_sources(settings: Settings, application_roots: Iterable[Path]) -> list[CatalogSource]:
    """Configured roots plus Start-menu roots, without sources another one covers."""
    candidates = [CatalogSource(Path(root)) for root in settings.roots]
    if settings.index_apps:
        candidates.extend(CatalogSource(root, apps_only=True) for root in application_roots)
    unique = list(dict.fromkeys(candidates))
    return [source for source in unique if not any(other.covers(source) for other in unique)]


def owning_source(folder: Path, sources: Iterable[CatalogSource]) -> CatalogSource:
    """Full sources win over app-only sources; then the most specific root wins.

    A folder outside every configured source (a broad Explorer scope) owns itself.
    """
    owners = [source for source in sources if is_within(folder, source.root)]
    if not owners:
        return CatalogSource(folder)
    return min(owners, key=lambda source: (source.apps_only, -len(path_key(source.root))))
