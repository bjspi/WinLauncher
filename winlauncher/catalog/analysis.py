"""Where the catalog piles up entries nobody opens, and what would thin it out.

The analysis reads the catalog's paths and what the user ever opened, then
suggests, for the search folders as they are set now:

- a depth for a large search folder listed in full, as deep as what was opened;
- listing code projects with their top level only, when their insides dominate;
- a low priority for large folders of which nothing was ever opened.

Every suggestion says how many entries it touches and how many of those were
opened, so the user can tick what costs nothing. Nothing here changes settings.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath

from winlauncher.catalog.model import PROJECT_ATTRIBUTE, CatalogSource, path_key
from winlauncher.settings import Settings

LARGE_FOLDER = 2000  # Entries from which a search folder listed in full gets a depth.
SMALLEST_DEPTH = 2  # Never suggest listing less than a folder and its subfolders.
LARGEST_DEPTH = 6  # Deeper is no real limit.
WORTH_IT = 0.3  # A depth must leave out this share of a folder's entries.
UNOPENED_FOLDER = 1000  # Entries from which a folder nobody opens is suggested lower.
LOWERED_SUGGESTIONS = 5  # The largest such folders only.
PROJECT_INSIDES = 1000  # Entries inside projects' subfolders worth the projects switch.
LOW_WEIGHT = -20


class SuggestionKind(StrEnum):
    DEPTH = "depth"
    PROJECTS = "projects"
    LOWER = "lower"


@dataclass(frozen=True, slots=True)
class Suggestion:
    kind: SuggestionKind
    path: str  # The folder it is about; empty for the projects switch.
    entries: int  # Entries it leaves out or lowers.
    opened: int  # Of those, entries the user opened.
    levels: int = 0  # For a depth.

    @property
    def title(self) -> str:
        if self.kind == SuggestionKind.DEPTH:
            return f"List {self.path} {self.levels} levels deep"
        if self.kind == SuggestionKind.PROJECTS:
            return "List code projects with their top level only"
        return f"Rank {self.path} low"

    @property
    def detail(self) -> str:
        verb = "lowers" if self.kind == SuggestionKind.LOWER else "leaves out"
        opened = "nothing you opened" if not self.opened else f"{self.opened} you opened"
        return f"{verb} {self.entries:,} entries · {opened}"


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str  # As Windows spells it, for showing.
    key: str  # The path key: upper case, / separated, ending in /.
    attributes: int


def analyze(
    settings: Settings, entries: Iterable[tuple[str, str, int]], opened: frozenset[str]
) -> list[Suggestion]:
    """Suggestions for `entries` (path, path key, attribute bits), best first."""
    catalog = [_Entry(path, key, attributes) for path, key, attributes in entries]
    suggestions = [
        *_depths(settings, catalog, opened),
        *_projects(settings, catalog, opened),
        *_lowered(settings, catalog, opened),
    ]
    return sorted(suggestions, key=lambda s: (s.opened > 0, -s.entries))


def _depths(settings: Settings, catalog: list[_Entry], opened: frozenset[str]):
    limited = settings.folder_depth_by_key()
    for root in settings.roots:
        root_key = path_key(root)
        if root_key in limited:
            continue
        inside = [entry for entry in catalog if entry.key.startswith(root_key)]
        if len(inside) < LARGE_FOLDER:
            continue
        levels = _depth_for(root_key, [e for e in inside if e.key in opened])
        deeper = [e for e in inside if _levels(e.key, root_key) > levels]
        if len(deeper) >= WORTH_IT * len(inside):
            lost = sum(1 for e in deeper if e.key in opened)
            yield Suggestion(SuggestionKind.DEPTH, root, len(deeper), lost, levels)


def _depth_for(root_key: str, opened: list[_Entry]) -> int:
    """As deep as what was opened, within sensible bounds."""
    deepest = max((_levels(entry.key, root_key) for entry in opened), default=0)
    return min(max(deepest, SMALLEST_DEPTH + 1), LARGEST_DEPTH)


def _projects(settings: Settings, catalog: list[_Entry], opened: frozenset[str]):
    if settings.code_projects_top_level:
        return
    projects = [entry.key for entry in catalog if entry.attributes & PROJECT_ATTRIBUTE]
    roots = {path_key(root) for root in settings.roots}
    inside = [
        entry
        for entry in catalog
        if any(
            entry.key.startswith(project) and _levels(entry.key, project) >= 2
            for project in projects
            if project not in roots
        )
    ]
    if len(inside) >= PROJECT_INSIDES:
        lost = sum(1 for entry in inside if entry.key in opened)
        yield Suggestion(SuggestionKind.PROJECTS, "", len(inside), lost)


def _lowered(settings: Settings, catalog: list[_Entry], opened: frozenset[str]):
    """The largest folders below a search folder of which nothing was ever opened."""
    already = {path_key(rule.path) for rule in settings.folder_priorities}
    spelled = {entry.key: entry.path for entry in catalog}
    roots = [CatalogSource(PureWindowsPath(root)) for root in settings.roots]
    sizes: Counter[str] = Counter()
    used: set[str] = set()
    for entry in catalog:
        for folder in _ancestors_below_roots(entry.key, roots):
            sizes[folder] += 1
            if entry.key in opened:
                used.add(folder)
    chosen: list[str] = []
    for folder, count in sizes.most_common():
        if count < UNOPENED_FOLDER or len(chosen) == LOWERED_SUGGESTIONS:
            break
        if folder in used or folder in opened:
            continue
        if any(folder.startswith(ruled) for ruled in already):
            continue  # A priority rule of this folder or one above it decides already.
        if any(folder.startswith(other) or other.startswith(folder) for other in chosen):
            continue
        chosen.append(folder)
        shown = spelled.get(folder) or str(PureWindowsPath(folder.rstrip("/")))
        yield Suggestion(SuggestionKind.LOWER, shown, count, 0)


def _ancestors_below_roots(key: str, roots: list[CatalogSource]) -> Iterable[str]:
    """The folders containing `key` strictly below a search folder, not the root itself."""
    for source in roots:
        root_key = path_key(source.root)
        if key.startswith(root_key) and key != root_key:
            parts = key[len(root_key) :].rstrip("/").split("/")[:-1]
            for index in range(len(parts)):
                yield root_key + "/".join(parts[: index + 1]) + "/"
            return


def _levels(key: str, root_key: str) -> int:
    return key[len(root_key) :].count("/")
