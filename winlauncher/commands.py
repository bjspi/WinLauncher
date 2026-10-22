"""Commands the user adds to the result context menu, such as Open in Terminal."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Replaced in one pass, so a path that contains `{name}` is never substituted again.
_PLACEHOLDER = re.compile(r"\{(path|folder|name)\}")
_ENVIRONMENT_VARIABLE = re.compile(r"%([^%\s]+)%")


@dataclass(frozen=True, slots=True)
class MenuCommand:
    """A command line run for a folder or file result.

    `{path}` is the result, `{folder}` the result if it is a folder and otherwise
    its folder, `{name}` its name; `%VARIABLE%` takes the environment's value. The
    program starts in `{folder}`.
    """

    title: str
    command: str
    folders: bool = True
    files: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Give each command a title.")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError(f"Give the command {self.title!r} a command line.")
        if any(ord(c) < 32 for c in self.command):
            raise ValueError(f"The command line of {self.title!r} must be a single line.")
        if type(self.folders) is not bool or type(self.files) is not bool:
            raise ValueError("Command folders/files flags must be JSON booleans.")
        if not (self.folders or self.files):
            raise ValueError(f"Show the command {self.title!r} for folders, files or both.")

    def command_line(self, path: Path, folder: Path) -> str:
        """The command line for `path`; `folder` is where it starts."""
        values = {"path": _argument(path), "folder": _argument(folder), "name": path.name}
        line = _ENVIRONMENT_VARIABLE.sub(
            lambda match: os.environ.get(match[1], match[0]), self.command
        )
        return _PLACEHOLDER.sub(lambda match: values[match[1]], line)


def _argument(path: Path) -> str:
    """`path` without a trailing backslash: in `"D:\\"` it would escape the quote."""
    text = str(path)
    return f"{text}." if text.endswith("\\") else text


DEFAULT_MENU_COMMANDS = (
    MenuCommand("Open in Windows Terminal", 'wt.exe -d "{folder}"'),
    # As Explorer's own "Open command window here"; pushd also maps network folders.
    MenuCommand("Open Command Prompt here", 'cmd.exe /k pushd "{folder}"'),
)
