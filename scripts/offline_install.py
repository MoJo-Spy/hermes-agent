#!/usr/bin/env python3
"""Build and install a platform-specific Hermes wheelhouse."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tomllib
import tempfile
import venv
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile


MANIFEST = "offline-manifest.json"
DESKTOP_FORMAT = 3
OFFLINE_MARKER = "offline-install.json"
EXE_MAGIC = b"HERMES_OFFLINE_1"
EXE_FOOTER = struct.Struct("<16sQ32s")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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
    with tempfile.TemporaryDirectory() as builder_temp:
        builder_venv = Path(builder_temp) / "wheel-builder"
        venv.EnvBuilder(with_pip=True).create(builder_venv)
        builder_python = builder_venv / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            [
                str(builder_python),
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


def _download(url: str, destination: Path, headers: dict[str, str] | None = None) -> None:
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=300) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _prepare_portable_node(target: Path, repo: Path) -> tuple[str, str]:
    index_url = "https://nodejs.org/dist/latest-v22.x/"
    with urlopen(index_url, timeout=60) as response:
        index = response.read().decode("utf-8", errors="replace")
    match = re.search(r"node-v(22\.\d+\.\d+)-win-x64\.zip", index)
    if not match:
        raise RuntimeError("could not resolve the current Node.js 22 Windows x64 ZIP")

    node_version = match.group(1)
    zip_name = match.group(0)
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        archive_path = temp_path / zip_name
        _download(f"{index_url}{zip_name}", archive_path)
        with urlopen(f"{index_url}SHASUMS256.txt", timeout=60) as response:
            sums = response.read().decode("utf-8")
        expected = next(
            (line.split()[0] for line in sums.splitlines() if line.endswith(f"  {zip_name}")),
            None,
        )
        if not expected or _sha256(archive_path) != expected:
            raise RuntimeError("Node.js ZIP failed its official SHA-256 check")
        extract = temp_path / "extract"
        with ZipFile(archive_path) as archive:
            archive.extractall(extract)
        source = extract / zip_name.removesuffix(".zip")
        if not (source / "node.exe").is_file():
            raise RuntimeError("Node.js ZIP did not contain node.exe")
        shutil.copytree(source, target)

    lock = json.loads((repo / "package-lock.json").read_text(encoding="utf-8"))
    agent_browser_version = lock["packages"]["node_modules/agent-browser"]["version"]
    subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            str(target / "npm.cmd"),
            "install",
            "--global",
            "--prefix",
            str(target),
            "--no-audit",
            "--no-fund",
            f"agent-browser@{agent_browser_version}",
        ],
        check=True,
    )
    return node_version, agent_browser_version


def _prepare_chromium(node: Path, target: Path) -> None:
    subprocess.run(
        ["cmd.exe", "/d", "/c", str(node / "agent-browser.cmd"), "install"],
        check=True,
    )
    browser_cache = Path.home() / ".agent-browser" / "browsers"
    candidates = sorted(browser_cache.glob("chrome-*"), reverse=True)
    source = next(
        (path for path in candidates if any(path.rglob("chrome.exe"))), None
    )
    if source is None:
        raise RuntimeError("agent-browser did not download Chrome for Testing")
    target.mkdir()
    shutil.copytree(
        source, target / source.name.replace("chrome-", "chromium-", 1)
    )


def _prepare_portable_git(target: Path) -> str:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hermes-offline-builder"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        "https://api.github.com/repos/git-for-windows/git/releases/latest",
        headers=headers,
    )
    with urlopen(request, timeout=60) as response:
        release = json.load(response)
    asset = next(
        (
            item
            for item in release["assets"]
            if re.fullmatch(r"PortableGit-.*-64-bit\.7z\.exe", item["name"])
        ),
        None,
    )
    if asset is None:
        raise RuntimeError("Git for Windows release has no PortableGit x64 asset")

    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / asset["name"]
        _download(asset["browser_download_url"], archive, headers)
        digest = asset.get("digest")
        if digest and digest.startswith("sha256:") and _sha256(archive) != digest.removeprefix("sha256:"):
            raise RuntimeError("PortableGit archive failed its GitHub SHA-256 check")
        subprocess.run([str(archive), "-y", f"-o{target}"], check=True)

    if not (target / "bin" / "bash.exe").is_file():
        raise RuntimeError("PortableGit extraction did not contain bin/bash.exe")
    return release["tag_name"]


def _prepare_desktop_builds(repo: Path) -> tuple[Path, Path]:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("Node.js and npm are required to build the desktop applications")
    lock_path = repo / "package-lock.json"
    lock_content = lock_path.read_bytes()
    try:
        subprocess.run(
            [npm, "install", "--no-audit", "--no-fund"], cwd=repo, check=True
        )
        subprocess.run(
            [npm, "run", "pack", "--workspace", "apps/desktop"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [
                npm,
                "run",
                "tauri:build",
                "--workspace",
                "apps/bootstrap-installer",
                "--",
                "--no-bundle",
            ],
            cwd=repo,
            check=True,
        )
    finally:
        lock_path.write_bytes(lock_content)

    desktop = repo / "apps" / "desktop" / "release" / "win-unpacked"
    bootstrap = (
        repo
        / "apps"
        / "bootstrap-installer"
        / "src-tauri"
        / "target"
        / "release"
        / "Hermes-Setup.exe"
    )
    if not (desktop / "Hermes.exe").is_file():
        raise RuntimeError("desktop build did not produce win-unpacked/Hermes.exe")
    if not bootstrap.is_file():
        raise RuntimeError("Tauri build did not produce Hermes-Setup.exe")
    return desktop, bootstrap


def _write_desktop_manifest(
    payload: Path,
    project: dict[str, Any],
    extras: list[str],
    repo: Path,
    node_version: str,
    agent_browser_version: str,
    git_version: str,
) -> dict[str, Any]:
    files = {
        relative: _sha256(path)
        for relative, path in sorted(_payload_files(payload).items())
    }
    files_json = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    manifest = {
        "format": DESKTOP_FORMAT,
        "kind": "windows-x64-desktop",
        "hermes_version": project["version"],
        "extras": extras,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8"
        ).strip(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "node": node_version,
        "agent_browser": agent_browser_version,
        "portable_git": git_version,
        "files": files,
        "payload_sha256": hashlib.sha256(files_json).hexdigest(),
    }
    (payload / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _write_payload_zip(payload: Path, archive_path: Path) -> None:
    with ZipFile(
        archive_path,
        "w",
        compression=ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for path in sorted(item for item in payload.rglob("*") if item.is_file()):
            archive.write(path, path.relative_to(payload).as_posix())


def _read_exe_footer(executable: Path) -> tuple[int, str]:
    with executable.open("rb") as file:
        file.seek(-EXE_FOOTER.size, 2)
        magic, length, digest = EXE_FOOTER.unpack(file.read(EXE_FOOTER.size))
    if magic != EXE_MAGIC:
        raise RuntimeError("offline EXE footer is missing")
    return length, digest.hex()


def _append_payload_exe(stub: Path, archive: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"output file already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = bytes.fromhex(_sha256(archive))
    with output.open("wb") as target, stub.open("rb") as source:
        shutil.copyfileobj(source, target)
        with archive.open("rb") as payload:
            shutil.copyfileobj(payload, target)
        target.write(EXE_FOOTER.pack(EXE_MAGIC, archive.stat().st_size, digest))
    length, written_hash = _read_exe_footer(output)
    if length != archive.stat().st_size or written_hash != digest.hex():
        raise RuntimeError("offline EXE footer verification failed")


def build_desktop_exe(output: Path, extras: list[str], repo: Path) -> None:
    machine = platform.machine().lower()
    python_platform = sysconfig.get_platform().lower()
    is_x64 = machine in ("amd64", "x86_64") or python_platform == "win-amd64"
    if sys.platform != "win32" or not is_x64:
        raise RuntimeError("the offline Desktop EXE can only be built on Windows x64")
    if sys.version_info[:2] != (3, 11) or sys.maxsize <= 2**32:
        raise RuntimeError("64-bit Python 3.11 is required to build the offline Desktop EXE")
    if output.suffix.lower() != ".exe":
        raise ValueError("offline Desktop output must use the .exe extension")

    metadata = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    unknown = sorted(set(extras) - set(project["optional-dependencies"]))
    if unknown:
        raise ValueError(f"unknown extras: {', '.join(unknown)}")

    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        payload = work / "payload"
        build_bundle(payload, extras, repo)
        _remove_runtime_path(payload / "runtime", payload)
        (payload / MANIFEST).unlink()
        shutil.copytree(repo / "skills", payload / "skills")

        desktop, bootstrap = _prepare_desktop_builds(repo)
        shutil.copytree(Path(sys.base_prefix), payload / "python")
        shutil.copytree(desktop, payload / "desktop")
        node_version, agent_browser_version = _prepare_portable_node(
            payload / "node", repo
        )
        _prepare_chromium(payload / "node", payload / "playwright")
        git_version = _prepare_portable_git(payload / "git")
        _write_desktop_manifest(
            payload,
            project,
            extras,
            repo,
            node_version,
            agent_browser_version,
            git_version,
        )
        _load_desktop_bundle(payload)

        archive = work / "payload.zip"
        _write_payload_zip(payload, archive)
        _append_payload_exe(bootstrap, archive, output)

    print(f"Offline Desktop installer created: {output}")


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


def _version(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise ValueError(f"invalid Hermes version: {value}")
    return tuple(map(int, match.groups()))


def _desktop_install_action(package: str, installed: str | None) -> str:
    package_version = _version(package)
    if installed is None:
        return "install"
    installed_version = _version(installed)
    if package_version < installed_version:
        raise RuntimeError(
            f"downgrade is not supported: installed {installed}, package {package}"
        )
    return "repair" if package_version == installed_version else "upgrade"


def _payload_files(bundle: Path) -> dict[str, Path]:
    return {
        path.relative_to(bundle).as_posix(): path
        for path in bundle.rglob("*")
        if path.is_file() and path.name != MANIFEST
    }


def _load_desktop_bundle(bundle: Path) -> dict[str, Any]:
    manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != DESKTOP_FORMAT:
        raise ValueError("unsupported offline desktop bundle format")
    if manifest.get("kind") != "windows-x64-desktop":
        raise ValueError("unsupported offline desktop bundle kind")
    _version(manifest["hermes_version"])

    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("offline desktop manifest has no files")
    for name in expected:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError(f"unsafe payload path: {name}")

    actual = _payload_files(bundle)
    if set(actual) != set(expected):
        raise RuntimeError("desktop payload contents do not match the manifest")
    for name, expected_hash in expected.items():
        if _sha256(actual[name]) != expected_hash:
            raise RuntimeError(f"missing or corrupt payload file: {name}")

    required = (
        "python/python.exe",
        "node/node.exe",
        "node/agent-browser.cmd",
        "git/bin/bash.exe",
        "git/cmd/git.exe",
        "desktop/Hermes.exe",
        "offline_install.py",
    )
    missing = [name for name in required if name not in actual]
    if missing or not any(name.startswith("wheels/") for name in actual):
        raise RuntimeError(f"desktop payload is incomplete: {', '.join(missing)}")
    if not any(name.startswith("playwright/chromium") for name in actual):
        raise RuntimeError("desktop payload is incomplete: Chromium is missing")
    if not any(name.startswith("skills/") and name.endswith("/SKILL.md") for name in actual):
        raise RuntimeError("desktop payload is incomplete: bundled skills are missing")
    return manifest


def _read_offline_marker(home: Path) -> dict[str, Any] | None:
    try:
        marker = json.loads((home / OFFLINE_MARKER).read_text(encoding="utf-8"))
        _version(marker["version"])
        return marker if marker.get("format") == 1 else None
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _remove_runtime_path(path: Path, home: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != home.resolve():
        raise RuntimeError(f"refusing to remove runtime path outside Hermes home: {path}")
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()


def _stop_hermes_processes(home: Path) -> None:
    if sys.platform != "win32":
        return
    script = r"""
