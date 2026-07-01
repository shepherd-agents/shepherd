"""RunTypeChecker — programmatic @task that runs mypy."""

from __future__ import annotations

import re
import shutil

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task

from shepherd_coding.findings import CodeFinding, Severity, Source
from shepherd_coding.models import ToolRunResult

from ._tool_runner import run_tool


@task
class RunTypeChecker(BaseModel):
    """Run mypy on the project and parse type errors into Issues.

    Programmatic task — no LLM needed. Runs mypy in incremental mode
    on the full project (not scoped to changed files, per Spike 18).
    """

    workspace_path: Input(str) = Field(description="Project root")
    target: Input(str) = Field(default=".", description="mypy target")

    result: Output(ToolRunResult) = Field(default=None)

    def execute(self) -> None:
        mypy_path = shutil.which("mypy") or "mypy"
        cmd = [mypy_path, self.target, "--no-error-summary"]
        # Exclude directories with invalid Python package names (e.g. hyphens)
        cmd.extend(["--exclude", "integration-tests"])

        self.result = run_tool(
            binary="mypy",
            tool_name="mypy",
            cmd=cmd,
            cwd=self.workspace_path,
            timeout=120,
            parse_output=_parse_mypy_output,
        )


def _parse_mypy_output(output: str) -> list[CodeFinding]:
    findings: list[CodeFinding] = []
    pattern = re.compile(r"^(.+?):(\d+): (error|warning): (.+?)(?:\s+\[(.+)\])?$")
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            file_path, line_no, severity, message, _code = m.groups()
            ln = int(line_no)
            findings.append(
                CodeFinding(
                    category="type_error",
                    description=message.strip(),
                    file_path=file_path,
                    line_range=(ln, ln),
                    severity=Severity.ERROR if severity == "error" else Severity.WARNING,
                    source=Source.PROGRAMMATIC,
                    evidence=line.strip(),
                )
            )
    return findings
