"""Find the installed plugins: the plugin folders, installed packages and built-ins.

A plugin folder holds one subfolder per plugin, `<name>/plugin.py`, beside an
optional `plugin.toml` that names its version. A loose `<name>.py` file in the
same folder is a plugin too, for a quick one of your own. WinLauncher searches its
data directory's `plugins` folder and the folders configured beside it, in order,
so a cloned plugin repository and private plugins live side by side. Packages may
declare plugins as entry points of the group `winlauncher.plugins` instead.

The first plugin claiming an id wins; later ones, unreadable folders and modules
that fail to import are reported and skipped, so one broken plugin never hides the
others. WinLauncher itself ships no plugin.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from winlauncher.plugins.api import PLUGIN_ID, Plugin

ENTRY_POINT_GROUP = "winlauncher.plugins"
BUILT_IN: tuple[str, ...] = ()  # `module:Class` references WinLauncher loads itself.
PLUGIN_FILE = "plugin.py"  # The module of a plugin folder.
MANIFEST_FILE = "plugin.toml"  # Optional, beside it: `version = "1.0.0"`.
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
        try:
            plugin = plugin_class()
        except Exception as exc:
            self.problems.append(f"{source.origin}: {exc}")
            logger.exception("Plugin %s could not be created", source.origin)
            return
        self.plugins.append(plugin)
        self.sources[plugin_id] = source


def discover_plugins(
    folders: Sequence[Path] = (), *, built_in: Iterable[str] = BUILT_IN
) -> Discovery:
    """Every plugin of `folders`, searched in order, plus packages and built-ins."""
    found = Discovery()
    for reference in built_in:
        _add_reference(found, reference, reference)
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        distribution = entry.dist.name if entry.dist else entry.name
        _add_reference(found, entry.value, distribution)
    for folder in folders:
        _add_plugin_folder(found, folder)
    logger.info(
        "Plugins found: %s; folders=%d problems=%d",
        ", ".join(plugin.id for plugin in found.plugins) or "none",
        len(folders),
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
            return  # A path that is not there yet is no error; it may be a clone to come.
        entries = sorted(folder.iterdir())
    except OSError as exc:
        found.problems.append(f"{folder}: {exc.strerror or exc}")
        return
    for entry in entries:
        if entry.is_dir():
            _add_plugin_subfolder(found, entry)
        elif entry.suffix == ".py" and not entry.name.startswith("_"):
            _add_module_file(found, entry, PluginSource(entry.name, entry))


def _add_plugin_subfolder(found: Discovery, folder: Path) -> None:
    """`<folder>/plugin.py` with the version of its manifest, if it is a plugin at all."""
    module = folder / PLUGIN_FILE
    manifest = folder / MANIFEST_FILE
    if not module.is_file():
        if manifest.is_file():  # Meant as a plugin; other folders are none of our business.
            found.problems.append(f"{folder.name}: has no {PLUGIN_FILE}.")
        return
    version = _manifest_version(found, manifest) if manifest.is_file() else ""
    _add_module_file(found, module, PluginSource(folder.name, module, version))


def _manifest_version(found: Discovery, manifest: Path) -> str:
    """The `version` of a plugin manifest; empty when it does not name a usable one."""
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        found.problems.append(f"{manifest.parent.name}: {MANIFEST_FILE} is unreadable ({exc}).")
        return ""
    version = data.get("version", "")
    if not isinstance(version, str):
        found.problems.append(f"{manifest.parent.name}: the version must be text.")
        return ""
    return version


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
    spec = importlib.util.spec_from_file_location(name, file)
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


def _module_name(file: Path) -> str:
    """A free dotted name for `file`, named after its plugin folder or its file name."""
    stem = file.parent.name if file.name == PLUGIN_FILE else file.stem
    base = f"{_FOLDER_MODULES}.{stem}"
    name, number = base, 2
    while name in sys.modules:  # A second plugin folder of the same name in another path.
        name, number = f"{base}_{number}", number + 1
    return name
