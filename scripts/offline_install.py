#!/usr/bin/env python3
"""Build and install a platform-specific Hermes wheelhouse."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tomllib
import venv
from pathlib import Path
from typing import Any


MANIFEST = "offline-manifest.json"


def _runtime() -> dict[str, str]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": sys.platform,
        "machine": platform.machine().lower(),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extras(value: str) -> list[str]:
    return [extra.strip() for extra in value.split(",") if extra.strip()]


def _spec(name: str, extras: list[str], version: str | None = None) -> str:
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"{name}{suffix}{f'=={version}' if version else ''}"


def build_bundle(output: Path, extras: list[str], repo: Path) -> None:
    metadata = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    unknown = sorted(set(extras) - set(project["optional-dependencies"]))
    if unknown:
        raise ValueError(f"unknown extras: {', '.join(unknown)}")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")

    wheels = output / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--wheel-dir",
            str(wheels),
            "--only-binary=:all:",
            _spec(".", extras),
        ],
        cwd=repo,
        check=True,
    )
    wheel_hashes = {path.name: _sha256(path) for path in sorted(wheels.glob("*.whl"))}
    if not any(name.startswith("hermes_agent-") for name in wheel_hashes):
        raise RuntimeError("Hermes wheel was not built")

    manifest = {
        "format": 1,
        "hermes_version": project["version"],
        "extras": extras,
        **_runtime(),
        "wheels": wheel_hashes,
    }
    shutil.copy2(Path(__file__), output / Path(__file__).name)
    (output / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Offline bundle created: {output}")


def _load_bundle(bundle: Path) -> dict[str, Any]:
    manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise ValueError("unsupported offline bundle format")
    expected = {key: manifest[key] for key in ("python", "platform", "machine")}
    actual = _runtime()
    if expected != actual:
        raise RuntimeError(
            f"bundle runtime mismatch: expected {expected}, got {actual}"
        )
    wheel_dir = bundle / "wheels"
    expected_wheels = set(manifest["wheels"])
    actual_wheels = {path.name for path in wheel_dir.glob("*.whl")}
    if expected_wheels != actual_wheels:
        raise RuntimeError("wheelhouse contents do not match the manifest")
    for name, expected_hash in manifest["wheels"].items():
        wheel = wheel_dir / name
        if not wheel.is_file() or _sha256(wheel) != expected_hash:
            raise RuntimeError(f"missing or corrupt wheel: {name}")
    return manifest


def install_bundle(bundle: Path, target: Path) -> None:
    manifest = _load_bundle(bundle)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"target virtual environment is not empty: {target}")
    venv.EnvBuilder(with_pip=True).create(target)
    python = target / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    package = _spec("hermes-agent", manifest["extras"], manifest["hermes_version"])
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(bundle / "wheels"),
            package,
        ],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-c",
            "from importlib.metadata import version; print(version('hermes-agent'))",
        ],
        check=True,
    )
    executable = target / (
        "Scripts/hermes.exe" if sys.platform == "win32" else "bin/hermes"
    )
    print(f"Hermes installed: {executable}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle", help="download wheels on an online machine")
    bundle.add_argument("output", type=Path)
    bundle.add_argument("--extras", default="all")
    install = commands.add_parser("install", help="install a prepared bundle offline")
    install.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    install.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "bundle":
        build_bundle(args.output.resolve(), _extras(args.extras), Path.cwd())
    else:
        install_bundle(args.bundle.resolve(), args.venv.expanduser().resolve())


if __name__ == "__main__":
    main()
