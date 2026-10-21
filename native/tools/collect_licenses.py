"""Write the license texts of every Rust crate the native wheel links statically.

    python native/tools/collect_licenses.py

Run after changing `Cargo.lock`; the result, `native/THIRD_PARTY_LICENSES.txt`,
is shipped inside the wheel.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[1]
OUTPUT = NATIVE / "THIRD_PARTY_LICENSES.txt"
LICENSE_FILE_PREFIXES = ("license", "licence", "copying", "notice", "unlicense")
SQLITE_NOTICE = (
    "SQLite (bundled by libsqlite3-sys) is in the public domain:\n"
    "The author disclaims copyright to this source code. In place of a legal notice,\n"
    "here is a blessing: May you do good and not evil. May you find forgiveness for\n"
    "yourself and forgive others. May you share freely, never taking more than you give.\n"
)
RULE = "=" * 78


def locked_packages() -> list[dict]:
    output = subprocess.check_output(
        [
            "cargo",
            "metadata",
            "--format-version",
            "1",
            "--locked",
            "--manifest-path",
            str(NATIVE / "Cargo.toml"),
        ],
        text=True,
    )
    metadata = json.loads(output)
    own = {member for member in metadata["workspace_members"]}
    packages = [package for package in metadata["packages"] if package["id"] not in own]
    return sorted(packages, key=lambda package: (package["name"], package["version"]))


def license_files(package: dict) -> list[Path]:
    folder = Path(package["manifest_path"]).parent
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.name.lower().startswith(LICENSE_FILE_PREFIXES)
    )


def section(package: dict) -> str:
    heading = f"{package['name']} {package['version']} ({package.get('license') or 'see below'})"
    texts = [
        path.read_text(encoding="utf-8", errors="replace").strip()
        for path in license_files(package)
    ]
    body = "\n\n".join(texts) or f"License: {package.get('license')}"
    return f"{RULE}\n{heading}\n{RULE}\n\n{body}\n"


def main() -> None:
    packages = locked_packages()
    parts = [
        "Third-party licenses of the WinLauncher native search engine\n",
        "The native wheel statically links the Rust crates below and SQLite.\n",
        f"{RULE}\nSQLite\n{RULE}\n\n{SQLITE_NOTICE}",
        *(section(package) for package in packages),
    ]
    OUTPUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {OUTPUT} for {len(packages)} crates")


if __name__ == "__main__":
    main()
