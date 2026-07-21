import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts import offline_install


def test_windows_powershell_paths_are_passed_through_environment(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(offline_install.sys, "platform", "win32")
    monkeypatch.setattr(offline_install.subprocess, "run", fake_run)
    home = Path(r"C:\Users\admin\AppData\Local\hermes path")
    desktop = home / "hermes-agent" / "apps" / "desktop" / "Hermes.exe"

    offline_install._stop_hermes_processes(home)
    offline_install._create_shortcuts(desktop)

    for (command, kwargs), expected in zip(calls, (home, desktop), strict=True):
        assert command[-2] == "-Command"
        assert str(expected) not in command
        assert "$env:HERMES_INSTALLER_ARGUMENT" in command[-1]
        assert kwargs["env"]["HERMES_INSTALLER_ARGUMENT"] == str(expected)
    stop_script = calls[0][0][-1]
    assert "ParentProcessId" in stop_script
    assert "foreach ($process in $roots)" in stop_script


def test_node_license_downloads_exact_version_when_not_installed(tmp_path, monkeypatch):
    requested = {}

    def fake_urlopen(url, timeout):
        requested.update(url=url, timeout=timeout)
        return io.BytesIO(b"node license")

    monkeypatch.setattr(offline_install, "urlopen", fake_urlopen, raising=False)

    assert (
        offline_install._node_license(tmp_path / "node.exe", "v24.18.0")
        == b"node license"
    )
    assert requested == {
        "url": "https://raw.githubusercontent.com/nodejs/node/v24.18.0/LICENSE",
        "timeout": 30,
    }


def test_portable_git_uses_github_token(tmp_path, monkeypatch):
    requested = {}
    release = {
        "tag_name": "v1",
        "assets": [
            {
                "name": "PortableGit-test-64-bit.7z.exe",
                "browser_download_url": "https://example.test/git.exe",
            }
        ],
    }

    def fake_urlopen(request, timeout):
        requested.update(headers=dict(request.header_items()), timeout=timeout)
        return io.BytesIO(json.dumps(release).encode())

    def fake_download(url, destination, headers):
        requested.update(download_url=url, download_headers=headers)
        destination.touch()

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "bash.exe").touch()
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(offline_install, "urlopen", fake_urlopen)
    monkeypatch.setattr(offline_install, "_download", fake_download)
    monkeypatch.setattr(offline_install.subprocess, "run", lambda *args, **kwargs: None)

    assert offline_install._prepare_portable_git(tmp_path) == "v1"
    assert requested["headers"]["Authorization"] == "Bearer test-token"
    assert requested["download_headers"]["Authorization"] == "Bearer test-token"


def _write_runtime(bundle):
    runtime = bundle / "runtime"
    runtime.mkdir()
    node = runtime / ("node.exe" if offline_install.sys.platform == "win32" else "node")
    license_file = runtime / "NODE-LICENSE"
    node.write_bytes(b"node")
    license_file.write_bytes(b"license")
    return {
        "version": "v20.0.0",
        "filename": node.name,
        "sha256": hashlib.sha256(b"node").hexdigest(),
        "license": license_file.name,
        "license_sha256": hashlib.sha256(b"license").hexdigest(),
    }


def test_load_bundle_accepts_matching_runtime_and_rejects_corruption(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "example.whl"
    wheel.write_bytes(b"wheel")
    manifest = {
        "format": 2,
        "hermes_version": "1.2.3",
        "extras": ["all"],
        **offline_install._runtime(),
        "wheels": {wheel.name: hashlib.sha256(b"wheel").hexdigest()},
        "node": _write_runtime(tmp_path),
    }
    (tmp_path / offline_install.MANIFEST).write_text(json.dumps(manifest))

    assert offline_install._load_bundle(tmp_path) == manifest

    (wheels / "unexpected.whl").write_bytes(b"wheel")
    with pytest.raises(RuntimeError, match="contents do not match"):
        offline_install._load_bundle(tmp_path)
    (wheels / "unexpected.whl").unlink()

    wheel.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="missing or corrupt wheel"):
        offline_install._load_bundle(tmp_path)


def test_load_bundle_rejects_incompatible_python(tmp_path):
    manifest = {
        "format": 2,
        "hermes_version": "1.2.3",
        "extras": [],
        **offline_install._runtime(),
        "wheels": {},
        "node": _write_runtime(tmp_path),
    }
    manifest["python"] = "0.0"
    (tmp_path / offline_install.MANIFEST).write_text(json.dumps(manifest))

    with pytest.raises(RuntimeError, match="bundle runtime mismatch"):
        offline_install._load_bundle(tmp_path)


