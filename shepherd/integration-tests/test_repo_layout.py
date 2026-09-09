from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib
from packaging.requirements import Requirement

from shepherd.cli import _workspace_layout as cli_layout

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _workspace_layout import (
    WorkspaceLayoutError,
    detect_layout,
    integration_tests_dir,
    package_dir,
    project_docs_dir,
    require_workspace_root,
    workspace_collection_targets,
)


def _write_workspace_pyproject(path: Path, members: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    members_block = ",\n".join(f'    "{member}"' for member in members)
    path.write_text(
        f'[project]\nname = "workspace"\nversion = "0.1.0"\n\n[tool.uv.workspace]\nmembers = [\n{members_block}\n]\n',
        encoding="utf-8",
    )


def _write_package_pyproject(package_dir: Path, name: str) -> None:
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )


def _project_metadata(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _minimum_python_version(requires_python: str) -> tuple[int, int]:
    match = re.search(r">=\s*(\d+)\.(\d+)", requires_python)
    assert match is not None, f"expected >=X.Y Python requirement, got {requires_python!r}"
    return int(match.group(1)), int(match.group(2))


def test_runtime_kernel_dependency_metadata_is_workspace_installable() -> None:
    """Runtime package metadata should resolve against the workspace kernel package."""
    repo_root = require_workspace_root(Path(__file__))
    runtime = _project_metadata(repo_root / "shepherd/packages/runtime/pyproject.toml")
    kernel = _project_metadata(repo_root / "shepherd/packages/kernel-v3-reference/pyproject.toml")

    runtime_dependencies = runtime["dependencies"]
    assert isinstance(runtime_dependencies, list)
    kernel_dependency = next(
        dependency
        for dependency in runtime_dependencies
        if isinstance(dependency, str) and dependency.startswith("shepherd-kernel-v3-reference")
    )

    kernel_version = kernel["version"]
    assert isinstance(kernel_version, str)
    # Workspace dependencies may intentionally have no version bound. Any
    # declared bounds must accept the actual workspace kernel version.
    assert kernel_version in Requirement(kernel_dependency).specifier
    assert _minimum_python_version(str(runtime["requires-python"])) >= _minimum_python_version(
        str(kernel["requires-python"])
    )


def test_detects_flat_layout_from_workspace_members_even_with_project_dirs_present(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(root / "pyproject.toml", ["packages/shepherd", "packages/vcs-core"])
    _write_package_pyproject(root / "packages" / "shepherd", "shepherd")
    _write_package_pyproject(root / "packages" / "vcs-core", "vcs-core")
    (root / "integration-tests").mkdir(parents=True)
    (root / "design" / "vcs-core").mkdir(parents=True)
    (root / "shepherd").mkdir()
    (root / "vcs-core").mkdir(exist_ok=True)

    assert detect_layout(root) == "flat"
    assert package_dir(root, "shepherd") == root / "packages" / "shepherd"
    assert integration_tests_dir(root) == root / "integration-tests"
    assert project_docs_dir(root, "shepherd", "design") is None
    assert project_docs_dir(root, "vcs-core", "design") == root / "design" / "vcs-core"
    assert workspace_collection_targets(root) == (root / "packages",)


def test_detects_nested_layout_from_workspace_members(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(
        root / "pyproject.toml",
        ["shepherd/packages/*", "shepherd/extras/*", "vcs-core/packages/*", "vcs-core/extras/*"],
    )
    _write_package_pyproject(root / "shepherd" / "packages" / "meta", "shepherd")
    _write_package_pyproject(root / "vcs-core" / "packages" / "core", "vcs-core")
    (root / "shepherd" / "integration-tests").mkdir(parents=True)
    (root / "vcs-core" / "design").mkdir(parents=True)

    assert detect_layout(root) == "nested"
    assert package_dir(root, "shepherd") == root / "shepherd" / "packages" / "meta"
    assert integration_tests_dir(root) == root / "shepherd" / "integration-tests"
    assert project_docs_dir(root, "vcs-core", "design") == root / "vcs-core" / "design"
    assert workspace_collection_targets(root) == (root / "shepherd" / "packages", root / "vcs-core" / "packages")


def test_mixed_workspace_members_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(root / "pyproject.toml", ["packages/shepherd", "shepherd/extras/*"])

    with pytest.raises(WorkspaceLayoutError, match="mix flat and nested"):
        detect_layout(root)


def test_umbrella_workspace_member_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(root / "pyproject.toml", ["packages"])

    with pytest.raises(WorkspaceLayoutError, match="Could not detect a supported repo layout"):
        detect_layout(root)


def test_repo_helper_import_does_not_import_shepherd_package() -> None:
    repo_root = require_workspace_root(Path(__file__))
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "import _workspace_layout\n"
        "print('shepherd' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_repo_shim_matches_canonical_helper_for_flat_layout(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(root / "pyproject.toml", ["packages/shepherd", "packages/vcs-core"])
    _write_package_pyproject(root / "packages" / "shepherd", "shepherd")
    _write_package_pyproject(root / "packages" / "vcs-core", "vcs-core")
    (root / "integration-tests").mkdir(parents=True)

    assert detect_layout(root) == cli_layout.detect_layout(root)
    assert package_dir(root, "shepherd") == cli_layout.package_dir(root, "shepherd")
    assert integration_tests_dir(root) == cli_layout.integration_tests_dir(root)
    assert workspace_collection_targets(root) == cli_layout.workspace_collection_targets(root)


def test_repo_shim_and_cli_adapter_share_exception_identity(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(root / "pyproject.toml", ["packages/shepherd", "shepherd/extras/*"])

    assert cli_layout.WorkspaceLayoutError is WorkspaceLayoutError
    with pytest.raises(WorkspaceLayoutError, match="mix flat and nested"):
        cli_layout.detect_layout(root)


def test_repo_shim_matches_canonical_helper_for_nested_layout(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_workspace_pyproject(
        root / "pyproject.toml",
        ["shepherd/packages/*", "shepherd/extras/*", "vcs-core/packages/*", "vcs-core/extras/*"],
    )
    _write_package_pyproject(root / "shepherd" / "packages" / "meta", "shepherd")
    _write_package_pyproject(root / "vcs-core" / "packages" / "core", "vcs-core")

    assert detect_layout(root) == cli_layout.detect_layout(root)
    assert package_dir(root, "vcs-core") == cli_layout.package_dir(root, "vcs-core")
    assert workspace_collection_targets(root) == cli_layout.workspace_collection_targets(root)


def test_markdown_link_script_accepts_shepherd_project_in_nested_layout() -> None:
    repo_root = require_workspace_root(Path(__file__))
    result = subprocess.run(
        [sys.executable, "scripts/check_markdown_links.py", "--project", "shepherd"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs/shepherd" in result.stdout
