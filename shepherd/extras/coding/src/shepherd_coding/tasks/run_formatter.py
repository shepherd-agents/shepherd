"""RunFormatter — programmatic @task that runs ruff format."""

from __future__ import annotations

import shutil

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task

from shepherd_coding.models import ToolRunResult

from ._tool_runner import run_tool


@task
class RunFormatter(BaseModel):
    """Run ruff format on specified files. Gracefully skips if ruff is missing."""

    workspace_path: Input(str) = Field(description="Project root")
    files: Input(list[str]) = Field(default=[], description="Files to format")
    check_only: Input(bool) = Field(default=False, description="Check mode only")

    result: Output(ToolRunResult) = Field(default=None)

    def execute(self) -> None:
        ruff_path = shutil.which("ruff") or "ruff"
        cmd = [ruff_path, "format"]
        if self.check_only:
            cmd.append("--check")
        py_files = [f for f in self.files if f.endswith(".py")] if self.files else []
        cmd.extend(py_files or ["."])

        self.result = run_tool(
            binary="ruff",
            tool_name="ruff-format",
            cmd=cmd,
            cwd=self.workspace_path,
            timeout=60,
        )
