"""Find the installed plugins: installed packages and the user's plugin folder.

Packages declare plugins as entry points of the group `winlauncher.plugins`, each
naming a `Plugin` subclass, such as the separate `winlauncher-plugin-telegram`
distribution. In the plugin folder, every `Plugin` subclass that a `*.py` file
defines is one plugin. A plugin that fails to load is reported and skipped; the
others still load. WinLauncher itself ships no plugin.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from types import ModuleType

from winlauncher.plugins.api import PLUGIN_ID, Plugin

ENTRY_POINT_GROUP = "winlauncher.plugins"
BUILT_IN: tuple[str, ...] = ()  # `module:Class` references WinLauncher loads itself.
_FOLDER_MODULES = "winlauncher_user_plugins"
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Discovery:
    plugins: list[Plugin] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # For the user: what did not load.

    def add(self, plugin_class: object, origin: str) -> None:
        if not (inspect.isclass(plugin_class) and issubclass(plugin_class, Plugin)):
            self.problems.append(f"{origin}: not a winlauncher Plugin class.")
            return
        plugin_id = plugin_class.id
        if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
            self.problems.append(f"{origin}: the id must use a-z, 0-9, _ and -.")
            return
        if any(plugin.id == plugin_id for plugin in self.plugins):
            self.problems.append(f"{origin}: another plugin already uses the id {plugin_id}.")
            return
        try:
            plugin = plugin_class()
        except Exception as exc:
            self.problems.append(f"{origin}: {exc}")
            logger.exception("Plugin %s could not be created", origin)
            return
        self.plugins.append(plugin)


def discover_plugins(folder: Path | None, *, built_in: Iterable[str] = BUILT_IN) -> Discovery:
    found = Discovery()
    for reference in built_in:
        _add_reference(found, reference, reference)
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        _add_reference(found, entry.value, f"{entry.dist.name if entry.dist else ''} {entry.name}")
    if folder is not None and folder.is_dir():
        for file in sorted(folder.glob("*.py")):
            _add_folder_module(found, file)
    logger.info(
        "Plugins found: %s; problems=%d",
        ", ".join(plugin.id for plugin in found.plugins) or "none",
        len(found.problems),
    )
    return found


def _add_reference(found: Discovery, reference: str, origin: str) -> None:
    module_name, _, attribute = reference.partition(":")
    try:
        found.add(getattr(importlib.import_module(module_name), attribute), origin.strip())
    except Exception as exc:
        found.problems.append(f"{origin.strip()}: {exc}")
        logger.exception("Plugin %s could not be loaded", reference)


def _add_folder_module(found: Discovery, file: Path) -> None:
    try:
        module = _load_file(file)
    except Exception as exc:
        found.problems.append(f"{file.name}: {type(exc).__name__}: {exc}")
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
        found.problems.append(f"{file.name}: defines no Plugin subclass.")
    for plugin_class in classes:
        found.add(plugin_class, f"{file.name} {plugin_class.__name__}")


def _load_file(file: Path) -> ModuleType:
    name = f"{_FOLDER_MODULES}.{file.stem}"
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
