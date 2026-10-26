"""How plugins lie on disk: plugin folders, their manifests, and sources offering them.

A plugin folder holds `plugin.py` and, optionally, a `plugin.toml` manifest with
what WinLauncher may know without running the plugin: its version, name,
description and the Python packages it needs. Both are read in UTF-8 or, as older
Windows editors save them, Windows-1252 (see `encoding`).

A plugin source — a local folder, or the clone of a remote repository — offers
plugin folders in one of three layouts:

- a `catalog.toml` at its root names the source and the folder of its plugins
  (`name = "Team plugins"`, `plugins = "plugins"`),
- the source is a plugin folder itself, or
- the source is a folder of plugin folders.

Reading never runs plugin code. Folders whose name starts with `_`, such as a
template, are never offered.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from winlauncher.plugins.encoding import read_text

PLUGIN_FILE = "plugin.py"
MANIFEST_FILE = "plugin.toml"
CATALOG_FILE = "catalog.toml"
DEFAULT_PLUGINS_FOLDER = "plugins"
SKIPPED_PREFIX = "_"  # Templates, staging folders and caches.


class ManifestError(ValueError):
    """A manifest exists but cannot be used; the message names the mistake."""


class SourceError(Exception):
    """A source cannot be read at all; the message is meant for the user."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    version: str = ""
    name: str = ""  # Shown before the plugin is loaded; its label afterwards.
    description: str = ""
    # Python packages to install beside WinLauncher, as pip requirements.
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OfferedPlugin:
    """A plugin folder a source offers, described by its manifest."""

    folder: Path
    manifest: PluginManifest

    @property
    def name(self) -> str:
        return self.manifest.name or self.folder.name

    @property
    def version(self) -> str:
        return self.manifest.version


@dataclass(frozen=True, slots=True)
class SourceContents:
    name: str  # From catalog.toml; empty when the source does not name itself.
    plugins: tuple[OfferedPlugin, ...]
    problems: tuple[str, ...]  # Plugin folders that could not be offered, and why.


def is_plugin_folder(folder: Path) -> bool:
    return (folder / PLUGIN_FILE).is_file()


def read_manifest(folder: Path) -> PluginManifest:
    """The manifest of a plugin folder; empty when it has none.

    Raises ManifestError when the manifest is unreadable or mistyped.
    """
    file = folder / MANIFEST_FILE
    if not file.is_file():
        return PluginManifest()
    try:
        data = tomllib.loads(read_text(file))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{MANIFEST_FILE} is unreadable ({exc}).") from None
    for key in ("version", "name", "description"):
        if not isinstance(data.get(key, ""), str):
            raise ManifestError(f"the {key} must be text.")
    requires = data.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
        raise ManifestError("requires must be a list of pip requirements.")
    return PluginManifest(
        version=data.get("version", ""),
        name=data.get("name", ""),
        description=data.get("description", ""),
        requires=tuple(requires),
    )


def plugin_folders(folder: Path) -> list[Path]:
    """The plugin folders directly inside `folder`, by name; `_` folders are skipped."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if not entry.name.startswith(SKIPPED_PREFIX) and entry.is_dir() and is_plugin_folder(entry)
    ]


def read_source(root: Path) -> SourceContents:
    """What the source at `root` offers, in whichever of the three layouts it uses."""
    if not root.is_dir():
        raise SourceError(f"The folder {root} does not exist.")
    name, folders = _catalog_layout(root)
    plugins, problems = [], []
    for folder in folders:
        try:
            plugins.append(OfferedPlugin(folder, read_manifest(folder)))
        except ManifestError as exc:
            problems.append(f"{folder.name}: {exc}")
    return SourceContents(name, tuple(plugins), tuple(problems))


def _catalog_layout(root: Path) -> tuple[str, list[Path]]:
    """The source's name and plugin folders; a catalog file decides where they are."""
    catalog = root / CATALOG_FILE
    if not catalog.is_file():
        return "", [root] if is_plugin_folder(root) else plugin_folders(root)
    try:
        data = tomllib.loads(read_text(catalog))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SourceError(f"{CATALOG_FILE} is unreadable ({exc}).") from None
    name, folder = data.get("name", ""), data.get("plugins", DEFAULT_PLUGINS_FOLDER)
    if not isinstance(name, str) or not isinstance(folder, str):
        raise SourceError(f"name and plugins in {CATALOG_FILE} must be text.")
    plugins = (root / folder).resolve()
    if not plugins.is_relative_to(root.resolve()):
        raise SourceError(f"The plugins folder of {CATALOG_FILE} must lie inside the source.")
    return name, plugin_folders(plugins)
