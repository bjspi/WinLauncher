"""Release notes gather the commits since the previous release, in a real repository."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import release_notes

GIT_TIMEOUT_SECONDS = 60


class ReleaseNotesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="winlauncher-notes-")
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )

    def commit(self, message: str) -> None:
        self.git("commit", "--quiet", "--allow-empty", "-m", message)

    def notes(self, tag: str) -> str:
        previous = release_notes.previous_tag(tag, self.repository)
        commits = release_notes.commits_since(previous, tag, self.repository)
        return release_notes.release_notes(tag, previous, commits, "owner/name")

    def test_a_release_lists_its_changes_under_its_summary(self):
        self.commit("First feature")
        self.commit("Release 1.0.0: the first one")
        self.git("tag", "-a", "v1.0.0", "-m", "1.0.0")
        self.commit("Open files faster\n\n- Read ahead.\n- Cache\n  the icons.")
        self.commit("Fix a typo [skip ci]")
        self.commit("Release 1.0.1: faster opening")
        self.git("tag", "-a", "v1.0.1", "-m", "1.0.1")
        self.assertEqual(
            self.notes("v1.0.1"),
            "Faster opening\n\n"
            "## Changes\n\n"
            "- **Open files faster**\n\n"
            "  - Read ahead.\n"
            "  - Cache\n"
            "    the icons.\n\n"
            "- **Fix a typo**\n\n"
            "**Full Changelog**: https://github.com/owner/name/compare/v1.0.0...v1.0.1\n",
        )

    def test_the_first_release_has_every_commit_and_no_compare_link(self):
        self.commit("Start")
        self.commit("Release 0.1.0")
        self.git("tag", "-a", "v0.1.0", "-m", "0.1.0")
        notes = self.notes("v0.1.0")
        self.assertEqual(notes, "## Changes\n\n- **Start**\n")

    def test_a_release_commit_with_a_description_keeps_it(self):
        self.commit("Release 2.0.0: new search\n\nSearch now ranks by use.")
        self.git("tag", "-a", "v2.0.0", "-m", "2.0.0")
        self.assertEqual(self.notes("v2.0.0"), "New search\n\nSearch now ranks by use.\n")


if __name__ == "__main__":
    unittest.main()
