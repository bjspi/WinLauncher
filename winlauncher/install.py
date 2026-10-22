"""Finish a source checkout update by reinstalling its packages at the next start.

The update moves the checkout to a new release, whose Rust engine and
dependencies may differ. Windows locks the running engine's `.pyd`, so the
updater only marks the reinstall as pending; the next start runs it before
anything loads the engine. No Qt here.
"""

from __future__ import annotations

import sys
from pathlib import Path

PENDING_MARKER = "reinstall-pending"  # Holds the number of starts that tried it.
REINSTALL_TIMEOUT_MS = 10 * 60 * 1000  # Downloads the release's engine wheel.
REINSTALL_ATTEMPTS = 3  # Starts that try a failing reinstall, such as one made offline.


def mark_reinstall_pending(data_dir: Path) -> None:
    (data_dir / PENDING_MARKER).write_text("0", encoding="ascii")


def take_pending_reinstall(data_dir: Path) -> bool:
    """Whether this start should reinstall; it counts as one attempt.

    The marker stays until `finish_reinstall`, so a failed reinstall is tried
    again at the next start, but at most `REINSTALL_ATTEMPTS` times in all.
    """
    marker = data_dir / PENDING_MARKER
    try:
        attempts = int(marker.read_text(encoding="ascii").strip() or 0)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        attempts = 0  # An older WinLauncher left an empty marker.
    if attempts >= REINSTALL_ATTEMPTS:
        marker.unlink(missing_ok=True)
        return False
    marker.write_text(str(attempts + 1), encoding="ascii")
    return True


def finish_reinstall(data_dir: Path) -> None:
    (data_dir / PENDING_MARKER).unlink(missing_ok=True)


def reinstall_arguments() -> list[str]:
    """Arguments for this interpreter: the checkout and its Rust engine extra."""
    return ["-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-e", ".[rust]"]


def reinstall_command(root: Path) -> str:
    """The same reinstall for the user to run by hand."""
    return f'cd "{root}" && "{sys.executable}" {" ".join(reinstall_arguments())}'
