"""Exclusion rules shared by indexing, change monitoring and search."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from pathscout.catalog import long_paths
from pathscout.catalog.model import normalize, path_key

DEFAULT_MARKER_FILES = (
    ".nobackup",
    ".no-backup",
    # Python and Conda environments under any folder name.
    "pyvenv.cfg",
    "conda-meta",
)
_GLOB_CHARACTERS = frozenset("*?[")
_SEPARATORS = re.compile(r"[\\/]+")
_ANY_FOLDERS = "(?:/[^/]+)*"


@dataclass(slots=True)
class ExclusionPolicy:
    """Excluded paths, file/folder name patterns and marker files.

    A marker file excludes its directory and the entire subtree. Marker probes
    are cached per policy instance, so create a fresh policy per scan, search or
    notification batch (see `fresh`).

    If a marker cannot be probed, indexing must keep the previous snapshot of
    that subtree (`raise_marker_errors=True` propagates the OSError), while
    search fails closed and treats the directory as excluded.
    """

    paths: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    markers: tuple[str, ...] = DEFAULT_MARKER_FILES
    raise_marker_errors: bool = False
    _excluded_keys: tuple[str, ...] = field(init=False, repr=False)
    _plain_names: frozenset[str] = field(init=False, repr=False)
    _name_globs: tuple[re.Pattern[str], ...] = field(init=False, repr=False)
    _path_globs: tuple[re.Pattern[str], ...] = field(init=False, repr=False)
    _directory_cache: dict[Path, bool] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._excluded_keys = tuple(path_key(p) for p in self.paths)
        patterns = [normalize(p).replace("\\", "/") for p in self.patterns]
        path_patterns = [p for p in patterns if "/" in p]
        name_patterns = [p for p in patterns if "/" not in p]
        self._plain_names = frozenset(p for p in name_patterns if not _is_glob(p))
        self._name_globs = tuple(_compile_glob(p) for p in name_patterns if _is_glob(p))
        self._path_globs = tuple(_compile_path_glob(p) for p in path_patterns)

    def fresh(self) -> ExclusionPolicy:
        """The same rules with an empty marker cache."""
        return ExclusionPolicy(self.paths, self.patterns, self.markers, self.raise_marker_errors)

    def allows(self, path: Path, *, directory: bool = False, application: bool = False) -> bool:
        """`application` marks Start-menu and App Paths entries: file-name patterns such
        as `*.lnk` never hide them, while folder rules and markers still apply."""
        if self._excluded_keys and path_key(path).startswith(self._excluded_keys):
            return False
        if not directory and not application and self._matches_pattern(path):
            return False
        return self._allows_directory(path if directory else path.parent)

    def _allows_directory(self, folder: Path) -> bool:
        cached = self._directory_cache.get(folder)
        if cached is not None:
            return cached
        allowed = (
            self._allows_parent(folder)
            and not self._matches_pattern(folder)
            and not self._has_marker(folder)
        )
        self._directory_cache[folder] = allowed
        return allowed

    def _allows_parent(self, folder: Path) -> bool:
        return folder.parent == folder or self._allows_directory(folder.parent)

    def _matches_pattern(self, folder: Path) -> bool:
        name = normalize(folder.name)
        if name in self._plain_names or any(glob.fullmatch(name) for glob in self._name_globs):
            return True
        if not self._path_globs:
            return False
        path = _SEPARATORS.sub("/", "/" + normalize(str(folder))).rstrip("/")
        return any(glob.fullmatch(path) for glob in self._path_globs)

    def _has_marker(self, folder: Path) -> bool:
        for marker in self.markers:
            try:
                long_paths.stat(folder / marker)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                if self.raise_marker_errors:
                    raise
                return True  # Fail closed: an unreadable marker may exist.
            return True
        return False


def _is_glob(pattern: str) -> bool:
    return not _GLOB_CHARACTERS.isdisjoint(pattern)


def _compile_glob(pattern: str) -> re.Pattern[str]:
    return re.compile(fnmatch.translate(pattern))


def _compile_path_glob(pattern: str) -> re.Pattern[str]:
    """Match a path pattern against `/`-prefixed paths such as `/c:/work/app`.

    `*`, `?` and `[…]` stay within one folder name and `**` spans any number of
    folders. Patterns starting with a drive or a separator are anchored at the
    root; others match the trailing folders of a path, so `android/**/build`
    excludes every `build` folder somewhere below any `android` folder.
    """
    pattern = _SEPARATORS.sub("/", pattern).rstrip("/")
    segments = pattern.strip("/").split("/")
    anchored = pattern.startswith("/") or segments[0].endswith(":")
    regex = "" if anchored else _ANY_FOLDERS
    for segment in segments:
        regex += _ANY_FOLDERS if segment == "**" else "/" + _segment_regex(segment)
    return re.compile(regex)


def _segment_regex(segment: str) -> str:
    """fnmatch semantics for one folder name; wildcards never match `/`."""
    regex, index = [], 0
    while index < len(segment):
        char = segment[index]
        index += 1
        if char == "*":
            regex.append("[^/]*")
        elif char == "?":
            regex.append("[^/]")
        elif char == "[" and (end := segment.find("]", index + 1)) > index:
            body = segment[index:end]
            index = end + 1
            negated = body.startswith("!")
            body = re.escape(body[1:] if negated else body).replace("\\-", "-")
            regex.append(f"[^/{body}]" if negated else f"[{body}]")
        else:
            regex.append(re.escape(char))
    return "".join(regex)