$root = [IO.Path]::GetFullPath($env:HERMES_INSTALLER_ARGUMENT).TrimEnd('\') + '\'
$processes = Get-CimInstance Win32_Process -Filter "Name = 'Hermes.exe'" |
  Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) }
$processIds = @{}
foreach ($process in $processes) { $processIds[[int]$process.ProcessId] = $true }
$roots = $processes | Where-Object { -not $processIds.ContainsKey([int]$_.ParentProcessId) }
foreach ($process in $roots) {
  & taskkill.exe /PID $process.ProcessId /T /F | Out-Null
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 128) { exit $LASTEXITCODE }
}
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        env={**os.environ, "HERMES_INSTALLER_ARGUMENT": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 128):
        raise RuntimeError(
            "could not close Hermes Desktop; close all Hermes windows and retry: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def _create_shortcuts(target: Path) -> None:
    if sys.platform != "win32":
        return
    script = """
$shell = New-Object -ComObject WScript.Shell
$target = $env:HERMES_INSTALLER_ARGUMENT
$work = Split-Path -Parent $target
$paths = @(
  (Join-Path ([Environment]::GetFolderPath('Programs')) 'Hermes.lnk'),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hermes.lnk')
)
foreach ($path in $paths) {
  $parent = Split-Path -Parent $path
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $shortcut = $shell.CreateShortcut($path)
  $shortcut.TargetPath = $target
  $shortcut.WorkingDirectory = $work
  $shortcut.IconLocation = "$target,0"
  $shortcut.Save()
}
"""
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        env={**os.environ, "HERMES_INSTALLER_ARGUMENT": str(target)},
        check=True,
    )


