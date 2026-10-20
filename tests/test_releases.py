"""Release tags, installation kinds, update commands and the pending reinstall marker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pathscout.install import mark_reinstall_pending, take_pending_reinstall
from pathscout.releases import (
    Installation,
    Release,
    install_command,
    installation,
    newest_release,
    remote_tags,
)


class ReleaseTest(unittest.TestCase):
    def test_newest_release_compares_numbers_and_ignores_other_tags(self):
        tags = ["v0.2.2", "v0.10.0", "v0.9.9", "v1.0-beta", "latest", "0.11.0"]
        self.assertEqual(newest_release(tags), Release(0, 10, 0))
        self.assertIsNone(newest_release(["nightly"]))
        self.assertEqual(Release.from_version("0.2.2").tag, "v0.2.2")
        self.assertIsNone(Release.from_version("0.3.0.dev1"))

    def test_remote_tags_read_ls_remote_output(self):
        output = "abc123\trefs/tags/v0.2.2\ndef456\trefs/tags/v0.3.0\n789abc\trefs/heads/main"
        self.assertEqual(remote_tags(output), ["v0.2.2", "v0.3.0"])
        self.assertEqual(remote_tags(""), [])

    def test_installation_kind_and_its_update_command(self):
        pipx = Path(r"C:\Users\me\pipx\venvs\pathscout")
        self.assertIs(installation(Path("D:/src/PathScout"), pipx), Installation.CHECKOUT)
        self.assertIs(installation(None, pipx), Installation.PIPX)
        self.assertIs(installation(None, Path(r"C:\Python312")), Installation.PACKAGE)
        release = Release(0, 3, 0)
        self.assertEqual(
            install_command(Installation.PIPX, release),
            'pipx install --force "pathscout[rust] @ '
            'git+https://github.com/bjspi/PathScout.git@v0.3.0"',
        )
        self.assertIn("pip install --upgrade", install_command(Installation.PACKAGE, release))

    def test_pending_reinstall_is_taken_once(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self.assertFalse(take_pending_reinstall(data_dir))
            mark_reinstall_pending(data_dir)
            self.assertTrue(take_pending_reinstall(data_dir))
            self.assertFalse(take_pending_reinstall(data_dir))


if __name__ == "__main__":
    unittest.main()
