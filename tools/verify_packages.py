"""Check built distributions against the workspace and reject private artifacts.

Run after building the archives into `dist/`:
    python tools/verify_packages.py

A native wheel (`winlauncher_native-*.whl`) in `dist/` is checked too: it must ship
its license files and must not contain paths of the machine that built it.
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
PUBLIC_ARTIFACTS = {"winlauncher/platform/bin/winlauncher-host.exe", "docs/images/icon.png"}
SDIST_SAMPLES = ("docs/architecture.md", "docs/specification.md", "tests/test_ui_flow.py")
NATIVE_LICENSES = {"LICENSE", "THIRD_PARTY_LICENSES.txt"}


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
        assert path.suffix not in PRIVATE_SUFFIXES or name in PUBLIC_ARTIFACTS, (
            f"Binary or private artifact: {name}"
        )


def check_licenses(files: dict[str, bytes]) -> None:
    present = {Path(name).name for name in files}
    missing = REQUIRED_NAMES - present
    assert not missing, f"Missing license files: {', '.join(sorted(missing))}"


def verify(archive: Path) -> int:
    if archive.suffix == ".whl":
        files = wheel_files(archive)
        sources = [p.relative_to(ROOT).as_posix() for p in (ROOT / "winlauncher").rglob("*.py")]
        sources += [
            "winlauncher/platform/bin/winlauncher-host.exe",
            "winlauncher/platform/bin/winlauncher-host.json",
        ]
        check_sources_current(files, sources)
    else:
        files = sdist_files(archive)
        check_sources_current(files, list(SDIST_SAMPLES))
    check_no_private_artifacts(files)
    check_licenses(files)
    return len(files)


def build_paths() -> set[bytes]:
    """Local folders `native/tools/build.py` remaps; none may appear in the binary."""
    folders = {Path.home(), ROOT}
    texts = {str(folder) for folder in folders}
    texts |= {text.replace("\\", "/") for text in texts}
    return {text.encode() for text in texts} | {text.encode("utf-16-le") for text in texts}


def verify_native(archive: Path) -> int:
    files = wheel_files(archive)
    licenses = {Path(name).name for name in files if ".dist-info/licenses/" in name}
    missing = NATIVE_LICENSES - licenses
    assert not missing, f"{archive.name} misses license files: {', '.join(sorted(missing))}"
    for name, data in files.items():
        for path in build_paths():
            assert path not in data, f"{archive.name}: {name} contains a local build path"
    return len(files)


def main() -> int:
    from build_loader import OUTPUT, check_current

    check_current()
    for path in build_paths():
        assert path not in OUTPUT.read_bytes(), "The loader contains a local build path"
    for archive in sorted((ROOT / "dist").glob("winlauncher_native-*.whl")):
        count = verify_native(archive)
        print(f"Verified {archive.name}: {count} files, licenses, no local build paths")
    archives = sorted((ROOT / "dist").glob("winlauncher-*"))
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
