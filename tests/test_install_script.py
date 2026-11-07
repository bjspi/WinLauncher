"""The one-line installer must survive `irm ... | iex` and follow the package's limits."""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.ps1"


class InstallScriptTests(unittest.TestCase):
    def test_stays_ascii_because_powershell_5_decodes_downloads_as_latin1(self) -> None:
        self.assertTrue(SCRIPT.read_bytes().isascii())

    def test_never_exits_because_that_would_close_the_users_terminal(self) -> None:
        text = SCRIPT.read_text(encoding="ascii")
        self.assertIsNone(re.search(r"^\s*exit\b", text, re.MULTILINE))

    def test_python_range_matches_the_package(self) -> None:
        text = SCRIPT.read_text(encoding="ascii")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        rust_extra = project["optional-dependencies"]["rust"][0]
        self.assertIn(f"$oldestMinor = {_oldest_minor(project)} ", text)
        self.assertIn(f"$newestMinor = {_newest_minor(rust_extra)} ", text)


def _oldest_minor(project: dict) -> int:
    return int(re.fullmatch(r">=3\.(\d+)", project["requires-python"]).group(1))


def _newest_minor(rust_extra: str) -> int:
    """The `rust` extra names the first Python it no longer covers."""
    return int(re.search(r"python_version < '3\.(\d+)'", rust_extra).group(1)) - 1


if __name__ == "__main__":
    unittest.main()
