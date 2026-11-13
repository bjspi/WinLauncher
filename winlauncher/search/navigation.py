"""Direct navigation: absolute paths and `%NAME%`, `shell:` and `~` shortcuts.

`SearchQuery.navigation` holds one typed path. It resolves to the exact target,
to the direct entries of one folder (trailing separator) or to the entries whose
names start with the last component. `%` plus a name prefix suggests folder
variables instead, and `shell:` plus a name prefix the Windows shell folders.
Nothing is indexed. Everything runs on a search worker and obeys the exclusion,
priority, type and limit rules of catalog results. The catalog only contributes
favorite flags; nothing is added to it.

Network shares and disconnected drives can block a filesystem call for many
seconds. Every drive or share root is asked through `storage.drive_state` first,
so a dead server cannot hold a search worker.
"""

from __future__ import annotations

import heapq
import os
import re
import stat
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from winlauncher.catalog import long_paths, storage
from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.indexer import catalog_item, classify_kind
from winlauncher.catalog.model import FOLDER_SEPARATORS, CatalogItem, ItemKind, normalize, path_key
from winlauncher.catalog.priority import PriorityPolicy
from winlauncher.catalog.store import CatalogStore, Usage
from winlauncher.search.candidates import Candidate
from winlauncher.search.query import (
    SHELL_FOLDER_PREFIX,
    VARIABLE_MARK,
    SearchQuery,
    is_shell_folder_path,
    shell_folder_name_prefix,
    split_at_first_separator,
    variable_name_prefix,
)
from winlauncher.search.ranking import REASON_SEPARATOR, PriorityBand, Ranker, Ranking, SearchHit
from winlauncher.settings import Settings

_VARIABLE = re.compile(r"%([^%]+)%")
_DIRECT_PATH_REASON = "Direct path"
_PATH_COMPLETION_REASON = "Path completion"
_FOLDER_VARIABLE_REASON = "Windows folder variable"
_SHELL_FOLDER_REASON = "Windows shell folder"


@dataclass(frozen=True, slots=True)
class FolderNames:
    """Names navigation resolves beside environment variables; the platform finds them.

    `variables`: folders `%NAME%` reaches, by uppercase name. Where several names
    reach one folder, suggestions show the first, after the environment's names.
    `shell`: what each `shell:` name opens, by its registered name: a folder, or
    a file such as the shortcut to a virtual folder.
    """

    variables: Mapping[str, Path] = field(default_factory=dict)
    shell: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _NavigationMatch:
    """A filesystem target found by navigation, before search policy is applied.

    `label` replaces the file name as the row title; only named targets have one,
    such as `%APPDATA%` or `shell:Startup`. The user asked for a named target by
    its name, so exclusions do not hide it; its contents still obey them.
    """

    item: CatalogItem
    reason: str
    label: str | None = None

    @property
    def named(self) -> bool:
        return self.label is not None


def search_paths(
    query: SearchQuery,
    settings: Settings,
    cancel: threading.Event | None,
    store: CatalogStore,
    names: FolderNames | None = None,
) -> list[SearchHit]:
    """Resolve `query.navigation` on a worker; results obey the catalog's search policy.

    `names` adds the Windows known folders to the environment variables. Raises
    ValueError with a user-facing message for an unknown or relative `%NAME%`, an
    unknown `shell:` name, an unresponsive drive or share and an unreadable folder.
    """
    if (navigation := query.navigation) is None:
        raise ValueError("The query does not navigate to a path.")
    exclusions, priorities = settings.exclusions(), settings.priorities()
    ranker = Ranker(replace(query, navigation=None, scope=None))
    lists_names = _lists_folder_names(navigation)

    def shown_hits() -> Iterator[SearchHit]:
        matches = _navigation_matches(navigation, names or FolderNames(), exclusions, cancel)
        if lists_names:
            matches = _with_usage(list(matches), store)
        for match in matches:
            if _cancelled(cancel):
                return
            if (hit := _ranked_hit(match, ranker, priorities, exclusions)) is not None:
                yield hit

    order = _name_order if lists_names else _display_order
    kept = heapq.nsmallest(settings.max_results, shown_hits(), key=order)
    if _cancelled(cancel):
        return []
    return _with_favorites(kept, store)


def _ranked_hit(
    match: _NavigationMatch,
    ranker: Ranker,
    priorities: PriorityPolicy,
    exclusions: ExclusionPolicy,
) -> SearchHit | None:
    """`match` as a hit, if type, priority and exclusion rules let it be shown.

    Exclusions do not apply to a named target.
    """
    item = match.item
    candidate = Candidate.from_item(item)
    priority = priorities.evaluate(candidate)
    if not ranker.eligible(candidate, priority):
        return None
    directory = item.kind == ItemKind.FOLDER
    if not match.named and not exclusions.allows(item.path, directory=directory):
        return None
    ranking = ranker.rank(candidate, priority)
    if ranking is None:
        return None
    hit = ranking.hit(item)
    return replace(hit, reason=_explanation(match, ranking, hit), label=match.label)


