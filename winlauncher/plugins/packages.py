"""Python packages a plugin needs, installed into WinLauncher's own environment.

pipx keeps WinLauncher in an environment of its own, so packages are added with
`pipx inject`; any other installation runs pip with the interpreter WinLauncher
runs on. Installing a package that is already there is quick and changes nothing.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from winlauncher.platform.git import NO_CONSOLE
from winlauncher.plugins.encoding import decode_text
from winlauncher.releases import Installation

PIPX_APP = "winlauncher"
# pip and pipx are Python programs: without this they write in the console's code page.
PYTHON_OUTPUT_ENVIRONMENT = {"PYTHONIOENCODING": "utf-8"}
INSTALL_TIMEOUT_SECONDS = 600  # Large wheels on a slow company network take a while.
_PLAIN_ARGUMENT = re.compile(r"[\w.:\\/-]+")  # Needs no quotes in a shell.
logger = logging.getLogger(__name__)


class PackageError(Exception):
    """The packages could not be installed; the message includes the command to run."""


def install_arguments(
    kind: Installation, requirements: Sequence[str], python: Path = Path(sys.executable)
) -> list[str]:
    if kind is Installation.PIPX:
        return ["pipx", "inject", PIPX_APP, *requirements]
    # The tray app runs on pythonw.exe; pip wants the console interpreter beside it.
    console = python.with_name("python.exe") if python.name.casefold() == "pythonw.exe" else python
    return [str(console), "-m", "pip", "install", *requirements]


def install_command(
    kind: Installation, requirements: Sequence[str], python: Path = Path(sys.executable)
) -> str:
    """The same installation as a command line a user can run in a terminal."""
    return " ".join(_quoted(argument) for argument in install_arguments(kind, requirements, python))


def install_packages(kind: Installation, requirements: Sequence[str]) -> None:
    """Install `requirements` beside WinLauncher; raises PackageError when that fails."""
    if not requirements:
        return
    arguments = install_arguments(kind, requirements)
    command = install_command(kind, requirements)
    logger.info("Installing plugin packages: %s", command)
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            env={**os.environ, **PYTHON_OUTPUT_ENVIRONMENT},
            creationflags=NO_CONSOLE,
        )
    except FileNotFoundError:
        raise PackageError(f"{arguments[0]} was not found. Run this yourself:\n{command}") from None
    except subprocess.TimeoutExpired:
        raise PackageError(
            f"Installing the packages took longer than {INSTALL_TIMEOUT_SECONDS} seconds. "
            f"Run this yourself:\n{command}"
        ) from None
    except OSError as exc:
        raise PackageError(f"{exc.strerror or exc}. Run this yourself:\n{command}") from None
    if result.returncode != 0:
        output = decode_text(result.stderr or result.stdout)
        lines = [line for line in output.splitlines() if line.strip()]
        reason = lines[-1].strip() if lines else f"exit code {result.returncode}"
        raise PackageError(
            f"Installing the packages failed: {reason}\nRun this yourself:\n{command}"
        )


def _quoted(argument: str) -> str:
    return argument if _PLAIN_ARGUMENT.fullmatch(argument) else f'"{argument}"'