def _validate_desktop_runtime(
    home: Path, root: Path, node: Path, playwright: Path, git: Path
) -> None:
    venv_root = root / "venv"
    python = venv_root / "Scripts" / "python.exe"
    desktop = root / "apps" / "desktop" / "release" / "win-unpacked"
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    }
    subprocess.run(
        [
            str(python),
            "-c",
            "from hermes_cli.main import _find_bundled_tui; assert _find_bundled_tui()",
        ],
        env=env,
        check=True,
    )
    subprocess.run(
        ["cmd.exe", "/d", "/c", str(node / "agent-browser.cmd"), "--version"],
        env=env,
        check=True,
    )
    subprocess.run([str(git / "bin" / "bash.exe"), "--version"], check=True)
    chrome = next(playwright.rglob("chrome.exe"), None)
    if chrome is None:
        raise RuntimeError("offline Chrome executable is missing after install")
    subprocess.run([str(chrome), "--version"], check=True)
    if not (desktop / "Hermes.exe").is_file():
        raise RuntimeError("offline Desktop executable is missing after install")


def _provision_desktop_runtime(
    bundle: Path,
    home: Path,
    manifest: dict[str, Any],
    paths: list[Path],
) -> None:
    root, node, playwright, git = paths
    python_root = root / "python"
    venv_root = root / "venv"
    desktop = root / "apps" / "desktop" / "release" / "win-unpacked"

    root.mkdir(parents=True)
    shutil.copytree(bundle / "python", python_root)
    shutil.copytree(bundle / "node", node)
    shutil.copytree(bundle / "playwright", playwright)
    shutil.copytree(bundle / "git", git)
    shutil.copytree(bundle / "desktop", desktop)

    base_python = python_root / "python.exe"
    subprocess.run([str(base_python), "-m", "venv", str(venv_root)], check=True)
    python = venv_root / "Scripts" / "python.exe"
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    }
    package = _spec(
        "hermes-agent", manifest.get("extras", ["all"]), manifest["hermes_version"]
    )
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
        env=env,
        check=True,
    )
    shutil.copytree(bundle / "skills", venv_root / "skills")
    shutil.copy2(node / "node.exe", venv_root / "Scripts" / "node.exe")
    license_path = node / "LICENSE"
    if license_path.is_file():
        shutil.copy2(license_path, venv_root / "NODE-LICENSE")

    bootstrap_marker = {
        "schemaVersion": 1,
        "pinnedCommit": manifest["source_commit"],
        "pinnedBranch": None,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "desktopVersion": manifest["hermes_version"],
    }
    (root / ".hermes-bootstrap-complete").write_text(
        json.dumps(bootstrap_marker, indent=2) + "\n", encoding="utf-8"
    )
    _validate_desktop_runtime(home, root, node, playwright, git)