def _explanation(match: _NavigationMatch, ranking: Ranking, hit: SearchHit) -> str:
    """The navigation reason, followed by ranking reasons only when there are any."""
    if not ranking.reasons and not hit.low_priority:
        return match.reason
    return REASON_SEPARATOR.join((match.reason, hit.reason))


def _display_order(hit: SearchHit) -> tuple[PriorityBand, bool, str, str]:
    """Priority band, folders before files, then the displayed title."""
    return (hit.band, hit.item.kind != ItemKind.FOLDER, hit.title.casefold(), str(hit.item.path))


def _name_order(hit: SearchHit) -> tuple[PriorityBand, bool, float, str, str]:
    """Priority band, favorites, the most recently opened, then the displayed name.

    A list of folder names is ranked like other results: what the user opens
    comes first, the rest stays alphabetical by name, not by target folder.
    """
    item = hit.item
    return (hit.band, not item.pinned, -item.last_used, hit.title.casefold(), str(item.path))


def _lists_folder_names(text: str) -> bool:
    """`%APP` or `shell:Sta`: the folder names that start with the typed text."""
    return variable_name_prefix(text) is not None or shell_folder_name_prefix(text) is not None


def _with_usage(matches: list[_NavigationMatch], store: CatalogStore) -> list[_NavigationMatch]:
    """`matches` with how often and when their targets were opened, and favorites."""
    keys = [match.item.identity for match in matches]
    usage, favorites = store.usage(keys), store.favorite_keys(keys)
    return [_used_match(match, usage.get(match.item.identity), favorites) for match in matches]


def _used_match(
    match: _NavigationMatch, usage: Usage | None, favorites: set[str]
) -> _NavigationMatch:
    item = match.item
    usage = usage or Usage(item.launches, item.last_used)
    pinned = item.identity in favorites
    used = replace(item, launches=usage.launches, last_used=usage.last_used, pinned=pinned)
    return replace(match, item=used)


def _with_favorites(hits: list[SearchHit], store: CatalogStore) -> list[SearchHit]:
    """`hits` with their favorite flags; navigation targets need no catalog entry."""
    if not hits:
        return hits
    favorites = store.favorite_keys(hit.item.identity for hit in hits)
    return [
        replace(hit, item=replace(hit.item, pinned=hit.item.identity in favorites)) for hit in hits
    ]


def _navigation_matches(
    text: str,
    names: FolderNames,
    exclusions: ExclusionPolicy,
    cancel: threading.Event | None,
) -> Iterator[_NavigationMatch]:
    """Folder variables for `%prefix`, shell folders for `shell:prefix`, else the
    exact target, the folder's entries (trailing separator) or the entries
    completing the last path component.

    An excluded folder is never read, and an exact target needs no enumeration.
    A named target, such as `%STARTUP%` in an excluded folder, is still shown.
    """
    if _cancelled(cancel):
        return
    if (prefix := variable_name_prefix(text)) is not None:
        yield from _folder_variables(prefix, names.variables, cancel)
        return
    if (prefix := shell_folder_name_prefix(text)) is not None:
        yield from _shell_folders(prefix, names.shell, cancel)
        return
    path = _resolve(text, names)
    _require_responsive_root(path)
    browsing = text.endswith(FOLDER_SEPARATORS)
    if not browsing and _VARIABLE.fullmatch(text):
        if (target := _exact_target(text, path)) is not None:
            yield target
        return
    folder = path if browsing else path.parent
    if not exclusions.allows_contents(folder):
        return
    if not browsing and os.path.lexists(long_paths.os_path(path)):
        if (target := _exact_target(text, path)) is not None:
            yield target
        return
    yield from _folder_entries(folder, "" if browsing else normalize(path.name), cancel)


