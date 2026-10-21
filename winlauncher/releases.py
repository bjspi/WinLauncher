"""Which published release is newest, and how each kind of installation updates to it.

Updates follow release tags such as `v0.2.3`, never untested commits: only a
release ships the matching prebuilt Rust engine. No Qt and no git calls here.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

REPOSITORY_URL = "https://github.com/bjspi/WinLauncher.git"
_RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


class Installation(Enum):
    CHECKOUT = "checkout"  # A git clone; WinLauncher fast-forwards and reinstalls it.
    PIPX = "pipx"
    PACKAGE = "package"  # Any other pip installation.


@dataclass(frozen=True, order=True, slots=True)
class Release:
    major: int
    minor: int
    patch: int

    @classmethod
    def from_tag(cls, tag: str) -> Release | None:
        match = _RELEASE_TAG.fullmatch(tag)
        return cls(*map(int, match.groups())) if match else None

    @classmethod
    def from_version(cls, version: str) -> Release | None:
        return cls.from_tag(f"v{version}")

    @property
    def tag(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"


def newest_release(tags: Iterable[str]) -> Release | None:
    """The highest `vX.Y.Z` tag; other tags such as `v1.0-beta` are ignored."""
    return max(filter(None, map(Release.from_tag, tags)), default=None)


def remote_tags(ls_remote_output: str) -> list[str]:
    """Tag names from `git ls-remote --tags --refs` output."""
    prefix = "refs/tags/"
    return [
        reference.removeprefix(prefix)
        for line in ls_remote_output.splitlines()
        if (reference := line.rpartition("\t")[2]).startswith(prefix)
    ]


def installation(checkout: Path | None, prefix: Path = Path(sys.prefix)) -> Installation:
    """How WinLauncher was installed; pipx keeps each app in `…/pipx/venvs/<app>`."""
    if checkout is not None:
        return Installation.CHECKOUT
    parts = [part.casefold() for part in prefix.parts]
    return Installation.PIPX if "pipx" in parts and "venvs" in parts else Installation.PACKAGE


def install_command(kind: Installation, release: Release) -> str:
    """The command a user runs to install `release` over an installed package."""
    requirement = f'"winlauncher[rust] @ git+{REPOSITORY_URL}@{release.tag}"'
    if kind is Installation.PIPX:
        # `pipx upgrade` keeps the pinned tag, so the release is installed again.
        return f"pipx install --force {requirement}"
    return f"python -m pip install --upgrade {requirement}"
