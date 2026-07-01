"""RunLinter / run_linter — programmatic task that runs ruff check."""

from __future__ import annotations

import shutil

from pydantic import BaseModel, Field
from shepherd_runtime.nucleus import task as nucleus_task
from shepherd_runtime.task.authoring import Input, Output, task

from shepherd_coding.models import ToolRunResult

from ._tool_runner import run_tool


def _run_linter(*, workspace_path: str, files: list[str] | None = None, fix: bool = False) -> ToolRunResult:
    ruff_path = shutil.which("ruff") or "ruff"
    cmd = [ruff_path, "check"]
    if fix:
        cmd.append("--fix")
    py_files = [f for f in files or [] if f.endswith(".py")]
    cmd.extend(py_files or ["."])

    return run_tool(
        binary="ruff",
        tool_name="ruff-check",
        cmd=cmd,
        cwd=workspace_path,
        timeout=60,
    )


@nucleus_task
def run_linter(workspace_path: str, files: list[str] | None = None, fix: bool = False) -> ToolRunResult:
    """Run ruff check on specified files. Gracefully skips if ruff is missing."""
    return _run_linter(workspace_path=workspace_path, files=files, fix=fix)


@task
class RunLinter(BaseModel):
    """Class-form compatibility wrapper for workflow-pipeline callers."""

    workspace_path: Input(str) = Field(description="Project root")
    files: Input(list[str]) = Field(default=[], description="Files to lint")
    fix: Input(bool) = Field(default=False, description="Auto-fix violations")

    result: Output(ToolRunResult) = Field(default=None)

    def execute(self) -> None:
        self.result = _run_linter(
            workspace_path=self.workspace_path,
            files=self.files,
            fix=self.fix,
        )


__all__ = ["RunLinter", "run_linter"]
