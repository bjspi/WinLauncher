"""Load the plugins in use: the plugins folder, activated plugin folders, packages.

WinLauncher's own plugins folder holds one subfolder per plugin — installed from
a source or copied in by hand — and loose `<name>.py` files for quick plugins of
your own; all of them load. Plugin folders of local sources load only once the
user activated them, from where they lie. Packages may declare plugins as entry
points of the group `winlauncher.plugins` instead.

The first plugin claiming an id wins; later ones, unreadable folders and modules
that fail to import are reported and skipped, so one broken plugin never hides the
others. WinLauncher itself ships no plugin.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import inspect
import logging
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from winlauncher.plugins.api import PLUGIN_ID, Plugin, PluginOption, PluginRoute
from winlauncher.plugins.config import KEYWORD, REFRESH_RANGE_MINUTES
from winlauncher.plugins.encoding import read_python_source
from winlauncher.plugins.folders import (
    MANIFEST_FILE,
    PLUGIN_FILE,
    SKIPPED_PREFIX,
    ManifestError,
    read_manifest,
)
from winlauncher.plugins.search import literal_patterns

ENTRY_POINT_GROUP = "winlauncher.plugins"
BUILT_IN: tuple[str, ...] = ()  # `module:Class` references WinLauncher loads itself.
_FOLDER_MODULES = "winlauncher_user_plugins"  # Module name prefix of loaded files.
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginSource:
    """Where a plugin came from, for the settings page and for updates."""

    origin: str  # Names the plugin in messages: its folder, file or distribution.
    path: Path | None = None  # The loaded module file; None for installed packages.
    version: str = ""  # From `plugin.toml`; empty when the plugin has no manifest.

    @property
    def folder(self) -> Path | None:
        """The plugin's own folder, for a plugin that has one."""
        if self.path is None or self.path.name != PLUGIN_FILE:
            return None
        return self.path.parent


@dataclass(slots=True)
class Discovery:
    """What one search found: usable plugins, their sources and what was skipped."""

    plugins: list[Plugin] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # For the user: what did not load.
    sources: dict[str, PluginSource] = field(default_factory=dict)  # By plugin id.

    def add(self, plugin_class: object, source: PluginSource) -> None:
        """Instantiate `plugin_class` unless it is no usable, unclaimed plugin."""
        if not (inspect.isclass(plugin_class) and issubclass(plugin_class, Plugin)):
            self.problems.append(f"{source.origin}: not a winlauncher Plugin class.")
            return
        plugin_id = plugin_class.id
        if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
            self.problems.append(f"{source.origin}: the id must use a-z, 0-9, _ and -.")
            return
        if plugin_id in self.sources:
            taken = self.sources[plugin_id].origin
            self.problems.append(f"{source.origin}: the id {plugin_id} is already used by {taken}.")
            return
        if (problem := _declaration_problem(plugin_class)) is not None:
            self.problems.append(f"{source.origin}: {problem}")
            return
        try:
            plugin = plugin_class()
        except Exception as exc:
            self.problems.append(f"{source.origin}: {exc}")
            logger.exception("Plugin %s could not be created", source.origin)
            return
        self.plugins.append(plugin)
        self.sources[plugin_id] = source


def _declaration_problem(plugin_class: type[Plugin]) -> str | None:
    """What a plugin declares wrongly, checked once so the rest may trust it.

    Everything below is read while painting results or building the settings page,
    where an exception would take the window down. A plugin that declares nonsense
    is therefore rejected here, with the message its author needs.
    """
    if plugin_class.refresh is Plugin.refresh:
        return "it does not implement refresh()."
    if not isinstance(plugin_class.label, str) or not plugin_class.label.strip():
        return "it needs a label, shown before each item's name."
    for name in ("description", "icon"):
        if not isinstance(getattr(plugin_class, name), str):
            return f"its {name} must be text."
    if (problem := _keywords_problem(plugin_class)) is not None:
        return problem
    if (problem := _routes_problem(plugin_class.routes)) is not None:
        return problem
    if type(plugin_class.round_icons) is not bool:
        return "round_icons must be True or False."
    if (problem := _literal_terms_problem(plugin_class.literal_terms)) is not None:
        return problem
    minutes, (low, high) = plugin_class.refresh_minutes, REFRESH_RANGE_MINUTES
    if type(minutes) is not int or not low <= minutes <= high:
        return f"refresh_minutes must be a whole number from {low} to {high}."
    return _options_problem(plugin_class.options)


