"""Finish a source checkout update by reinstalling its packages at the next start.

The update moves the checkout to a new release, whose Rust engine and
dependencies may differ. Windows locks the running engine's `.pyd`, so the
updater only marks the reinstall as pending; the next start runs it before
anything loads the engine. A checkout moved by hand, which skips that step, is
repaired the same way once its installed engine is missing or of another release.
No Qt here.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

from winlauncher import __version__

PENDING_MARKER = "reinstall-pending"  # Holds the number of starts that tried it.
REPAIR_MARKER = "engine-reinstall"  # "<version> <starts that tried it>"
RUST_EXTRA_BEFORE = (3, 15)  # The `rust` extra's python_version marker in pyproject.toml.
FITS_MARKER = "engine-fits"  # "<engine version> <WinLauncher version>" the probe accepted.
PROBE_TIMEOUT_SECONDS = 30
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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


def take_engine_repair(data_dir: Path) -> bool:
    """Whether this start should reinstall a checkout whose Rust engine is missing or old.

    That happens when the checkout was moved by hand, such as with `git pull` or
    `git reset`, which skips the reinstall an update from WinLauncher schedules.
    Each WinLauncher version tries it at most `REINSTALL_ATTEMPTS` times.
    """
    marker = data_dir / REPAIR_MARKER
    if not engine_outdated(data_dir):
        marker.unlink(missing_ok=True)
        return False
    try:
        version, _, count = marker.read_text(encoding="ascii").partition(" ")
        attempts = int(count) if version == __version__ else 0
    except (OSError, ValueError):
        attempts = 0
    if attempts >= REINSTALL_ATTEMPTS:
        return False
    marker.write_text(f"{__version__} {attempts + 1}", encoding="ascii")
    return True


def engine_outdated(data_dir: Path) -> bool:
    """Whether the Rust engine the `rust` extra installs is missing or does not fit.

    An engine of another version still fits when its API matches, as a locally
    built one may; that answer is remembered for the pair of versions.
    """
    if sysconfig.get_platform() != "win-amd64" or sys.version_info >= RUST_EXTRA_BEFORE:
        return False  # The extra installs nothing here; see pyproject.toml.
    try:
        installed = metadata.version("winlauncher_native")
    except metadata.PackageNotFoundError:
        return True
    if installed == __version__:
        return False
    marker, pair = data_dir / FITS_MARKER, f"{installed} {__version__}"
    try:
        if marker.read_text(encoding="ascii") == pair:
            return False
    except (OSError, ValueError):
        pass
    if not _engine_fits():
        return True
    marker.write_text(pair, encoding="ascii")
    return False


def _engine_fits() -> bool:
    """Import the engine in another process, so this one never locks its `.pyd`."""
    from winlauncher.search.native_engine import API_VERSION

    code = f"import winlauncher_native as n; raise SystemExit(n.API_VERSION != {API_VERSION})"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # Unknown: the search reports a real problem itself.
    return result.returncode == 0


def finish_reinstall(data_dir: Path) -> None:
    (data_dir / PENDING_MARKER).unlink(missing_ok=True)


def reinstall_arguments() -> list[str]:
    """Arguments for this interpreter: the checkout and its Rust engine extra."""
    return ["-m", "pip", "install", "--disable-pip-version-check", "--quiet", "-e", ".[rust]"]


def reinstall_command(root: Path) -> str:
    """The same reinstall for the user to run by hand, in a console that shows pip."""
    python = Path(sys.executable)
    if python.name.casefold() == "pythonw.exe" and python.with_name("python.exe").is_file():
        python = python.with_name("python.exe")
    return f'cd "{root}" && "{python}" {" ".join(reinstall_arguments())}'