def _relocate_staged_venv(staged_root: Path, final_root: Path) -> None:
    replacements = [
        (str(staged_root).encode(), str(final_root).encode()),
        (str(staged_root).replace("\\", "/").encode(), str(final_root).replace("\\", "/").encode()),
        (str(staged_root).encode("utf-16le"), str(final_root).encode("utf-16le")),
    ]
    if any(len(before) != len(after) for before, after in replacements):
        raise RuntimeError("staging and final runtime paths must have equal lengths")

    venv = final_root / "venv"
    candidates = [venv / "pyvenv.cfg"] + [
        path for path in (venv / "Scripts").rglob("*") if path.is_file()
    ]
    for path in candidates:
        content = path.read_bytes()
        updated = content
        for before, after in replacements:
            updated = updated.replace(before, after)
        if updated != content:
            path.write_bytes(updated)


def _finish_desktop_install(home: Path, root: Path) -> None:
    for name in (
        "cron",
        "sessions",
        "logs",
        "pairing",
        "hooks",
        "image_cache",
        "audio_cache",
        "memories",
        "skills",
    ):
        (home / name).mkdir(exist_ok=True)
    (home / ".env").touch(exist_ok=True)
    (root / "venv" / "Lib" / "site-packages" / ".install_method").write_text(
        "offline\n", encoding="utf-8"
    )
    desktop = root / "apps" / "desktop" / "release" / "win-unpacked"
    _create_shortcuts(desktop / "Hermes.exe")
    sync_profiles = (
        "from hermes_cli.profiles import list_profiles, seed_profile_skills; "
        "failed = [p.name for p in list_profiles() "
        "if seed_profile_skills(p.path, quiet=True) is None]; "
        "assert not failed, f'failed to sync bundled skills: {failed}'"
    )
    subprocess.run(
        [str(root / "venv" / "Scripts" / "python.exe"), "-c", sync_profiles],
        env={**os.environ, "HERMES_HOME": str(home)},
        check=True,
    )


