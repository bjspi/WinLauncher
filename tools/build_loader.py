"""Build the version-independent Windows host; keep the prebuilt template current.

The checked-in template makes pipx/git installs work without a Rust compiler.
Normal releases run --check and reuse it; rebuilding needs Rust and Windows SDK.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "loader"
OUTPUT = ROOT / "winlauncher" / "platform" / "bin" / "winlauncher-host.exe"
MANIFEST = OUTPUT.with_suffix(".json")
SOURCES = (
    "loader/Cargo.toml",
    "loader/Cargo.lock",
    "loader/src/main.rs",
    "loader/resources.rc",
    "tools/build_loader.py",
    "docs/images/icon.png",
)
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def source_hash() -> str:
    """Fingerprint only the host, its build instructions and its icon."""
    digest = hashlib.sha256()
    for name in SOURCES:
        path = ROOT / name
        data = (
            path.read_bytes()
            if path.suffix == ".png"
            else path.read_text(encoding="utf-8").encode()
        )
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()


def check_current() -> None:
    """Reject stale sources, accidental binary edits and unrecorded builds."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["sources"] != source_hash():
        raise ValueError("Loader sources changed; run python tools/build_loader.py on Windows.")
    if manifest["binary"] != hashlib.sha256(OUTPUT.read_bytes()).hexdigest():
        raise ValueError("Loader binary differs from its recorded build.")


def resource_compiler() -> Path:
    """Use the SDK installed with MSVC, even outside a Developer PowerShell."""
    if compiler := shutil.which("rc.exe"):
        return Path(compiler)
    sdk = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    candidates = list((sdk / "Windows Kits/10/bin").glob("*/x64/rc.exe"))
    if not candidates:
        raise FileNotFoundError("Install the Windows SDK (rc.exe) to build the loader.")
    return max(candidates, key=lambda path: tuple(map(int, path.parents[1].name.split("."))))


def write_icon(path: Path) -> None:
    """Give Windows native sizes instead of relying on its icon resampling."""
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QImage

    source = QImage(str(ROOT / "docs/images/icon.png"))
    if source.isNull() or source.width() < max(ICON_SIZES) or source.height() < max(ICON_SIZES):
        raise ValueError("The host icon must be at least 256 by 256 pixels.")
    entries = bytearray()
    images = bytearray()
    offset = 6 + 16 * len(ICON_SIZES)
    for size in ICON_SIZES:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled = source.scaled(
            size,
            size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if not scaled.save(buffer, "PNG"):
            raise OSError(f"Cannot encode the {size}px host icon.")
        png = bytes(buffer.data())
        dimension = size if size < 256 else 0
        entries.extend(
            struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset)
        )
        images.extend(png)
        offset += len(png)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(ICON_SIZES)) + entries + images)


def build() -> None:
    """Compile the resource and dependency-free Rust host without local build paths."""
    staging = LOADER / "target" / "resources"
    staging.mkdir(parents=True, exist_ok=True)
    marker = LOADER / "target" / ".nobackup"
    if not marker.exists():
        marker.touch()
    write_icon(staging / "winlauncher.ico")
    # Cargo does not track linker-input contents; a changed name forces relinking.
    resource = staging / f"winlauncher-{source_hash()[:16]}.res"
    subprocess.run(
        [str(resource_compiler()), "/nologo", "/fo", str(resource), str(LOADER / "resources.rc")],
        cwd=staging,
        check=True,
    )
    flags = [
        f"--remap-path-prefix={Path.home()}=/home",
        f"--remap-path-prefix={ROOT}=/winlauncher",
    ]
    environment = {**os.environ, "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(flags)}
    subprocess.run(
        [
            "cargo",
            "rustc",
            "--release",
            "--locked",
            "--manifest-path",
            str(LOADER / "Cargo.toml"),
            "--",
            "-C",
            f"link-arg={resource}",
        ],
        env=environment,
        check=True,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LOADER / "target/release/winlauncher-host.exe", OUTPUT)
    manifest = {"sources": source_hash(), "binary": hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Verify the prebuilt host; never compile"
    )
    args = parser.parse_args()
    if not args.check:
        build()
    check_current()
    print(f"Verified loader: {OUTPUT.name} ({OUTPUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
