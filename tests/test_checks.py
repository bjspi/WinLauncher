"""Local checks must leave no Rust debug cache, including after a failed check."""

import subprocess
import unittest
from unittest.mock import Mock, patch

from tools import check


class LocalChecksTest(unittest.TestCase):
    def test_failed_check_still_cleans_both_crates_and_keeps_its_exit_code(self):
        with (
            patch.object(check.Path, "exists", return_value=True),
            patch.object(
                check.subprocess,
                "run",
                side_effect=[
                    subprocess.CalledProcessError(3, "ruff"),
                    Mock(returncode=0),
                    Mock(returncode=0),
                ],
            ) as run,
        ):
            self.assertEqual(check.main(), 3)
        self.assertEqual(run.call_count, 3)
        for call, crate in zip(run.call_args_list[1:], check.CRATES, strict=True):
            self.assertEqual(
                call.args[0],
                [
                    "cargo",
                    "clean",
                    "--manifest-path",
                    str(check.ROOT / crate / "Cargo.toml"),
                    "--target-dir",
                    str(check.ROOT / crate / "target"),
                    "--profile",
                    "dev",
                ],
            )

    def test_cleanup_failure_is_reported_even_when_all_checks_pass(self):
        def command_result(command, **kwargs):
            return Mock(returncode=1 if command[:2] == ["cargo", "clean"] else 0)

        with (
            patch.object(check.Path, "exists", return_value=True),
            patch.object(check.subprocess, "run", side_effect=command_result) as run,
        ):
            self.assertEqual(check.main(), 1)
        engines = [
            call.kwargs["env"]["WINLAUNCHER_SEARCH_ENGINE"]
            for call in run.call_args_list
            if "env" in call.kwargs
        ]
        self.assertEqual(engines, ["python", "native"])


if __name__ == "__main__":
    unittest.main()
