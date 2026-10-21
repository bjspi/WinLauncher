"""Read-only git queries for self-updating a source checkout. No Qt here."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from winlauncher.platform.startup import PACKAGE_PARENT

GIT_QUERY_TIMEOUT_SECONDS = 10
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # pythonw must not flash a console.


def checkout_root() -> Path | None:
    """The source checkout WinLauncher runs from, or None for an installed package.

    Only a `.git` entry directly beside the `winlauncher` package counts, so a copy
    inside some unrelated repository never updates that repository.
    """
    if getattr(sys, "frozen", False):
        return None
    return PACKAGE_PARENT if (PACKAGE_PARENT / ".git").exists() else None


def git_available() -> bool:
    return shutil.which("git") is not None


def current_head(root: Path) -> str | None:
    return _git_output(root, "rev-parse", "HEAD")


def remote_url(root: Path) -> str | None:
    """The upstream remote's URL, for display in the About dialog."""
    return _git_output(root, "remote", "get-url", "origin")


def git_environment() -> dict[str, str]:
    """Git must never wait for a password on a hidden console."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}


def _git_output(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
            env=git_environment(),
            creationflags=NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None
