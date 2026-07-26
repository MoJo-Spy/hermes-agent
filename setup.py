"""
setup.py — wheel/sdist build guard.

pip/PyPI and Homebrew are no longer supported distribution methods for
Hermes Agent (see website/docs/getting-started/platform-support.md). The
wheel would ship without bundled assets (locales, skills, optional-mcps,
web_dist, tui_dist, plugin manifests) since those are resolved at runtime
via env-var overrides set by the nix wrapper or the source-checkout layout.

This file overrides the ``bdist_wheel`` and ``sdist`` setuptools commands
to raise an error when run outside a Nix build. The PEP 517
``build_wheel`` / ``build_sdist`` hooks in
``setuptools.build_meta`` call these commands internally, so the guard
fires for ``uv build``, ``pip wheel``, ``python -m build``, and direct
``setup.py`` invocations alike.

The normal legitimate consumer of ``build_wheel`` is uv2nix, which calls
``setuptools.build_meta.build_wheel`` (→ ``bdist_wheel``) inside a Nix
build sandbox. ``nix/python.nix`` sets ``HERMES_NIX_BUILD=1`` on the
Hermes package derivation.

The Windows offline Desktop builder is the one additional, private wheel
consumer. ``scripts/offline_install.py`` sets ``HERMES_OFFLINE_BUILD=1`` only
for its wheel subprocess. That marker permits a wheel (never an sdist) and
adds the runtime assets needed by the self-contained installer; it does not
re-enable a public pip/PyPI distribution path.

Editable installs (``uv sync``, ``pip install -e .``, ``nix develop``)
use ``build_editable``, which does NOT call ``bdist_wheel`` — it calls
``build_ext`` in editable mode. So the guard does not affect development.
"""

import os
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

_REPO_ROOT = Path(__file__).parent.resolve()
_IN_NIX_BUILD = os.environ.get("HERMES_NIX_BUILD") == "1"
_IN_OFFLINE_BUILD = os.environ.get("HERMES_OFFLINE_BUILD") == "1"

_BLOCK_MESSAGE = (
    "Building wheels or sdists for hermes-agent is not supported.\n"
    "Hermes is distributed via the shell installer, Docker image, or Nix.\n"
    "See: https://hermes-agent.nousresearch.com/docs/getting-started/installation\n"
    "\n"
    "If you are developing, use an editable install instead:\n"
    "  uv sync          # or: uv pip install -e .\n"
    "\n"
    "If you are building with Nix (uv2nix), this error should not fire —\n"
    "the Hermes Nix derivation sets HERMES_NIX_BUILD=1. If it does, file a bug."
)


class _GuardedSdist(sdist):
    def run(self, *args, **kwargs):
        if not _IN_NIX_BUILD:
            raise RuntimeError(_BLOCK_MESSAGE)
        return super().run(*args, **kwargs)


cmdclass = {"sdist": _GuardedSdist}


class _OfflineBuildPy(build_py):
    """Add bare runtime directories beside packages in the private wheel."""

    def run(self, *args, **kwargs):
        super().run(*args, **kwargs)
        output = Path(self.build_lib)
        for name in ("locales", "optional-mcps", "hermes_cli/tui_dist", "gateway/assets"):
            source = _REPO_ROOT / name
            if not source.is_dir():
                raise RuntimeError(f"offline wheel asset directory is missing: {source}")
            shutil.copytree(source, output / name, dirs_exist_ok=True)

        plugins = _REPO_ROOT / "plugins"
        for source in sorted(plugins.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(_REPO_ROOT)
            value = relative.as_posix()
            if (
                source.name in {"plugin.yaml", "plugin.yml"}
                or value.endswith("/dashboard/manifest.json")
                or "/dashboard/dist/" in value
            ):
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)


if _IN_OFFLINE_BUILD:
    cmdclass["build_py"] = _OfflineBuildPy

# bdist_wheel is only available when the `wheel` package is installed.
# setuptools.build_meta.build_wheel() calls it internally, so the guard
# fires for all PEP 517 wheel build paths. Define the subclass only when
# the import succeeds — otherwise a None base class raises TypeError at
# class-definition time, before the cmdclass guard can run.
try:
    from setuptools.command.bdist_wheel import bdist_wheel

    class _GuardedBdistWheel(bdist_wheel):
        def run(self, *args, **kwargs):
            if not (_IN_NIX_BUILD or _IN_OFFLINE_BUILD):
                raise RuntimeError(_BLOCK_MESSAGE)
            return super().run(*args, **kwargs)

    cmdclass["bdist_wheel"] = _GuardedBdistWheel
except ImportError:
    pass

setup(cmdclass=cmdclass)
