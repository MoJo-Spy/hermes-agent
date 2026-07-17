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
from urllib.request import urlopen
from zipfile import ZipFile


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


def _node_license(node_path: Path, node_version: str) -> bytes:
    for path in (node_path.parent / "LICENSE", node_path.parent / "LICENSE.txt"):
        if path.is_file():
            return path.read_bytes()

    url = f"https://raw.githubusercontent.com/nodejs/node/{node_version}/LICENSE"
    try:
        with urlopen(url, timeout=30) as response:
            return response.read()
    except OSError as exc:
        raise RuntimeError(
            f"Node.js LICENSE is missing beside {node_path} and could not be downloaded"
        ) from exc


def _prepare_tui(repo: Path) -> tuple[Path, bytes, str]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Node.js 20+ and npm are required to build the offline TUI")

    node_path = Path(node)
    node_version = subprocess.check_output(
        [node, "--version"], text=True, encoding="utf-8"
    ).strip()
    if int(node_version.removeprefix("v").split(".", 1)[0]) < 20:
        raise RuntimeError(f"Node.js 20+ is required, found {node_version}")
    license_content = _node_license(node_path, node_version)

    lock = repo / "package-lock.json"
    lock_content = lock.read_bytes()
    try:
        subprocess.run(
            [
                npm,
                "install",
                "--workspace",
                "ui-tui",
                "--include-workspace-root=false",
                "--no-audit",
                "--no-fund",
            ],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [npm, "run", "build", "--workspace", "ui-tui"], cwd=repo, check=True
        )
    finally:
        lock.write_bytes(lock_content)

    entry = repo / "ui-tui" / "dist" / "entry.js"
    if not entry.is_file():
        raise RuntimeError("TUI build did not produce ui-tui/dist/entry.js")
    bundled_entry = repo / "hermes_cli" / "tui_dist" / "entry.js"
    bundled_entry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(entry, bundled_entry)
    return node_path, license_content, node_version


def build_bundle(output: Path, extras: list[str], repo: Path) -> None:
    metadata = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    unknown = sorted(set(extras) - set(project["optional-dependencies"]))
    if unknown:
        raise ValueError(f"unknown extras: {', '.join(unknown)}")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")

    node, node_license, node_version = _prepare_tui(repo)
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
    wheel_paths = sorted(wheels.glob("*.whl"))
    wheel_hashes = {path.name: _sha256(path) for path in wheel_paths}
    hermes_wheel = next(
        (path for path in wheel_paths if path.name.startswith("hermes_agent-")), None
    )
    if hermes_wheel is None:
        raise RuntimeError("Hermes wheel was not built")
    with ZipFile(hermes_wheel) as archive:
        if "hermes_cli/tui_dist/entry.js" not in archive.namelist():
            raise RuntimeError("Hermes wheel does not contain the prebuilt TUI")

    runtime = output / "runtime"
    runtime.mkdir()
    node_name = "node.exe" if sys.platform == "win32" else "node"
    bundled_node = runtime / node_name
    bundled_license = runtime / "NODE-LICENSE"
    shutil.copy2(node, bundled_node)
    bundled_license.write_bytes(node_license)

    manifest = {
        "format": 2,
        "hermes_version": project["version"],
        "extras": extras,
        **_runtime(),
        "wheels": wheel_hashes,
        "node": {
            "version": node_version,
            "filename": node_name,
            "sha256": _sha256(bundled_node),
            "license": bundled_license.name,
            "license_sha256": _sha256(bundled_license),
        },
    }
    shutil.copy2(Path(__file__), output / Path(__file__).name)
    (output / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Offline bundle created: {output}")


def _load_bundle(bundle: Path) -> dict[str, Any]:
    manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != 2:
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
    node = manifest["node"]
    runtime = bundle / "runtime"
    expected_runtime = {node["filename"], node["license"]}
    actual_runtime = {path.name for path in runtime.iterdir() if path.is_file()}
    if expected_runtime != actual_runtime:
        raise RuntimeError("runtime contents do not match the manifest")
    for name, expected_hash in (
        (node["filename"], node["sha256"]),
        (node["license"], node["license_sha256"]),
    ):
        if _sha256(runtime / name) != expected_hash:
            raise RuntimeError(f"missing or corrupt runtime file: {name}")
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
    bin_dir = python.parent
    node = manifest["node"]
    installed_node = bin_dir / node["filename"]
    shutil.copy2(bundle / "runtime" / node["filename"], installed_node)
    shutil.copy2(bundle / "runtime" / node["license"], target / node["license"])
    installed_node.chmod(installed_node.stat().st_mode | 0o111)
    subprocess.run(
        [
            str(python),
            "-c",
            "from hermes_cli.main import _find_bundled_tui; assert _find_bundled_tui()",
        ],
        cwd=target,
        check=True,
    )
    subprocess.run([str(installed_node), "--version"], check=True)
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
