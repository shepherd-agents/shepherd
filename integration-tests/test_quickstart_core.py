"""Core evidence for the v0.1.2 getting-started quickstart."""

from __future__ import annotations

import importlib.resources
import json
import subprocess
import sys
from pathlib import Path

import tomllib
from click.testing import CliRunner
from shepherd.cli import main as shepherd_cli

REPO = Path(__file__).resolve().parents[1]
QUICKSTART_EXAMPLES = REPO / "examples" / "quickstart"


def test_quickstart_templates_match_checked_in_examples() -> None:
    """The generator templates and checked-in examples must not drift."""
    templates = importlib.resources.files("shepherd.templates.quickstart")
    for filename in ("offline_task.py", "world_channel.py", "claude_readme.py", "agent_task.py"):
        assert templates.joinpath(filename).read_text(encoding="utf-8") == (
            QUICKSTART_EXAMPLES / filename
        ).read_text(encoding="utf-8")


def test_quickstart_generator_emits_compilable_demo() -> None:
    """`sp demo write quickstart` emits a compilable standalone script."""
    result = CliRunner().invoke(shepherd_cli, ["demo", "write", "quickstart"])

    assert result.exit_code == 0, result.output
    assert "import shepherd as sp" in result.output
    compile(result.output, "<sp demo write quickstart>", "exec")


def test_agent_task_generator_emits_compilable_demo() -> None:
    """`sp demo write agent-task` emits a compilable bodyless-task script."""
    result = CliRunner().invoke(shepherd_cli, ["demo", "write", "agent-task"])

    assert result.exit_code == 0, result.output
    assert "import shepherd as sp" in result.output
    assert "def write_program(" in result.output
    compile(result.output, "<sp demo write agent-task>", "exec")


def test_requirements_dev_pins_local_quickstart_closure() -> None:
    """The local pip install closure must stay explicit and editable."""
    expected = {
        "shepherd",
        "shepherd-core",
        "shepherd-dialect",
        "shepherd-export",
        "shepherd-kernel-v3-reference",
        "shepherd-providers",
        "shepherd-runtime",
        "shepherd2",
        "commons-vcs",
        "vcs-core",
    }
    assert _editable_project_names(REPO / "requirements-dev.txt") == expected


def test_offline_quickstart_example_runs() -> None:
    """The offline task quickstart runs without `.vcscore` or provider keys."""
    proc = subprocess.run(
        [sys.executable, str(QUICKSTART_EXAMPLES / "offline_task.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "retained outputs can be inspected" in proc.stdout


def test_world_channel_quickstart_runs_from_initialized_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The deterministic world-channel quickstart runs from `sp init`."""
    root = tmp_path / "workspace"
    root.mkdir()
    init_result = CliRunner().invoke(shepherd_cli, ["init", str(root)])
    assert init_result.exit_code == 0, init_result.output

    proc = subprocess.run(
        [sys.executable, str(QUICKSTART_EXAMPLES / "world_channel.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    summary = json.loads(proc.stdout)
    assert summary["status"] == "retained"
    assert summary["output_state"] == "released"
    assert summary["changed_paths"] == ["SHEPHERD_QUICKSTART.txt"]
    assert summary["settlement"] == "released"

    monkeypatch.chdir(root)
    show = CliRunner().invoke(shepherd_cli, ["run", "show", "--latest", "--json"])
    assert show.exit_code == 0, show.output
    assert json.loads(show.output)["run_ref"] == summary["run_ref"]


def _editable_project_names(requirements_path: Path) -> set[str]:
    names: set[str] = set()
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("-e "):
            raise AssertionError(f"requirements-dev.txt must use editable local paths only, got {line!r}")
        raw_path = line.removeprefix("-e ").strip()
        path_part = raw_path.split("[", 1)[0]
        pyproject = REPO.joinpath(path_part).joinpath("pyproject.toml")
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        names.add(data["project"]["name"])
    return names
