"""Commands the user adds to the result context menu, such as Open in Terminal."""

from __future__ import annotations

import os
import re
import shutil
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
    # An .ico, .png, .svg, .webp, .exe or .dll file; empty means the icon of the program it runs.
    icon: str = ""

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
        if not isinstance(self.icon, str) or any(ord(c) < 32 for c in self.icon):
            raise ValueError(f"The icon of {self.title!r} must be a file path.")

    def command_line(self, path: Path, folder: Path) -> str:
        """The command line for `path`; `folder` is where it starts."""
        values = {"path": _argument(path), "folder": _argument(folder), "name": path.name}
        return _PLACEHOLDER.sub(lambda match: values[match[1]], _expand(self.command))

    def icon_file(self) -> Path | None:
        """The file whose icon the menu shows: the chosen one, or the program's if found."""
        if self.icon.strip():
            return Path(_expand(self.icon.strip().strip('"')))
        line = _expand(self.command).strip()
        # As CreateProcess reads it: a quoted program name, or up to the first space.
        program = line[1:].partition('"')[0] if line.startswith('"') else line.split(" ")[0]
        found = shutil.which(program) if program else None
        return Path(found) if found else None


def _expand(text: str) -> str:
    """`%VARIABLE%` references with the environment's value; unknown ones stay as typed."""
    return _ENVIRONMENT_VARIABLE.sub(lambda match: os.environ.get(match[1], match[0]), text)


def _argument(path: Path) -> str:
    """`path` without a trailing backslash: in `"D:\\"` it would escape the quote."""
    text = str(path)
    return f"{text}." if text.endswith("\\") else text


DEFAULT_MENU_COMMANDS = (
    MenuCommand("Open in Windows Terminal", 'wt.exe -d "{folder}"'),
    # As Explorer's own "Open command window here"; pushd also maps network folders.
    MenuCommand("Open Command Prompt here", 'cmd.exe /k pushd "{folder}"'),
)
