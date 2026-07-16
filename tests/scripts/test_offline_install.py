import hashlib
import json

import pytest

from scripts import offline_install


def test_load_bundle_accepts_matching_runtime_and_rejects_corruption(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = wheels / "example.whl"
    wheel.write_bytes(b"wheel")
    manifest = {
        "format": 1,
        "hermes_version": "1.2.3",
        "extras": ["all"],
        **offline_install._runtime(),
        "wheels": {wheel.name: hashlib.sha256(b"wheel").hexdigest()},
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
        "format": 1,
        "hermes_version": "1.2.3",
        "extras": [],
        **offline_install._runtime(),
        "wheels": {},
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
        "format": 1,
        "hermes_version": "1.2.3",
        "extras": [],
        **offline_install._runtime(),
        "wheels": {},
    }
    (bundle / offline_install.MANIFEST).write_text(json.dumps(manifest))
    target = tmp_path / "target"
    target.mkdir()
    (target / "existing").touch()

    with pytest.raises(FileExistsError, match="not empty"):
        offline_install.install_bundle(bundle, target)
