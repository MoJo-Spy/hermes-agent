"""Behavioral regression coverage for the wheel/sdist distribution guard."""

import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_artifact(
    kind: str,
    tmp_path,
    *,
    nix_build: bool = False,
    offline_build: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real PEP 517 hook (build_sdist / build_wheel) as a subprocess.

    The wheel and sdist guards live in SEPARATE cmdclass entries in setup.py
    (the bdist_wheel one behind a try/except ImportError), so each hook needs
    its own regression coverage — a passing sdist test proves nothing about
    the wheel path.
    """
    env = os.environ.copy()
    # nix develop exports this too, so it must not grant permission to build
    # a distributable artifact.
    env["NIX_BUILD_TOP"] = "/build/devshell"
    if nix_build:
        env["HERMES_NIX_BUILD"] = "1"
    else:
        env.pop("HERMES_NIX_BUILD", None)
    if offline_build:
        env["HERMES_OFFLINE_BUILD"] = "1"
    else:
        env.pop("HERMES_OFFLINE_BUILD", None)
    # Redirect setuptools' scratch dirs (build/, *.egg-info) into tmp_path so
    # the allowed-marker build doesn't litter the real worktree.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    extra_cfg = tmp_path / "dist-extra.cfg"
    extra_cfg.write_text(
        f"[build]\nbuild_base = {scratch / 'build'}\n\n[egg_info]\negg_base = {scratch}\n",
        encoding="utf-8",
    )
    env["DIST_EXTRA_CONFIG"] = str(extra_cfg)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_{kind}; build_{kind}(r'{out}')".format(
                kind=kind, out=tmp_path
            ),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_artifact_build_rejects_nix_development_shell_environment(kind, tmp_path):
    result = _build_artifact(kind, tmp_path, nix_build=False)

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr


@pytest.mark.parametrize(
    ("kind", "artifact_glob"),
    [("sdist", "hermes_agent-*.tar.gz"), ("wheel", "hermes_agent-*.whl")],
)
def test_artifact_build_allows_explicit_nix_package_build_marker(kind, artifact_glob, tmp_path):
    result = _build_artifact(kind, tmp_path, nix_build=True)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob(artifact_glob))


def test_offline_marker_allows_only_the_self_contained_wheel(tmp_path):
    result = _build_artifact("wheel", tmp_path, offline_build=True)

    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("hermes_agent-*.whl"))
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())

    expected_locales = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "locales").glob("*.yaml")
    }
    expected_mcp_manifests = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "optional-mcps").glob("*/manifest.yaml")
    }
    assert (
        {
            "hermes_cli/tui_dist/entry.js",
            "gateway/assets/status_phrases.yaml",
            "plugins/platforms/telegram/plugin.yaml",
            "plugins/hermes-achievements/dashboard/manifest.json",
            "plugins/hermes-achievements/dashboard/dist/index.js",
        }
        | expected_locales
        | expected_mcp_manifests
    ) <= names


def test_offline_marker_does_not_allow_an_sdist(tmp_path):
    result = _build_artifact("sdist", tmp_path, offline_build=True)

    assert result.returncode != 0
    assert "Building wheels or sdists for hermes-agent is not supported" in result.stderr
