"""Guards on the contract between pyproject.toml and the code that ships.

The wheel published to Artifact Registry and the image published to the Docker
registry are only useful if the entry points they declare actually resolve.
These tests fail loudly when that drifts.
"""

from __future__ import annotations

import importlib
import pkgutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_declared_console_scripts_resolve() -> None:
    """Every ``[project.scripts]`` target must import and be callable."""
    scripts = load_pyproject()["project"].get("scripts", {})
    assert scripts, "expected at least one console script to be declared"

    for name, target in scripts.items():
        module_path, _, attribute = target.partition(":")
        module = importlib.import_module(module_path)
        assert hasattr(module, attribute), (
            f"console script {name!r} points at {target!r} but "
            f"{module_path!r} has no attribute {attribute!r}"
        )
        assert callable(getattr(module, attribute)), f"{target!r} is not callable"


def test_amf_core_modules_import() -> None:
    """Nothing under amf.core may fail at import time."""
    import amf.core

    modules = list(pkgutil.iter_modules(amf.core.__path__, "amf.core."))
    assert modules, "amf.core exposes no modules"
    for info in modules:
        importlib.import_module(info.name)


def test_entry_point_returns_zero_without_arguments() -> None:
    from amf.core.entry import main

    assert main([]) == 0


def test_packaged_paths_declared_in_pyproject_exist() -> None:
    """`packages.find.include` must not name packages that no longer exist."""
    config = load_pyproject()
    include = config["tool"]["setuptools"]["packages"]["find"]["include"]

    missing = [
        pattern
        for pattern in include
        if not (ROOT / pattern.rstrip("*").replace(".", "/")).exists()
    ]
    assert not missing, f"pyproject packages.find.include names missing packages: {missing}"
