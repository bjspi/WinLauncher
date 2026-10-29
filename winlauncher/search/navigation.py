"""Direct navigation: absolute paths and `%NAME%`/`~` shortcuts, without indexing.

`SearchQuery.navigation` holds one typed path. It resolves to the exact target,
to the direct entries of one folder (trailing separator) or to the entries whose
names start with the last component. `%` plus a name prefix suggests folder
variables instead. Everything runs on a search worker and obeys the exclusion,
priority, type and limit rules of catalog results. The catalog only contributes
favorite flags; nothing is added to it.

Network shares and disconnected drives can block a filesystem call for many
seconds. `_RootProbe` asks each drive or share root on a daemon thread and waits
at most `ROOT_PROBE_TIMEOUT_S`, so a dead server cannot hold a search worker.
"""

from __future__ import annotations

import heapq
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from winlauncher.catalog import long_paths
from winlauncher.catalog.exclusions import ExclusionPolicy
from winlauncher.catalog.model import FOLDER_SEPARATORS, CatalogItem, ItemKind, normalize, path_key
from winlauncher.catalog.priority import PriorityPolicy
from winlauncher.catalog.store import CatalogStore
from winlauncher.search.candidates import Candidate
from winlauncher.search.query import SearchQuery
from winlauncher.search.ranking import REASON_SEPARATOR, PriorityBand, Ranker, Ranking, SearchHit
from winlauncher.settings import Settings

ROOT_PROBE_TIMEOUT_S = 2.0
_VARIABLE_MARK = "%"
_VARIABLE = re.compile(r"%([^%]+)%")
_DIRECT_PATH_REASON = "Direct path"
_PATH_COMPLETION_REASON = "Path completion"
_FOLDER_VARIABLE_REASON = "Windows folder variable"


@dataclass(frozen=True, slots=True)
class _NavigationMatch:
    """A filesystem target found by navigation, before search policy is applied.

    `label` replaces the file name as the row title; only `%NAME%` targets have one.
    """

    item: CatalogItem
    reason: str
    label: str | None = None


class RootState(StrEnum):
    """What a drive or share root answered within the probe timeout."""

    PRESENT = "present"
    MISSING = "missing"
    UNRESPONSIVE = "unresponsive"


@dataclass(slots=True)
class _Probe:
    """One running root check; every waiter for the same root shares it."""

    done: threading.Event = field(default_factory=threading.Event)
    exists: bool = False


class _RootProbe:
    """Checks drive and share roots on daemon threads with a bounded wait.

    At most one check per root runs at a time, so typing a path to a dead server
    leaves one hanging thread, not one per keystroke. Results are not cached: a
    share that comes back answers at the next keystroke.
    """

    def __init__(self, timeout_s: float, exists: Callable[[str], bool] = os.path.isdir) -> None:
        self._timeout_s = timeout_s
        self._exists = exists
        self._lock = threading.Lock()
        self._running: dict[str, _Probe] = {}

    def state(self, path: Path) -> RootState:
        probe = self._probe(path.anchor)
        if not probe.done.wait(self._timeout_s):
            return RootState.UNRESPONSIVE
        return RootState.PRESENT if probe.exists else RootState.MISSING

    def _probe(self, root: str) -> _Probe:
        key = path_key(root)
        with self._lock:
            if (probe := self._running.get(key)) is None:
                probe = self._running[key] = _Probe()
                threading.Thread(
                    target=self._check, args=(root, key, probe), name="root_probe", daemon=True
                ).start()
        return probe

    def _check(self, root: str, key: str, probe: _Probe) -> None:
        try:
            probe.exists = self._exists(root)
        finally:
            with self._lock:
                del self._running[key]
            probe.done.set()


_ROOTS = _RootProbe(ROOT_PROBE_TIMEOUT_S)


def search_paths(
    query: SearchQuery,
    settings: Settings,
    cancel: threading.Event | None,
    store: CatalogStore,
    shortcuts: Mapping[str, Path] | None = None,
) -> list[SearchHit]:
    """Resolve `query.navigation` on a worker; results obey the catalog's search policy.

    `shortcuts` adds `%NAME%` folders beyond the environment variables, such as
    the Windows known folders. Raises ValueError with a user-facing message for an
    unknown or relative `%NAME%`, an unresponsive drive or share and an
    unreadable folder.
    """
    if (navigation := query.navigation) is None:
        raise ValueError("The query does not navigate to a path.")
    exclusions, priorities = settings.exclusions(), settings.priorities()
    ranker = Ranker(replace(query, navigation=None, scope=None))

    def shown_hits() -> Iterator[SearchHit]:
        matches = _navigation_matches(navigation, shortcuts or {}, exclusions, cancel)
        for match in matches:
            if _cancelled(cancel):
                return
            if (hit := _ranked_hit(match, ranker, priorities, exclusions)) is not None:
                yield hit

    kept = heapq.nsmallest(settings.max_results, shown_hits(), key=_display_order)
    if _cancelled(cancel):
        return []
    return _with_favorites(kept, store)


