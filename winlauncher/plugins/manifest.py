"""A plugin folder's `plugin.toml`: what WinLauncher knows about a plugin without running it.

The loader reads the version; a plugin catalog also shows the name, description
and the Python packages the plugin needs before anyone installs it. Every field
is optional, so a manifest holding only `version = "1.0.0"` stays valid.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILE = "plugin.toml"


class ManifestError(ValueError):
    """The manifest exists but cannot be used; the message names the mistake."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    version: str = ""
    name: str = ""  # Shown in a catalog; the plugin's label once it is loaded.
    description: str = ""
    # Python packages to install beside WinLauncher, as pip requirements.
    requires: tuple[str, ...] = ()


def read_manifest(file: Path) -> PluginManifest:
    """The manifest in `file`; raises ManifestError when it is unreadable or mistyped."""
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
