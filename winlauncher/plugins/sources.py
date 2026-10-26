"""Plugin sources: where plugins come from, what they offer, and installing them.

A source is one of

- **remote**: a git repository — on GitHub, a company's git server or over SSH.
  WinLauncher keeps a shallow clone of it below its data directory, brought up to
  date with the user's git credentials and never a prompt. Installing copies a
  plugin folder into WinLauncher's own plugins folder, beside a note naming the
  source, so updates and removal only ever touch folders that came from there and
  a private plugin of the same name stays put. A new version is an update.
- **local**: a folder on this computer or a share. Its plugins are not copied:
  activating one loads it from where it lies, so edits apply at the next start.

Both are read with `folders.read_source`, in whichever layout they use.

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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from winlauncher.platform.git import NO_CONSOLE, git_environment
from winlauncher.plugins.config import source_key
from winlauncher.plugins.encoding import decode_text, read_text
from winlauncher.plugins.folders import (
    SKIPPED_PREFIX,
    ManifestError,
    OfferedPlugin,
    SourceError,
    read_manifest,
    read_source,
)

CLONES_FOLDER = "plugin-sources"  # Below WinLauncher's data directory.
INSTALLED_FROM_FILE = "installed-from.txt"  # In an installed plugin: its source.
GIT_TIMEOUT_SECONDS = 120  # A stalled network or credential helper is given up on.
# Both start with SKIPPED_PREFIX, so the plugin loader never picks one up.
STAGING_PREFIX = f"{SKIPPED_PREFIX}installing-"
REPLACED_PREFIX = f"{SKIPPED_PREFIX}replaced-"
PARTIAL_SUFFIX = ".partial"  # A clone that has not finished yet.
CLONE_HASH_LENGTH = 10
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
logger = logging.getLogger(__name__)


class SourceKind(StrEnum):
    REMOTE = "remote"
    LOCAL = "local"


class InstallState(StrEnum):
    """How a plugin of a remote source relates to what is installed."""

    AVAILABLE = "available"  # Not installed.
    INSTALLED = "installed"  # Installed from this source, in the version it offers.
    UPDATE = "update"  # Installed from this source; it offers another version.
    TAKEN = "taken"  # A plugin folder of this name exists that did not come from here.


def source_kind(source: str) -> SourceKind:
    """A drive or share path is a local folder; anything else is for git to clone."""
    return SourceKind.LOCAL if PureWindowsPath(source.strip()).is_absolute() else SourceKind.REMOTE


@dataclass(frozen=True, slots=True)
class Offer:
    """One plugin a source offers."""

    source: str
    kind: SourceKind
    plugin: OfferedPlugin  # Its folder lies in the local source, or in the clone.
    install: InstallState = InstallState.AVAILABLE  # Remote sources only.
    installed_version: str = ""

    @property
    def name(self) -> str:
        return self.plugin.name

    @property
    def version(self) -> str:
        return self.plugin.version

    @property
    def folder(self) -> Path:
        return self.plugin.folder


@dataclass(frozen=True, slots=True)
class Source:
    """What one source offered when it was last read."""

    location: str
    kind: SourceKind
    name: str = ""
    offers: tuple[Offer, ...] = ()
    # Why it could not be checked or read; a remote one keeps its last clone's offers.
    error: str = ""
    problems: tuple[str, ...] = ()  # Plugin folders that could not be offered, and why.

    @property
    def title(self) -> str:
        return self.name or self.location

    def with_offer(self, offer: Offer) -> Source:
        """This source with `offer` replacing its offer of the same folder."""
        offers = tuple(offer if entry.folder == offer.folder else entry for entry in self.offers)
        return replace(self, offers=offers)


class SourceChangeError(Exception):
    """A plugin could not be installed or removed; the message is meant for the user."""


class PluginSources:
    """Reads the configured sources and installs plugins from remote ones."""

    def __init__(self, clones: Path, plugins: Path):
        """`clones` holds remote sources' clones; `plugins` is where installs go."""
        self.clones = clones
        self.plugins = plugins

    # Reading ---------------------------------------------------------------------

    def fetch(self, location: str) -> Source:
        """Bring a remote source's clone up to date, then read the source.

        When git fails, the last clone is still read, so an offline computer keeps
        listing the plugins it knew; the source's `error` says why it is stale.
        """
        if source_kind(location) is SourceKind.LOCAL:
            return self.read(location)
        try:
            self._update_clone(location, self.clone(location))
        except SourceError as exc:
            logger.info("Plugin source %s could not be checked: %s", location, exc)
            return replace(self.read(location), error=f"Could not be checked: {exc}")
        return self.read(location)

    def read(self, location: str) -> Source:
        """The source as it lies on disk, without asking the network."""
        kind = source_kind(location)
        root = Path(location.strip()) if kind is SourceKind.LOCAL else self.clone(location)
        if kind is SourceKind.REMOTE and not root.is_dir():
            return Source(location, kind, error="Not loaded yet.")
        try:
            contents = read_source(root)
        except SourceError as exc:
            return Source(location, kind, error=str(exc))
        offers = tuple(self._offer(location, kind, plugin) for plugin in contents.plugins)
        return Source(location, kind, contents.name, offers, problems=contents.problems)

    def clone(self, location: str) -> Path:
        """Where the clone of a remote source lives: a readable name plus a hash."""
        key = source_key(location)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:CLONE_HASH_LENGTH]
        last_part = re.split(r"[/\\:]", key)[-1]
        return self.clones / f"{_UNSAFE_NAME.sub('-', last_part) or 'source'}-{digest}"

    def prune(self, locations: Sequence[str]) -> None:
        """Delete the clones of remote sources that are no longer configured."""
        if not self.clones.is_dir():
            return
        kept = {self.clone(location).name for location in locations}
        for folder in self.clones.iterdir():
            if folder.is_dir() and folder.name not in kept:
                _delete_clone(folder)

    # Installing ------------------------------------------------------------------

    def install(self, offer: Offer) -> Offer:
        """Copy a remote offer into the plugins folder, replacing an older copy of it.

        The new copy is assembled beside the old one and swapped in by renaming,
        so an interrupted install never leaves half a plugin behind.
        """
        target = self.plugins / offer.folder.name
        if target.exists() and not _came_from(offer.source, target):
            raise SourceChangeError(
                f"A plugin folder named {target.name} already exists in {self.plugins} "
                "and did not come from this source. Rename or remove it first."
            )
        staging = self.plugins / f"{STAGING_PREFIX}{target.name}"
        try:
            self.plugins.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(
                offer.folder, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
            note = staging / INSTALLED_FROM_FILE
            note.write_text(offer.source.strip() + "\n", encoding="utf-8")
            _swap_in(staging, target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SourceChangeError(
                f"{offer.name} could not be installed: {_reason(exc)}"
            ) from None
        logger.info("Plugin %s %s installed from %s", target.name, offer.version, offer.source)
        return replace(offer, install=InstallState.INSTALLED, installed_version=offer.version)

    def uninstall(self, offer: Offer) -> Offer:
        """Delete a plugin installed from a remote source; its data and settings stay."""
        target = self.plugins / offer.folder.name
        if not _came_from(offer.source, target):
            raise SourceChangeError(f"{offer.name} was not installed from this source.")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise SourceChangeError(f"{offer.name} could not be removed: {_reason(exc)}") from None
        logger.info("Plugin %s uninstalled", target.name)
        return replace(offer, install=InstallState.AVAILABLE, installed_version="")

    # Internals -------------------------------------------------------------------

    def _offer(self, location: str, kind: SourceKind, plugin: OfferedPlugin) -> Offer:
        offer = Offer(location, kind, plugin)
        if kind is SourceKind.LOCAL:
            return offer
        target = self.plugins / plugin.folder.name
        if not target.exists():
            return offer
        if not _came_from(location, target):
            return replace(offer, install=InstallState.TAKEN)
        try:
            installed = read_manifest(target).version
        except ManifestError:
            installed = ""  # A damaged copy is worth replacing.
        state = InstallState.INSTALLED if installed == plugin.version else InstallState.UPDATE
        return replace(offer, install=state, installed_version=installed)

    def _update_clone(self, location: str, clone: Path) -> None:
        """Fetch the remote's default branch into the clone, or clone it the first time."""
        if (clone / ".git").is_dir():
            _git(["fetch", "--depth", "1", "--quiet", "origin", "HEAD"], clone)
            _git(["reset", "--hard", "--quiet", "FETCH_HEAD"], clone)
            _git(["clean", "-ffdxq"], clone)
            return
        partial = clone.with_name(clone.name + PARTIAL_SUFFIX)
        _delete_clone(partial)
        _delete_clone(clone)  # Not a clone: something left over.
        self.clones.mkdir(parents=True, exist_ok=True)
        _git(
            ["clone", "--depth", "1", "--quiet", "--", location.strip(), str(partial)], self.clones
        )
        partial.rename(clone)


def _came_from(location: str, target: Path) -> bool:
    try:
        recorded = read_text(target / INSTALLED_FROM_FILE)
    except OSError:
        return False
    return source_key(recorded) == source_key(location)


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


def _git(arguments: list[str], cwd: Path) -> None:
    """Run git without a console or prompt; raise SourceError with git's own reason."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=git_environment(),
            creationflags=NO_CONSOLE,
        )
    except FileNotFoundError:
        raise SourceError("Install git to use remote plugin sources.") from None
    except subprocess.TimeoutExpired:
        raise SourceError(f"git gave no answer within {GIT_TIMEOUT_SECONDS} seconds.") from None
    except OSError as exc:
        raise SourceError(f"git could not run: {_reason(exc)}") from None
    if result.returncode != 0:
        lines = [line for line in decode_text(result.stderr).splitlines() if line.strip()]
        raise SourceError(lines[-1].strip() if lines else f"git {arguments[0]} failed.")


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)