def install_desktop_bundle(bundle: Path, home: Path) -> str:
    bundle = bundle.resolve()
    home = home.resolve()
    manifest = _load_desktop_bundle(bundle)
    installed_marker = _read_offline_marker(home)
    action = _desktop_install_action(
        manifest["hermes_version"],
        installed_marker["version"] if installed_marker else None,
    )
    home.mkdir(parents=True, exist_ok=True)

    targets = [
        home / "hermes-agent",
        home / "node",
        home / "playwright",
        home / "git",
    ]
    staging = [
        home / ".hermes-next",
        home / ".new",
        home / ".play-next",
        home / ".g2",
    ]
    if any(len(str(stage)) != len(str(target)) for stage, target in zip(staging, targets)):
        raise RuntimeError("invalid staging path layout")
    rollbacks = [home / f".{path.name}.rollback" for path in targets]
    previous = [home / f"{path.name}.previous" for path in targets]
    for target, rollback in zip(targets, rollbacks):
        if rollback.exists():
            if target.exists():
                _remove_runtime_path(target, home)
            rollback.replace(target)
    for stage in staging:
        if stage.exists():
            _remove_runtime_path(stage, home)

    try:
        _provision_desktop_runtime(bundle, home, manifest, staging)
    except Exception:
        for stage in staging:
            if stage.exists():
                _remove_runtime_path(stage, home)
        raise

    try:
        _stop_hermes_processes(home)
    except Exception:
        for stage in staging:
            if stage.exists():
                _remove_runtime_path(stage, home)
        raise

    moved: list[tuple[Path, Path]] = []
    try:
        for target, rollback in zip(targets, rollbacks):
            if target.exists():
                target.replace(rollback)
                moved.append((target, rollback))
    except OSError as exc:
        for target, rollback in reversed(moved):
            rollback.replace(target)
        for stage in staging:
            if stage.exists():
                _remove_runtime_path(stage, home)
        raise RuntimeError(
            "could not replace the Hermes runtime; close all Hermes processes and retry"
        ) from exc

    switched: list[Path] = []
    try:
        for stage, target in zip(staging, targets):
            stage.replace(target)
            switched.append(target)
        _relocate_staged_venv(staging[0], targets[0])
        _validate_desktop_runtime(home, *targets)
        _finish_desktop_install(home, targets[0])
        content_hash = manifest.get("payload_sha256") or hashlib.sha256(
            json.dumps(manifest["files"], sort_keys=True).encode()
        ).hexdigest()
        marker = {
            "format": 1,
            "version": manifest["hermes_version"],
            "installedAt": datetime.now(timezone.utc).isoformat(),
            "payloadSha256": content_hash,
        }
        for old in previous:
            if old.exists():
                _remove_runtime_path(old, home)
        for rollback, old in zip(rollbacks, previous):
            if rollback.exists():
                rollback.replace(old)
        marker_tmp = home / f".{OFFLINE_MARKER}.tmp"
        marker_tmp.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        marker_tmp.replace(home / OFFLINE_MARKER)
    except Exception:
        marker_tmp = home / f".{OFFLINE_MARKER}.tmp"
        if marker_tmp.exists():
            marker_tmp.unlink()
        for target in switched:
            if target.exists():
                _remove_runtime_path(target, home)
        for stage in staging:
            if stage.exists():
                _remove_runtime_path(stage, home)
        for target, rollback, old in zip(targets, rollbacks, previous):
            backup = rollback if rollback.exists() else old
            if backup.exists():
                backup.replace(target)
        raise

    print(f"Hermes Desktop {manifest['hermes_version']} {action} complete")
    return action


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
    desktop = commands.add_parser(
        "desktop-exe", help="build a Windows x64 all-in-one offline Desktop EXE"
    )
    desktop.add_argument("output", type=Path)
    desktop.add_argument("--extras", default="all,anthropic")
    install_desktop = commands.add_parser(
        "install-desktop", help="install an extracted offline Desktop payload"
    )
    install_desktop.add_argument(
        "--bundle", type=Path, default=Path(__file__).resolve().parent
    )
    install_desktop.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "bundle":
        build_bundle(args.output.resolve(), _extras(args.extras), Path.cwd())
    elif args.command == "install":
        install_bundle(args.bundle.resolve(), args.venv.expanduser().resolve())
    elif args.command == "desktop-exe":
        build_desktop_exe(
            args.output.resolve(), _extras(args.extras), Path.cwd()
        )
    else:
        install_desktop_bundle(
            args.bundle.resolve(), args.home.expanduser().resolve()
        )


if __name__ == "__main__":
    main()
