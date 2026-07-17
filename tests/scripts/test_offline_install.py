import hashlib
import io
import json

import pytest

from scripts import offline_install


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
