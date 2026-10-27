"""Run local checks and always remove their temporary Rust debug builds."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATES = ("native", "loader")


def main() -> int:
    """Stop on the first failure; cleanup runs on success, failure and Ctrl+C."""
    result = 0
    try:
        for action in ("check", "format"):
            command = [sys.executable, "-m", "ruff", action]
            if action == "format":
                command.append("--check")
            subprocess.run(
                command + ["winlauncher", "tests", "tools", "native/tools"], cwd=ROOT, check=True
            )
        subprocess.run([sys.executable, "tools/build_loader.py", "--check"], cwd=ROOT, check=True)
        for crate in CRATES:
            manifest = str(ROOT / crate / "Cargo.toml")
            subprocess.run(["cargo", "fmt", "--manifest-path", manifest, "--check"], check=True)
            target = str(ROOT / crate / "target")
            for action in ("test", "clippy"):
                command = [
                    "cargo",
                    action,
                    "--manifest-path",
                    manifest,
                    "--locked",
                    "--target-dir",
                    target,
                ]
                if action == "clippy":
                    command += ["--all-targets", "--", "-D", "warnings"]
                subprocess.run(command, cwd=ROOT, check=True)
        for engine in ("python", "native"):
            subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=ROOT,
                env={**os.environ, "WINLAUNCHER_SEARCH_ENGINE": engine},
                check=True,
            )
    except subprocess.CalledProcessError as exc:
        result = exc.returncode
    finally:
        for crate in CRATES:
            target = ROOT / crate / "target"
            if not (target / "debug").exists():
                continue
            cleanup = subprocess.run(
                [
                    "cargo",
                    "clean",
                    "--manifest-path",
                    str(ROOT / crate / "Cargo.toml"),
                    "--target-dir",
                    str(target),
                    "--profile",
                    "dev",
                ],
                cwd=ROOT,
                check=False,
            )
            result = result or cleanup.returncode
    return result


if __name__ == "__main__":
    raise SystemExit(main())
