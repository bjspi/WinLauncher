"""Plugin catalogs: git repositories that offer plugins to install with one click.

A catalog is any repository git can clone — on GitHub, on a company's own git
server or on a network share — with a `catalog.toml` at its root:

    name = "WinLauncher Plugins"   # Shown in Settings; the URL when missing.
    plugins = "plugins"            # The folder holding one subfolder per plugin.

Each plugin folder carries its own `plugin.toml` (see `manifest`) with its
version, name and description, so the catalog never repeats them.

WinLauncher keeps a shallow clone of each catalog below its data directory and
brings it up to date with git, using the user's git credentials without ever
prompting. Installing copies a plugin folder into WinLauncher's own plugins
folder, beside a note naming the catalog, so updates and removal only ever touch
folders that came from that catalog; a private plugin of the same name stays put.

No Qt here. Every method blocks on git or the disk, so callers run them on a
worker, one at a time: two calls must not change the same clone at once.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from winlauncher.platform.git import NO_CONSOLE, git_environment
from winlauncher.plugins.config import catalog_key
from winlauncher.plugins.manifest import (
    MANIFEST_FILE,
    ManifestError,
    PluginManifest,
    read_manifest,
)
from winlauncher.plugins.registry import PLUGIN_FILE

CATALOG_FILE = "catalog.toml"
CLONES_FOLDER = "plugin-catalogs"  # Below WinLauncher's data directory.
DEFAULT_PLUGINS_FOLDER = "plugins"
INSTALLED_FROM_FILE = "installed-from.txt"  # In an installed plugin: its catalog's URL.
GIT_TIMEOUT_SECONDS = 120  # A stalled network or credential helper is given up on.
# Staging folders start with "_", so the plugin loader never picks one up.
STAGING_PREFIX = "_installing-"
REPLACED_PREFIX = "_replaced-"
PARTIAL_SUFFIX = ".partial"  # A clone that has not finished yet.
MIRROR_HASH_LENGTH = 10
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
logger = logging.getLogger(__name__)


class CatalogError(Exception):
    """A catalog could not be loaded or changed; the message is meant for the user."""


class InstallState(StrEnum):
    AVAILABLE = "available"  # Not installed.
    INSTALLED = "installed"  # Installed from this catalog, in the version it offers.
    UPDATE = "update"  # Installed from this catalog; it offers another version.
    TAKEN = "taken"  # A plugin folder of this name exists that did not come from here.


@dataclass(frozen=True, slots=True)
class CatalogPlugin:
    """One plugin a catalog offers, and how it relates to what is installed."""

    folder: str  # Its folder name, in the catalog and once installed.
    manifest: PluginManifest
    source: Path  # The folder in the local clone.
    state: InstallState = InstallState.AVAILABLE
    installed_version: str = ""

    @property
    def name(self) -> str:
        return self.manifest.name or self.folder

    @property
    def version(self) -> str:
        return self.manifest.version


@dataclass(frozen=True, slots=True)
class Catalog:
    """What one catalog offered when it was last read."""

    url: str
    name: str = ""
    plugins: tuple[CatalogPlugin, ...] = ()
    # Why it could not be checked or read; plugins of the last good clone stay listed.
    error: str = ""
    problems: tuple[str, ...] = ()  # Plugin folders that could not be offered, and why.

    @property
    def title(self) -> str:
        return self.name or self.url

    def with_plugin(self, plugin: CatalogPlugin) -> Catalog:
        """This catalog with `plugin` replacing its entry of the same folder."""
        plugins = tuple(
            plugin if entry.folder == plugin.folder else entry for entry in self.plugins
        )
        return replace(self, plugins=plugins)


class PluginCatalogs:
    """The local clones of the configured catalogs and the plugins installed from them."""

    def __init__(self, mirrors: Path, plugins: Path):
        """`mirrors` holds the clones; `plugins` is the folder plugins are installed into."""
        self.mirrors = mirrors
        self.plugins = plugins

    # Reading ---------------------------------------------------------------------

    def fetch(self, url: str) -> Catalog:
        """Bring the clone of `url` up to date, then read it.

        When git fails, the last clone is still read, so an offline computer keeps
        listing the plugins it knew; the catalog's `error` says why it is stale.
        """
        mirror = self.mirror(url)
        try:
            self._update_mirror(url, mirror)
        except CatalogError as exc:
            logger.info("Plugin catalog %s could not be checked: %s", url, exc)
            catalog = self.read(url)
            return replace(catalog, error=f"Could not be checked: {exc}")
        return self.read(url)

    def read(self, url: str) -> Catalog:
        """The catalog as its local clone has it, without asking the network."""
        mirror = self.mirror(url)
        if not mirror.is_dir():
            return Catalog(url, error="Not loaded yet.")
        try:
            name, folder = _catalog_settings(mirror)
        except CatalogError as exc:
            return Catalog(url, error=str(exc))
        plugins, problems = [], []
        for entry in _plugin_folders(folder):
            try:
                manifest = read_manifest(entry / MANIFEST_FILE)
            except ManifestError as exc:
                problems.append(f"{entry.name}: {exc}")
                continue
            plugins.append(self._with_state(url, CatalogPlugin(entry.name, manifest, entry)))
        return Catalog(url, name, tuple(plugins), problems=tuple(problems))

    def mirror(self, url: str) -> Path:
        """Where the clone of `url` lives: a readable name plus a hash of the URL."""
        key = catalog_key(url)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:MIRROR_HASH_LENGTH]
        readable = _UNSAFE_NAME.sub("-", key.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) or "catalog"
        return self.mirrors / f"{readable}-{digest}"

    def prune(self, urls: Sequence[str]) -> None:
        """Delete the clones of catalogs that are no longer configured."""
        if not self.mirrors.is_dir():
            return
        kept = {self.mirror(url).name for url in urls}
        for folder in self.mirrors.iterdir():
            if folder.is_dir() and folder.name not in kept:
                _delete_clone(folder)

    # Installing ------------------------------------------------------------------

    def install(self, url: str, plugin: CatalogPlugin) -> CatalogPlugin:
        """Copy `plugin` into the plugins folder, replacing an older copy from `url`.

        The new copy is assembled beside the old one and swapped in by renaming,
        so an interrupted install never leaves half a plugin behind.
        """
        target = self.plugins / plugin.folder
        if target.exists() and not self._came_from(url, target):
            raise CatalogError(
                f"A plugin folder named {plugin.folder} already exists in {self.plugins} "
                "and did not come from this catalog. Rename or remove it first."
            )
        staging = self.plugins / f"{STAGING_PREFIX}{plugin.folder}"
        try:
            self.plugins.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(
                plugin.source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
            (staging / INSTALLED_FROM_FILE).write_text(url.strip() + "\n", encoding="utf-8")
            _swap_in(staging, target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise CatalogError(f"{plugin.name} could not be installed: {_reason(exc)}") from None
        logger.info("Plugin %s %s installed from %s", plugin.folder, plugin.version, url)
        return replace(plugin, state=InstallState.INSTALLED, installed_version=plugin.version)

    def remove(self, url: str, plugin: CatalogPlugin) -> CatalogPlugin:
        """Delete the plugin installed from `url`; its data and settings are kept."""
        target = self.plugins / plugin.folder
        if not self._came_from(url, target):
            raise CatalogError(f"{plugin.name} was not installed from this catalog.")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise CatalogError(f"{plugin.name} could not be removed: {_reason(exc)}") from None
        logger.info("Plugin %s removed", plugin.folder)
        return replace(plugin, state=InstallState.AVAILABLE, installed_version="")

    # Internals -------------------------------------------------------------------

    def _with_state(self, url: str, plugin: CatalogPlugin) -> CatalogPlugin:
        target = self.plugins / plugin.folder
        if not target.exists():
            return plugin
        if not self._came_from(url, target):
            return replace(plugin, state=InstallState.TAKEN)
        try:
            installed = read_manifest(target / MANIFEST_FILE).version
        except ManifestError:
            installed = ""  # A damaged copy is worth replacing.
        state = InstallState.INSTALLED if installed == plugin.version else InstallState.UPDATE
        return replace(plugin, state=state, installed_version=installed)

    @staticmethod
    def _came_from(url: str, target: Path) -> bool:
        try:
            recorded = (target / INSTALLED_FROM_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return catalog_key(recorded) == catalog_key(url)

    def _update_mirror(self, url: str, mirror: Path) -> None:
        """Fetch the remote's default branch into the clone, or clone it the first time."""
        if (mirror / ".git").is_dir():
            _git(["fetch", "--depth", "1", "--quiet", "origin", "HEAD"], mirror)
            _git(["reset", "--hard", "--quiet", "FETCH_HEAD"], mirror)
            _git(["clean", "-ffdxq"], mirror)
            return
        partial = mirror.with_name(mirror.name + PARTIAL_SUFFIX)
        _delete_clone(partial)
        _delete_clone(mirror)  # Not a clone: something left over.
        self.mirrors.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "1", "--quiet", "--", url.strip(), str(partial)], self.mirrors)
        partial.rename(mirror)


