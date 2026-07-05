"""D2 boundary tests for the post-hard-cut Shepherd/vcs-core dependency line.

The v1 hard-cut retires the legacy ``shepherd.vcscore`` spine and moves the live
vcs-core integration home to ``shepherd_dialect``. The boundary is therefore:

* ``shepherd/packages/meta/src/shepherd`` is the public facade and must not import
  ``vcs_core`` at all.
* ``shepherd/packages/dialect`` is the intentional integration home and may
  import public ``vcs_core`` surfaces.
* no active Shepherd production module may import private ``vcs_core._*``
  surfaces.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHEPHERD_PACKAGES = REPO_ROOT / "shepherd" / "packages"
SHEPHERD_META_SRC = SHEPHERD_PACKAGES / "meta" / "src" / "shepherd"
SHEPHERD_DIALECT_SRC = SHEPHERD_PACKAGES / "dialect" / "src" / "shepherd_dialect"


def _active_shepherd_python_files() -> list[Path]:
    return [
        path
        for path in sorted(SHEPHERD_PACKAGES.rglob("*.py"))
        if "/tests/" not in path.as_posix() and "/__pycache__/" not in path.as_posix()
    ]


def _import_targets(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            targets.add(node.module)
    return targets


def _is_vcs_core(target: str) -> bool:
    return target == "vcs_core" or target.startswith("vcs_core.")


def _is_private_vcs_core(target: str) -> bool:
    return _is_vcs_core(target) and any(part.startswith("_") for part in target.split(".")[1:])


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_shepherd_meta_facade_imports_no_vcs_core_surface() -> None:
    """The public ``shepherd`` package no longer owns a vcs-core spine."""
    actual = {
        _rel(path): sorted(target for target in _import_targets(path) if _is_vcs_core(target))
        for path in sorted(SHEPHERD_META_SRC.rglob("*.py"))
        if "/tests/" not in path.as_posix()
    }
    actual = {path: targets for path, targets in actual.items() if targets}

    assert actual == {}


def test_shepherd_dialect_is_the_vcs_core_integration_home() -> None:
    """Dialect is the only Shepherd package allowed to import public vcs-core."""
    importers = {
        _rel(path): sorted(target for target in _import_targets(path) if _is_vcs_core(target))
        for path in _active_shepherd_python_files()
    }
    importers = {path: targets for path, targets in importers.items() if targets}

    assert importers, "expected the dialect run path to import public vcs-core surfaces"
    unexpected = {
        path: targets
        for path, targets in importers.items()
        if not str(REPO_ROOT / path).startswith(str(SHEPHERD_DIALECT_SRC))
    }
    assert unexpected == {}


def test_private_vcs_core_imports_are_absent_from_shepherd_code() -> None:
    """No active Shepherd code may import private ``vcs_core._*`` modules."""
    actual = {
        _rel(path): sorted(target for target in _import_targets(path) if _is_private_vcs_core(target))
        for path in _active_shepherd_python_files()
    }
    actual = {path: targets for path, targets in actual.items() if targets}

    assert actual == {}


def test_import_shepherd_has_no_vcs_core_import_side_effect() -> None:
    """Importing the public facade does not install or import the retired spine."""
    before = {name for name in sys.modules if name == "vcs_core" or name.startswith("vcs_core.")}
    __import__("shepherd")
    after = {name for name in sys.modules if name == "vcs_core" or name.startswith("vcs_core.")}

    assert after == before


def test_meta_cli_mutates_no_ambient_process_env() -> None:
    """The CLI/templates must not mutate ambient process env (W1c).

    ``os.environ.setdefault``/``os.environ[...] = ...`` / ``os.environ.update``
    at the entrypoint leaks across in-process CliRunner invocations and poisons
    test order. Feature flags are scoped via ``scoped_seal_and_select()``
    (a restoring context manager) instead. This is an AST scan so it also
    catches the templates the quickstart copies verbatim.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(SHEPHERD_META_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits: list[str] = []
        for node in ast.walk(tree):
            # os.environ.setdefault(...) / os.environ.update(...)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"setdefault", "update", "pop"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
            ):
                hits.append(f"os.environ.{node.func.attr}@L{node.lineno}")
            # os.environ[...] = ... (Subscript assignment target)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Attribute)
                        and tgt.value.attr == "environ"
                    ):
                        hits.append(f"os.environ[...]=@L{node.lineno}")
        if hits:
            offenders[_rel(path)] = hits
    assert offenders == {}, f"ambient process-env mutation in the meta package: {offenders}"