def _exact_target(text: str, path: Path) -> _NavigationMatch | None:
    """The typed target itself, labeled when `text` is exactly one `%NAME%`.

    A dangling link has no target.
    """
    try:
        item = _item(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _read_error(path, error) from error
    if (variable := _VARIABLE.fullmatch(text)) is None:
        return _NavigationMatch(item, _DIRECT_PATH_REASON)
    return _NavigationMatch(item, _FOLDER_VARIABLE_REASON, _variable_label(variable[1]))


def _folder_entries(
    folder: Path, prefix: str, cancel: threading.Event | None
) -> Iterator[_NavigationMatch]:
    """Direct entries of `folder` whose normalized names start with `prefix`.

    A missing folder has no entries; an unreadable one is reported to the user.
    """
    if not os.path.isdir(long_paths.os_path(folder)):
        return
    try:
        entries = long_paths.scandir(folder)
    except OSError as error:
        raise _read_error(folder, error) from error
    with entries:
        for entry in entries:
            if _cancelled(cancel):
                return
            if not normalize(entry.name).startswith(prefix):
                continue
            if (item := _entry_item(entry)) is not None:
                yield _NavigationMatch(item, _PATH_COMPLETION_REASON)


def _entry_item(entry: os.DirEntry[str]) -> CatalogItem | None:
    """The entry's item; it may vanish or deny access during enumeration."""
    try:
        return _item(long_paths.ordinary_path(entry.path))
    except OSError:
        return None


def _folder_variables(
    prefix: str, variables: Mapping[str, Path], cancel: threading.Event | None
) -> Iterator[_NavigationMatch]:
    """Every `%NAME%` that starts with `prefix` and names an existing folder.

    Environment variables count only when their value is a folder, so values such
    as tokens are never shown.
    """
    named = (
        (_variable_label(name), path)
        for name, path in _named_folders(variables).items()
        if _starts_with(name, prefix)
    )
    for label, path in _reachable_targets(named, _is_reachable_folder, cancel):
        yield _NavigationMatch(_item(path), _FOLDER_VARIABLE_REASON, label)


def _shell_folders(
    prefix: str, shell: Mapping[str, Path], cancel: threading.Event | None
) -> Iterator[_NavigationMatch]:
    """Every `shell:` name that starts with `prefix` and opens something existing."""
    named = (
        (_shell_label(name), path)
        for name, path in sorted(shell.items(), key=lambda entry: entry[0].casefold())
        if _starts_with(name, prefix)
    )
    for label, path in _reachable_targets(named, _is_reachable_target, cancel):
        yield _NavigationMatch(_item(path), _SHELL_FOLDER_REASON, label)


def _reachable_targets(
    named: Iterable[tuple[str, Path]],
    reachable: Callable[[Path], bool],
    cancel: threading.Event | None,
) -> Iterator[tuple[str, Path]]:
    """The first (label, target) per target that is absolute and `reachable`.

    Targets on an unresponsive drive or share are left out.
    """
    seen: set[str] = set()
    for label, path in named:
        if _cancelled(cancel):
            return
        if not path.is_absolute() or (key := path_key(path)) in seen or not reachable(path):
            continue
        seen.add(key)
        yield label, path


def _named_folders(variables: Mapping[str, Path]) -> dict[str, Path]:
    """Environment variables sorted by name, then the other folder names in order.

    All by uppercase name; a variable wins over a folder of the same name.
    """
    named = {name.upper(): Path(value) for name, value in sorted(os.environ.items())}
    for name, path in variables.items():
        named.setdefault(name.upper(), path)
    return named


def _starts_with(name: str, prefix: str) -> bool:
    return name.casefold().startswith(prefix.casefold())


def _is_reachable_folder(path: Path) -> bool:
    return _is_reachable_target(path) and path.is_dir()


def _is_reachable_target(path: Path) -> bool:
    return storage.drive_state(path) is storage.DriveState.PRESENT and path.exists()


def _resolve(text: str, names: FolderNames) -> Path:
    """`text` with its `shell:` name, `%NAME%` or leading `~` expanded.

    The result must be absolute.
    """
    if is_shell_folder_path(text):
        path = _expand_shell_folder(text, names.shell)
    else:
        path = Path(_expand_variables(text, names.variables)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{text} must expand to an absolute path.")
    return path


def _expand_shell_folder(text: str, shell: Mapping[str, Path]) -> Path:
    r"""`shell:Name\rest` as the path of what `Name` opens, followed by `rest`."""
    name, rest = split_at_first_separator(text[len(SHELL_FOLDER_PREFIX) :])
    by_name = {registered.casefold(): path for registered, path in shell.items()}
    if (target := by_name.get(name.casefold())) is None:
        raise ValueError(f"Unknown shell folder: {_shell_label(name)}")
    return target / rest


def _expand_variables(text: str, variables: Mapping[str, Path]) -> str:
    """Replace each `%NAME%` by its environment variable, else by a named folder."""

    def value(match: re.Match[str]) -> str:
        name = match[1]
        if name in os.environ:
            return os.environ[name]
        if (folder := variables.get(name.upper())) is not None:
            return str(folder)
        raise ValueError(f"Unknown environment variable or folder: {_variable_label(name)}")

    return _VARIABLE.sub(value, text)


def _require_responsive_root(path: Path) -> None:
    if (state := storage.drive_state(path)) is storage.DriveState.UNRESPONSIVE:
        raise ValueError(storage.unavailable_message(path, state))


def _read_error(path: Path, error: OSError) -> ValueError:
    return ValueError(f"Cannot read {path}: {error.strerror or error}")


def _variable_label(name: str) -> str:
    return f"{VARIABLE_MARK}{name.upper()}{VARIABLE_MARK}"


def _shell_label(name: str) -> str:
    return SHELL_FOLDER_PREFIX + name


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _item(path: Path) -> CatalogItem:
    """A catalog value for `path` read live from the filesystem; it stays unstored."""
    info = long_paths.stat(path)
    return catalog_item(path, info, classify_kind(path, stat.S_ISDIR(info.st_mode)))
