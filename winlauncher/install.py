"""Finish a source checkout update by reinstalling its packages at the next start.

The update moves the checkout to a new release, whose Rust engine and
dependencies may differ. Windows locks the running engine's `.pyd`, so the
updater only marks the reinstall as pending; the next start runs it before
anything loads the engine. No Qt here.
"""

from __future__ import annotations

import sys
from pathlib import Path

PENDING_MARKER = "reinstall-pending"
REINSTALL_TIMEOUT_MS = 10 * 60 * 1000  # Downloads the release's engine wheel.


def mark_reinstall_pending(data_dir: Path) -> None:
    (data_dir / PENDING_MARKER).touch()


def take_pending_reinstall(data_dir: Path) -> bool:
    """Whether a reinstall is pending; it is cleared, so a failure is not retried forever."""
    marker = data_dir / PENDING_MARKER
    if not marker.exists():
        return False
    marker.unlink(missing_ok=True)
    return True


def reinstall_arguments() -> list[str]:
    """Arguments for this interpreter: the checkout and its Rust engine extra."""
    return ["-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-e", ".[rust]"]


def reinstall_command(root: Path) -> str:
    """The same reinstall for the user to run by hand."""
    return f'cd "{root}" && "{sys.executable}" {" ".join(reinstall_arguments())}'