def _ranked_hit(
    match: _NavigationMatch,
    ranker: Ranker,
    priorities: PriorityPolicy,
    exclusions: ExclusionPolicy,
) -> SearchHit | None:
    """`match` as a hit, if type, priority and exclusion rules let it be shown."""
    item = match.item
    candidate = Candidate.from_item(item)
    priority = priorities.evaluate(candidate)
    if not ranker.eligible(candidate, priority):
        return None
    if not exclusions.allows(item.path, directory=item.kind == ItemKind.FOLDER):
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
    """Priority band, folders before files, then the displayed title.

    Sorting by title keeps `%NAME%` suggestions alphabetical although their target
    folders have unrelated names.
    """
    return (hit.band, hit.item.kind != ItemKind.FOLDER, hit.title.casefold(), str(hit.item.path))


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
    shortcuts: Mapping[str, Path],
    exclusions: ExclusionPolicy,
    cancel: threading.Event | None,
) -> Iterator[_NavigationMatch]:
    """Folder variables for `%prefix`, else the exact target, the folder's entries
    (trailing separator) or the entries completing the last path component.

    An excluded folder is never read, and an exact target needs no enumeration.
    """
    if _cancelled(cancel):
        return
    if _is_variable_prefix(text):
        yield from _folder_variables(text.removeprefix(_VARIABLE_MARK), shortcuts, cancel)
        return
    path = _resolve(text, shortcuts)
    _require_responsive_root(path)
    browsing = text.endswith(FOLDER_SEPARATORS)
    folder = path if browsing else path.parent
    if not exclusions.allows(folder, directory=True):
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
    prefix: str, shortcuts: Mapping[str, Path], cancel: threading.Event | None
) -> Iterator[_NavigationMatch]:
    """Every `%NAME%` that starts with `prefix` and names an existing folder.

    Environment variables count only when their value is a folder, so values such
    as tokens are never shown. Names reaching the same folder appear once, and
    folders on an unresponsive drive or share are left out.
    """
    seen: set[str] = set()
    for name, path in sorted(_named_folders(shortcuts).items()):
        if _cancelled(cancel):
            return
        if not name.casefold().startswith(prefix.casefold()) or not path.is_absolute():
            continue
        key = path_key(path)
        if key in seen or not _is_reachable_folder(path):
            continue
        seen.add(key)
        yield _NavigationMatch(_item(path), _FOLDER_VARIABLE_REASON, _variable_label(name))


def _named_folders(shortcuts: Mapping[str, Path]) -> dict[str, Path]:
    """Environment variables and folder shortcuts by uppercase name; variables win."""
    named = {name.upper(): Path(value) for name, value in os.environ.items()}
    for name, path in shortcuts.items():
        named.setdefault(name.upper(), path)
    return named


def _is_reachable_folder(path: Path) -> bool:
    return _ROOTS.state(path) is RootState.PRESENT and path.is_dir()


def _resolve(text: str, shortcuts: Mapping[str, Path]) -> Path:
    """`text` with `%NAME%` and a leading `~` expanded; the result must be absolute."""
    path = Path(_expand_variables(text, shortcuts)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{text} must expand to an absolute path.")
    return path


def _expand_variables(text: str, shortcuts: Mapping[str, Path]) -> str:
    """Replace each `%NAME%` by its environment variable, else by a folder shortcut."""

    def value(match: re.Match[str]) -> str:
        name = match[1]
        if name in os.environ:
            return os.environ[name]
        if (folder := shortcuts.get(name.upper())) is not None:
            return str(folder)
        raise ValueError(f"Unknown environment variable or folder: {_variable_label(name)}")

    return _VARIABLE.sub(value, text)


def _require_responsive_root(path: Path) -> None:
    if _ROOTS.state(path) is RootState.UNRESPONSIVE:
        raise ValueError(f"{path.anchor} does not respond. Check the network or drive connection.")


def _read_error(path: Path, error: OSError) -> ValueError:
    return ValueError(f"Cannot read {path}: {error.strerror or error}")


def _is_variable_prefix(text: str) -> bool:
    """`%` or `%APP`: a folder variable name still being typed."""
    return text.startswith(_VARIABLE_MARK) and _VARIABLE_MARK not in text[1:]


def _variable_label(name: str) -> str:
    return f"{_VARIABLE_MARK}{name.upper()}{_VARIABLE_MARK}"


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _item(path: Path) -> CatalogItem:
    """A catalog value for `path` read live from the filesystem; it stays unstored."""
    info = long_paths.stat(path)
    kind = ItemKind.FOLDER if stat.S_ISDIR(info.st_mode) else ItemKind.FILE
    return CatalogItem(path, kind, info.st_size, info.st_mtime, attributes=info.st_file_attributes)