def _keywords_problem(plugin_class: type[Plugin]) -> str | None:
    keyword, keywords = plugin_class.keyword, plugin_class.keywords
    if not isinstance(keyword, str) or (keyword and not KEYWORD.fullmatch(keyword)):
        return "its keyword may contain letters, numbers, _ and -."
    if not _usable_keywords(keywords):
        return "keywords must be a tuple of words of letters, numbers, _ and -."
    if keyword and keywords:
        return "declare keywords or keyword, not both."
    return None


def _routes_problem(routes: object) -> str | None:
    if not isinstance(routes, tuple) or not all(isinstance(route, PluginRoute) for route in routes):
        return "routes must be a tuple of PluginRoute values."
    ids: set[str] = set()
    for route in routes:
        if not isinstance(route.id, str) or not PLUGIN_ID.fullmatch(route.id):
            return "a route id must use a-z, 0-9, _ and -."
        if route.id in ids:
            return f"the route {route.id} is declared twice."
        ids.add(route.id)
        if not isinstance(route.label, str) or not route.label.strip():
            return f"the route {route.id} needs a label."
        if not isinstance(route.description, str):
            return f"the description of route {route.id} must be text."
        if not _usable_keywords(route.keywords):
            return f"the keywords of route {route.id} must be a tuple of words."
    return None


def _usable_keywords(keywords: object) -> bool:
    return isinstance(keywords, tuple) and all(
        isinstance(keyword, str) and KEYWORD.fullmatch(keyword) for keyword in keywords
    )


def _literal_terms_problem(patterns: object) -> str | None:
    """Patterns are compiled here, so a broken one never raises during a search."""
    if not isinstance(patterns, tuple) or not all(isinstance(pattern, str) for pattern in patterns):
        return "literal_terms must be a tuple of regular expressions."
    try:
        literal_patterns(patterns)
    except re.error as exc:
        return f"the literal_terms pattern {exc.pattern!r} is invalid: {exc.msg}."
    return None


def _options_problem(options: object) -> str | None:
    if not isinstance(options, tuple) or not all(
        isinstance(option, PluginOption) for option in options
    ):
        return "options must be a tuple of PluginOption values."
    keys: set[str] = set()
    for option in options:
        if not isinstance(option.key, str) or not option.key:
            return "every option needs a key."
        if option.key in keys:
            return f"the option {option.key} is declared twice."
        keys.add(option.key)
        if not isinstance(option.label, str) or not option.label.strip():
            return f"the option {option.key} needs a label."
        if type(option.default) not in (bool, int, str):
            return f"the option {option.key} needs a True/False, whole number or text default."
        if option.bounds is not None and not _usable_bounds(option):
            return f"the option {option.key} has bounds that its default misses."
        if type(option.multiline) is not bool:
            return f"the option {option.key} multiline must be True or False."
        if option.multiline and not isinstance(option.default, str):
            return f"the option {option.key} multiline needs a text default."
    return None


def _usable_bounds(option: PluginOption) -> bool:
    bounds = option.bounds
    if not (isinstance(bounds, tuple) and len(bounds) == 2):
        return False
    low, high = bounds
    if type(low) is not int or type(high) is not int or low > high:
        return False
    # Bounds only apply to numbers, and the default must be one the user can return to.
    return type(option.default) is int and low <= option.default <= high


def discover_plugins(
    folders: Sequence[Path] = (),
    active: Sequence[Path] = (),
    *,
    built_in: Iterable[str] = BUILT_IN,
) -> Discovery:
    """Built-ins and packages, every plugin in `folders`, then each `active` plugin folder."""
    found = Discovery()
    for reference in built_in:
        _add_reference(found, reference, reference)
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        distribution = entry.dist.name if entry.dist else entry.name
        _add_reference(found, entry.value, distribution)
    for folder in folders:
        _add_plugin_folder(found, folder)
    for folder in active:
        if folder.is_dir():
            _add_plugin_subfolder(found, folder)
        else:
            found.problems.append(f"{folder}: the activated plugin folder is gone.")
    logger.info(
        "Plugins found: %s; folders=%d active=%d problems=%d",
        ", ".join(plugin.id for plugin in found.plugins) or "none",
        len(folders),
        len(active),
        len(found.problems),
    )
    return found