def _catalog_settings(mirror: Path) -> tuple[str, Path]:
    """The catalog's name and its plugins folder, which must lie inside the clone."""
    file = mirror / CATALOG_FILE
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"This repository has no {CATALOG_FILE}, so it is no catalog.") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"{CATALOG_FILE} is unreadable ({exc}).") from None
    name, folder = data.get("name", ""), data.get("plugins", DEFAULT_PLUGINS_FOLDER)
    if not isinstance(name, str) or not isinstance(folder, str):
        raise CatalogError(f"name and plugins in {CATALOG_FILE} must be text.")
    plugins = (mirror / folder).resolve()
    if not plugins.is_relative_to(mirror.resolve()):
        raise CatalogError(f"The plugins folder of {CATALOG_FILE} must lie inside the catalog.")
    return name, plugins


def _plugin_folders(folder: Path) -> list[Path]:
    """The catalog's plugin folders, as the plugin loader would find them."""
    if not folder.is_dir():
        return []
    return [
        entry
        for entry in sorted(folder.iterdir())
        if entry.is_dir() and not entry.name.startswith("_") and (entry / PLUGIN_FILE).is_file()
    ]


def _delete_clone(folder: Path) -> None:
    """Delete a clone as far as possible; git marks object files read-only, which
    Windows refuses to delete until they are writable again."""
    if not folder.exists():
        return
    for path in folder.rglob("*"):
        try:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            continue  # Deleted below if possible; a leftover is retried next time.
    shutil.rmtree(folder, ignore_errors=True)


def _swap_in(staging: Path, target: Path) -> None:
    """Rename `staging` to `target`; an existing `target` is moved aside, then deleted."""
    if not target.exists():
        staging.rename(target)
        return
    replaced = target.with_name(f"{REPLACED_PREFIX}{target.name}")
    shutil.rmtree(replaced, ignore_errors=True)
    target.rename(replaced)
    try:
        staging.rename(target)
    except OSError:
        replaced.rename(target)  # Put the working copy back.
        raise
    shutil.rmtree(replaced, ignore_errors=True)


def _git(arguments: list[str], cwd: Path) -> None:
    """Run git without a console or prompt; raise CatalogError with git's own reason."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            env=git_environment(),
            creationflags=NO_CONSOLE,
        )
    except FileNotFoundError:
        raise CatalogError("Install git to use plugin catalogs.") from None
    except subprocess.TimeoutExpired:
        raise CatalogError(f"git gave no answer within {GIT_TIMEOUT_SECONDS} seconds.") from None
    except OSError as exc:
        raise CatalogError(f"git could not run: {_reason(exc)}") from None
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        raise CatalogError(lines[-1].strip() if lines else f"git {arguments[0]} failed.")


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)