def test_install_rejects_nonempty_target(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "wheels").mkdir()
    manifest = {
        "format": 2,
        "hermes_version": "1.2.3",
        "extras": [],
        **offline_install._runtime(),
        "wheels": {},
        "node": _write_runtime(bundle),
    }
    (bundle / offline_install.MANIFEST).write_text(json.dumps(manifest))
    target = tmp_path / "target"
    target.mkdir()
    (target / "existing").touch()

    with pytest.raises(FileExistsError, match="not empty"):
        offline_install.install_bundle(bundle, target)


@pytest.mark.parametrize(
    ("installed", "expected"),
    [(None, "install"), ("1.2.3", "repair"), ("1.2.2", "upgrade")],
)
def test_desktop_install_action(installed, expected):
    assert offline_install._desktop_install_action("1.2.3", installed) == expected


def test_desktop_install_action_rejects_downgrade():
    with pytest.raises(RuntimeError, match=r"downgrade.*1\.2\.3.*1\.2\.2"):
        offline_install._desktop_install_action("1.2.2", "1.2.3")


def _write_desktop_bundle(bundle: Path, version: str = "1.2.3") -> dict:
    files = {
        "python/python.exe": b"python",
        "wheels/hermes_agent-1.2.3-py3-none-any.whl": b"wheel",
        "node/node.exe": b"node",
        "node/agent-browser.cmd": b"agent-browser",
        "git/bin/bash.exe": b"bash",
        "git/cmd/git.exe": b"git",
        "playwright/chromium-1/chrome.exe": b"chromium",
        "desktop/Hermes.exe": b"desktop",
        "offline_install.py": b"installer",
        "skills/example/SKILL.md": b"---\nname: example\n---\n",
    }
    for relative, content in files.items():
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "format": offline_install.DESKTOP_FORMAT,
        "kind": "windows-x64-desktop",
        "hermes_version": version,
        "source_commit": "a" * 40,
        "files": {
            relative: hashlib.sha256(content).hexdigest()
            for relative, content in files.items()
        },
    }
    (bundle / offline_install.MANIFEST).write_text(json.dumps(manifest))
    return manifest


def test_load_desktop_bundle_rejects_extra_or_corrupt_payload(tmp_path):
    manifest = _write_desktop_bundle(tmp_path)
    assert offline_install._load_desktop_bundle(tmp_path) == manifest

    extra = tmp_path / "unexpected"
    extra.write_bytes(b"extra")
    with pytest.raises(RuntimeError, match="contents do not match"):
        offline_install._load_desktop_bundle(tmp_path)
    extra.unlink()

    (tmp_path / "desktop" / "Hermes.exe").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="missing or corrupt payload file"):
        offline_install._load_desktop_bundle(tmp_path)


def test_load_desktop_bundle_requires_bundled_skills(tmp_path):
    manifest = _write_desktop_bundle(tmp_path)
    skill = "skills/example/SKILL.md"
    (tmp_path / skill).unlink()
    manifest["files"].pop(skill)
    (tmp_path / offline_install.MANIFEST).write_text(json.dumps(manifest))

    with pytest.raises(RuntimeError, match="bundled skills are missing"):
        offline_install._load_desktop_bundle(tmp_path)


def test_append_payload_exe_writes_verifiable_footer(tmp_path):
    stub = tmp_path / "stub.exe"
    archive = tmp_path / "payload.zip"
    output = tmp_path / "offline.exe"
    stub.write_bytes(b"MZ-stub")
    archive.write_bytes(b"zip-payload")

    offline_install._append_payload_exe(stub, archive, output)

    length, digest = offline_install._read_exe_footer(output)
    assert length == archive.stat().st_size
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert output.read_bytes().startswith(stub.read_bytes() + archive.read_bytes())


def test_desktop_install_restores_partial_swap(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _write_desktop_bundle(bundle, version="2.0.0")
    home = tmp_path / "home"
    root = home / "hermes-agent"
    node = home / "node"
    root.mkdir(parents=True)
    node.mkdir()
    (root / "old-runtime").write_text("old")
    (node / "old-node").write_text("old")
    (home / offline_install.OFFLINE_MARKER).write_text(
        json.dumps({"format": 1, "version": "1.0.0"})
    )

    monkeypatch.setattr(offline_install, "_load_desktop_bundle", lambda _: manifest)
    monkeypatch.setattr(offline_install, "_stop_hermes_processes", lambda *_: None)

    def provision_staging(_bundle, _home, _manifest, paths):
        for path in paths:
            path.mkdir(parents=True)

    monkeypatch.setattr(
        offline_install, "_provision_desktop_runtime", provision_staging
    )
    original_replace = Path.replace

    def fail_node_swap(path, target):
        if path == node and Path(target).name == ".node.rollback":
            raise OSError("injected lock")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_node_swap)

    with pytest.raises(RuntimeError, match="close all Hermes processes"):
        offline_install.install_desktop_bundle(bundle, home)

    assert (root / "old-runtime").read_text() == "old"
    assert (node / "old-node").read_text() == "old"