def _add_reference(found: Discovery, reference: str, origin: str) -> None:
    """Load `module:Class` from the import path, as a package's entry point names it."""
    module_name, _, attribute = reference.partition(":")
    try:
        found.add(getattr(importlib.import_module(module_name), attribute), PluginSource(origin))
    except Exception as exc:
        found.problems.append(f"{origin}: {exc}")
        logger.exception("Plugin %s could not be loaded", reference)


def _add_plugin_folder(found: Discovery, folder: Path) -> None:
    """One configured plugin path: its plugin folders and its loose `*.py` files."""
    try:
        if not folder.is_dir():
            return  # Created when the first plugin is installed.
        entries = sorted(folder.iterdir())
    except OSError as exc:
        found.problems.append(f"{folder}: {exc.strerror or exc}")
        return
    for entry in entries:
        if entry.name.startswith(SKIPPED_PREFIX):
            continue  # Templates, staging folders and caches.
        if entry.is_dir():
            _add_plugin_subfolder(found, entry)
        elif entry.suffix == ".py":
            _add_module_file(found, entry, PluginSource(entry.name, entry))


def _add_plugin_subfolder(found: Discovery, folder: Path) -> None:
    """`<folder>/plugin.py` with the version of its manifest, if it is a plugin at all."""
    module = folder / PLUGIN_FILE
    manifest = folder / MANIFEST_FILE
    if not module.is_file():
        if manifest.is_file():  # Meant as a plugin; other folders are none of our business.
            found.problems.append(f"{folder.name}: has no {PLUGIN_FILE}.")
        return
    _add_module_file(found, module, PluginSource(folder.name, module, _version(found, folder)))


def _version(found: Discovery, folder: Path) -> str:
    """The version its manifest names; empty when it has none or an unusable one."""
    try:
        return read_manifest(folder).version
    except ManifestError as exc:
        found.problems.append(f"{folder.name}: {exc}")
        return ""


def _add_module_file(found: Discovery, file: Path, source: PluginSource) -> None:
    """Import `file` and take every `Plugin` subclass it defines itself."""
    try:
        module = _load_module(file)
    except Exception as exc:
        found.problems.append(f"{source.origin}: {type(exc).__name__}: {exc}")
        logger.exception("Plugin file %s could not be loaded", file)
        return
    classes = [
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and issubclass(value, Plugin)
        and value.__module__ == module.__name__
    ]
    if not classes:
        found.problems.append(f"{source.origin}: defines no Plugin subclass.")
    for plugin_class in classes:
        named = (
            source
            if len(classes) == 1
            else replace(source, origin=f"{source.origin} {plugin_class.__name__}")
        )
        found.add(plugin_class, named)


def _load_module(file: Path) -> ModuleType:
    name = _module_name(file)
    spec = importlib.util.spec_from_file_location(name, file, loader=_PluginFileLoader(file))
    if spec is None or spec.loader is None:
        raise ImportError("not a Python module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # Dataclasses and pickling look modules up by name.
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _PluginFileLoader(importlib.abc.Loader):
    """Runs a plugin file in whatever encoding its author's editor saved it.

    Python's own loader insists on UTF-8 or a coding declaration, so a plugin saved
    as Windows-1252 would fail over an umlaut in a comment.
    """

    def __init__(self, file: Path):
        self.file = file

    def exec_module(self, module: ModuleType) -> None:
        code = compile(read_python_source(self.file), str(self.file), "exec", dont_inherit=True)
        exec(code, module.__dict__)  # noqa: S102 - running the plugin is the loader's job.


def _module_name(file: Path) -> str:
    """A free dotted name for `file`, named after its plugin folder or its file name."""
    stem = file.parent.name if file.name == PLUGIN_FILE else file.stem
    base = f"{_FOLDER_MODULES}.{stem}"
    name, number = base, 2
    while name in sys.modules:  # A second plugin folder of the same name in another path.
        name, number = f"{base}_{number}", number + 1
    return name
