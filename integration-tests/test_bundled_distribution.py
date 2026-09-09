"""Release metadata and resources must survive workspace consolidation."""

import importlib.util
from pathlib import Path

import tomllib


def builder():
    """Load the public distribution assembler without running a build."""
    path = Path(__file__).resolve().parents[1] / "packaging/shepherd-ai/build.py"
    spec = importlib.util.spec_from_file_location("bundle_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_preserves_checker_registration_resources_and_dependency_bounds(tmp_path, monkeypatch):
    """Keep registration, resources and tested bounds in the consolidated metadata."""
    module = builder()
    stage = tmp_path / "stage"
    monkeypatch.setattr(module, "STAGE", stage)
    module.stage(module.DEFAULT_VERSION)
    project = tomllib.loads((stage / "pyproject.toml").read_text())["project"]
    assert project["version"] == module.DEFAULT_VERSION
    checker = "shepherd_citation_checker"
    assert project["entry-points"]["shepherd.packages"]["citation_checker"] == checker + ".registration"
    assert project["scripts"]["shepherd-check-citations"] == checker + ".cli:main"
    assert (stage / "src" / checker / "reviewer.md").is_file()
    assert not (stage / "src" / checker / "evaluation").exists()
    workspace = tomllib.loads((module.REPO / "shepherd/packages/providers/pyproject.toml").read_text())
    assert project["optional-dependencies"]["claude"] == workspace["project"]["optional-dependencies"]["claude"]
    assert project["optional-dependencies"]["citation-checker"] == ["pdfplumber>=0.11,<0.12", "beautifulsoup4>=4.12,<5"]
    assert all(
        not requirement.startswith(("shepherd-", "vcs-core", "commons-vcs")) for requirement in project["dependencies"]
    )
    readme = (stage / "README.md").read_text()
    assert "](shepherd/extras/citation-checker/README.md)" not in readme
