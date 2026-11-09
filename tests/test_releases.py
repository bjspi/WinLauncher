"""Release tags, installation kinds, update commands and the pending reinstall marker."""

from __future__ import annotations

import importlib.util
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from winlauncher import __version__, install
from winlauncher.install import (
    REINSTALL_ATTEMPTS,
    finish_reinstall,
    mark_reinstall_pending,
    take_engine_repair,
    take_pending_reinstall,
)
from winlauncher.releases import (
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
        pipx = Path(r"C:\Users\me\pipx\venvs\winlauncher")
        self.assertIs(installation(Path("D:/src/WinLauncher"), pipx), Installation.CHECKOUT)
        self.assertIs(installation(None, pipx), Installation.PIPX)
        self.assertIs(installation(None, Path(r"C:\Python312")), Installation.PACKAGE)
        release = Release(0, 3, 0)
        self.assertEqual(
            install_command(Installation.PIPX, release),
            'pipx install --force "winlauncher[rust] @ '
            'git+https://github.com/bjspi/WinLauncher.git@v0.3.0"',
        )
        self.assertIn("pip install --upgrade", install_command(Installation.PACKAGE, release))

    def test_the_rust_extra_names_this_releases_engine_wheel(self):
        project = Path(__file__).parents[1] / "pyproject.toml"
        extra = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        rust = extra["optional-dependencies"]["rust"][0]
        url = Release.from_version(__version__).native_wheel_url
        self.assertIn(f"@ {url} ;", rust)
        self.assertEqual(
            Release(0, 9, 1).native_wheel_url,
            "https://github.com/bjspi/WinLauncher/releases/download/v0.9.1/"
            "winlauncher_native-0.9.1-cp311-abi3-win_amd64.whl",
        )

    def test_a_failed_reinstall_is_tried_again_at_later_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self.assertFalse(take_pending_reinstall(data_dir))
            mark_reinstall_pending(data_dir)
            for _attempt in range(REINSTALL_ATTEMPTS):
                self.assertTrue(take_pending_reinstall(data_dir))  # Each start fails.
            self.assertFalse(take_pending_reinstall(data_dir))  # Then it gives up.
            mark_reinstall_pending(data_dir)
            self.assertTrue(take_pending_reinstall(data_dir))
            finish_reinstall(data_dir)  # It succeeded.
            self.assertFalse(take_pending_reinstall(data_dir))
            (data_dir / "reinstall-pending").touch()  # Left by an older WinLauncher.
            self.assertTrue(take_pending_reinstall(data_dir))

    def test_a_missing_or_old_engine_is_reinstalled_a_few_times_per_version(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(install, "engine_outdated", return_value=True),
        ):
            data_dir = Path(directory)
            for _attempt in range(REINSTALL_ATTEMPTS):
                self.assertTrue(take_engine_repair(data_dir))  # Each start fails.
            self.assertFalse(take_engine_repair(data_dir))  # Then this version gives up.
            marker = data_dir / install.REPAIR_MARKER
            marker.write_text(f"0.0.1 {REINSTALL_ATTEMPTS}", encoding="ascii")
            self.assertTrue(take_engine_repair(data_dir))  # A new version tries again.
            with patch.object(install, "engine_outdated", return_value=False):
                self.assertFalse(take_engine_repair(data_dir))
            self.assertFalse(marker.exists())

    def test_an_engine_of_another_version_is_kept_while_its_api_fits(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(install.sysconfig, "get_platform", return_value="win-amd64"),
            patch.object(install, "RUST_EXTRA_BEFORE", (99, 0)),
        ):
            data_dir = Path(directory)
            with patch.object(install.metadata, "version", return_value=__version__):
                self.assertFalse(install.engine_outdated(data_dir))
            missing = install.metadata.PackageNotFoundError("winlauncher_native")
            with patch.object(install.metadata, "version", side_effect=missing):
                self.assertTrue(install.engine_outdated(data_dir))
            with (
                patch.object(install.metadata, "version", return_value="0.6.0"),
                patch.object(install, "_engine_fits", return_value=False),
            ):
                self.assertTrue(install.engine_outdated(data_dir))
            with (
                patch.object(install.metadata, "version", return_value="0.6.0"),
                patch.object(install, "_engine_fits", return_value=True) as probe,
            ):
                self.assertFalse(install.engine_outdated(data_dir))
                self.assertFalse(install.engine_outdated(data_dir))
                probe.assert_called_once()  # The answer is remembered for these versions.
        with patch.object(install, "RUST_EXTRA_BEFORE", (3, 0)):
            self.assertFalse(install.engine_outdated(Path(".")))  # The extra installs nothing.

    def test_the_manual_reinstall_runs_the_console_python(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("python.exe", "pythonw.exe"):
                (folder / name).touch()
            with patch.object(install.sys, "executable", str(folder / "pythonw.exe")):
                command = install.reinstall_command(Path("C:/WinLauncher"))
        self.assertIn(f'"{folder / "python.exe"}" -m pip install', command)

    def test_the_probe_imports_the_installed_engine_in_another_process(self):
        if importlib.util.find_spec("winlauncher_native") is None:
            self.skipTest("The Rust engine is not installed")
        self.assertTrue(install._engine_fits())


if __name__ == "__main__":
    unittest.main()
