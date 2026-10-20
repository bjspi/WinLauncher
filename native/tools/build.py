"""Build the native wheel without embedding local paths such as the user folder.

Rust stores source paths (for panic messages) in the binary. This script remaps
the checkout, the Cargo and rustup homes and the user folder to neutral names,
then runs `maturin build`. Extra arguments go to maturin, for example:

    python native/tools/build.py --out dist

maturin also writes a CycloneDX SBOM into the wheel whose references name the
checkout folder; those are rewritten to the same neutral name afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[1]
NEUTRAL_NATIVE = "/pathscout-native"
DEFAULT_OUTPUT = NATIVE / "target" / "wheels"
FLAG_SEPARATOR = "\x1f"  # CARGO_ENCODED_RUSTFLAGS keeps paths with spaces intact.


def remapped_prefixes() -> list[tuple[Path, str]]:
    """Most specific last: rustc applies the last matching remapping."""
    home = Path.home()
    cargo = Path(os.environ.get("CARGO_HOME", home / ".cargo"))
    rustup = Path(os.environ.get("RUSTUP_HOME", home / ".rustup"))
    return [(home, "/home"), (rustup, "/rustup"), (cargo, "/cargo"), (NATIVE, NEUTRAL_NATIVE)]


def encoded_rustflags() -> str:
    existing = os.environ.get("CARGO_ENCODED_RUSTFLAGS", "")
    flags = [flag for flag in existing.split(FLAG_SEPARATOR) if flag]
    flags += [f"--remap-path-prefix={path}={name}" for path, name in remapped_prefixes()]
    return FLAG_SEPARATOR.join(flags)


def maturin_command() -> list[str]:
    if importlib.util.find_spec("maturin") is not None:
        return [sys.executable, "-m", "maturin"]
    if uvx := shutil.which("uvx"):
        return [uvx, "maturin"]
    raise SystemExit("Install maturin (pip install maturin) or uv to build the native wheel.")


def output_folder(arguments: list[str]) -> Path:
    for index, argument in enumerate(arguments):
        if argument in ("--out", "-o") and index + 1 < len(arguments):
            return Path(arguments[index + 1])
        if argument.startswith("--out="):
            return Path(argument.removeprefix("--out="))
    return DEFAULT_OUTPUT


def record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode()},{len(data)}"


def neutralize_sboms(wheel: Path) -> bool:
    """Replace the checkout's file URL in SBOMs and keep RECORD consistent."""
    checkout = NATIVE.as_uri().encode()  # file:///D:/.../native
    neutral = f"file://{NEUTRAL_NATIVE}".encode()
    with zipfile.ZipFile(wheel) as package:
        entries = [(info, package.read(info)) for info in package.infolist()]
    changed = {
        info.filename: data.replace(checkout, neutral)
        for info, data in entries
        if "/sboms/" in info.filename and checkout in data
    }
    if not changed:
        return False
    record_info, record = next(
        (info, data) for info, data in entries if info.filename.endswith(".dist-info/RECORD")
    )
    lines = []
    for line in record.decode().splitlines():
        name = line.split(",", 1)[0]
        lines.append(f"{name},{record_hash(changed[name])}" if name in changed else line)
    changed[record_info.filename] = "".join(f"{line}\n" for line in lines).encode()
    rewritten = wheel.with_suffix(".tmp")
    with zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as package:
        for info, data in entries:
            package.writestr(info, changed.get(info.filename, data))
    rewritten.replace(wheel)
    return True


def main() -> int:
    environment = {**os.environ, "CARGO_ENCODED_RUSTFLAGS": encoded_rustflags()}
    environment.pop("RUSTFLAGS", None)  # Ignored by Cargo when the encoded form is set.
    command = [
        *maturin_command(),
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        str(NATIVE / "Cargo.toml"),
        *sys.argv[1:],
    ]
    started = time.time()
    status = subprocess.call(command, env=environment)
    if status == 0:
        for wheel in output_folder(sys.argv[1:]).glob("pathscout_native-*.whl"):
            if wheel.stat().st_mtime >= started - 1 and neutralize_sboms(wheel):
                print(f"Removed the checkout path from the SBOM in {wheel.name}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