def test_desktop_install_rolls_back_runtime_and_marker(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _write_desktop_bundle(bundle, version="2.0.0")
    home = tmp_path / "home"
    root = home / "hermes-agent"
    node = home / "node"
    playwright = home / "playwright"
    git = home / "git"
    root.mkdir(parents=True)
    node.mkdir()
    playwright.mkdir()
    git.mkdir()
    (root / "old-runtime").write_text("old")
    (node / "old-node").write_text("old")
    (playwright / "old-browser").write_text("old")
    (git / "old-git").write_text("old")
    old_marker = {"format": 1, "version": "1.0.0"}
    (home / offline_install.OFFLINE_MARKER).write_text(json.dumps(old_marker))

    monkeypatch.setattr(offline_install, "_load_desktop_bundle", lambda _: manifest)
    monkeypatch.setattr(offline_install, "_stop_hermes_processes", lambda *_: None)

    def provision_staging(_bundle, _home, _manifest, paths):
        for path in paths:
            path.mkdir(parents=True)
            (path / "new").write_text("new")

    monkeypatch.setattr(
        offline_install, "_provision_desktop_runtime", provision_staging
    )
    monkeypatch.setattr(offline_install, "_relocate_staged_venv", lambda *_: None)

    def fail_final_validation(*_args):
        raise RuntimeError("injected final validation failure")

    monkeypatch.setattr(
        offline_install, "_validate_desktop_runtime", fail_final_validation
    )

    with pytest.raises(RuntimeError, match="injected final validation failure"):
        offline_install.install_desktop_bundle(bundle, home)

    assert (root / "old-runtime").read_text() == "old"
    assert (node / "old-node").read_text() == "old"
    assert (playwright / "old-browser").read_text() == "old"
    assert (git / "old-git").read_text() == "old"
    assert json.loads((home / offline_install.OFFLINE_MARKER).read_text()) == old_marker
    assert not (home / "hermes-agent.previous").exists()


def test_relocate_staged_venv_rewrites_equal_length_paths(tmp_path):
    staged = tmp_path / ".hermes-next"
    final = tmp_path / "hermes-agent"
    scripts = final / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (final / "venv" / "pyvenv.cfg").write_text(f"home = {staged}\\python\n")
    launcher = scripts / "hermes.exe"
    launcher.write_bytes(b"prefix" + str(staged).encode() + b"suffix")

    offline_install._relocate_staged_venv(staged, final)

    assert str(final) in (final / "venv" / "pyvenv.cfg").read_text()
    assert str(staged).encode() not in launcher.read_bytes()
    assert str(final).encode() in launcher.read_bytes()


def test_finish_desktop_install_stamps_offline_method_and_syncs_skills(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    root = home / "hermes-agent"
    python = root / "venv" / "Scripts" / "python.exe"
    site_packages = root / "venv" / "Lib" / "site-packages"
    desktop = root / "apps" / "desktop" / "release" / "win-unpacked" / "Hermes.exe"
    python.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    desktop.parent.mkdir(parents=True)
    python.touch()
    desktop.touch()
    calls = []
    monkeypatch.setattr(offline_install, "_create_shortcuts", lambda *_: None)
    monkeypatch.setattr(
        offline_install.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    offline_install._finish_desktop_install(home, root)

    from hermes_cli.config import detect_install_method, is_unsupported_install_method

    method = detect_install_method(site_packages)
    assert method == "offline"
    assert not is_unsupported_install_method(method)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == [str(python), "-c"]
    assert "list_profiles" in command[2]
    assert "seed_profile_skills" in command[2]
    assert kwargs == {
        "env": {**offline_install.os.environ, "HERMES_HOME": str(home)},
        "check": True,
    }


def test_desktop_exe_includes_anthropic_by_default(tmp_path, monkeypatch):
    captured = {}
    output = tmp_path / "Hermes-Offline-Setup.exe"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        offline_install,
        "build_desktop_exe",
        lambda actual_output, extras, repo: captured.update(
            output=actual_output, extras=extras, repo=repo
        ),
    )
    monkeypatch.setattr(
        offline_install.sys,
        "argv",
        ["offline_install.py", "desktop-exe", str(output)],
    )

    offline_install.main()

    assert captured == {
        "output": output,
        "extras": ["all", "anthropic"],
        "repo": tmp_path,
    }
