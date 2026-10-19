"""Check built distributions against the workspace and reject private artifacts.

Run after building both archives into `dist/`:
    python tools/verify_packages.py
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_FOLDERS = {".analysis", ".git", ".venv", "__pycache__", ".runtime"}
PRIVATE_NAMES = {".nobackup", ".no-backup"}
PRIVATE_SUFFIXES = {".exe", ".dll", ".gzf", ".pyc", ".sqlite3", ".log", ".png"}
REQUIRED_NAMES = {"LICENSE", "THIRD_PARTY_NOTICES.md"}
SDIST_SAMPLES = ("docs/architecture.md", "docs/review.md", "tests/test_ui_flow.py")


def wheel_files(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as package:
        names = [name for name in package.namelist() if not name.endswith("/")]
        return {name: package.read(name) for name in names}


def sdist_files(archive: Path) -> dict[str, bytes]:
    """Contents keyed by path below the archive's top-level folder."""
    files = {}
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            if member.isfile():
                relative = member.name.split("/", 1)[1]
                files[relative] = package.extractfile(member).read()
    return files


def check_sources_current(files: dict[str, bytes], names: list[str]) -> None:
    for name in names:
        assert name in files, f"Missing from package: {name}"
        assert files[name] == (ROOT / name).read_bytes(), f"Outdated in package: {name}"


def check_no_private_artifacts(files: dict[str, bytes]) -> None:
    for name in files:
        path = Path(name)
        assert not PRIVATE_FOLDERS.intersection(path.parts), f"Private folder: {name}"
        assert path.name not in PRIVATE_NAMES, f"Backup marker: {name}"
        assert path.suffix not in PRIVATE_SUFFIXES, f"Binary or private artifact: {name}"


def check_licenses(files: dict[str, bytes]) -> None:
    present = {Path(name).name for name in files}
    missing = REQUIRED_NAMES - present
    assert not missing, f"Missing license files: {', '.join(sorted(missing))}"


def verify(archive: Path) -> int:
    if archive.suffix == ".whl":
        files = wheel_files(archive)
        sources = [p.relative_to(ROOT).as_posix() for p in (ROOT / "pathscout").rglob("*.py")]
        check_sources_current(files, sources)
    else:
        files = sdist_files(archive)
        check_sources_current(files, list(SDIST_SAMPLES))
    check_no_private_artifacts(files)
    check_licenses(files)
    return len(files)


def main() -> int:
    archives = sorted((ROOT / "dist").glob("pathscout-*"))
    archives = [a for a in archives if a.suffix == ".whl" or a.name.endswith(".tar.gz")]
    if not archives:
        print("No distributions found in dist/", file=sys.stderr)
        return 1
    for archive in archives:
        count = verify(archive)
        print(
            f"Verified {archive.name}: {count} files, current sources, licenses, no private files"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
