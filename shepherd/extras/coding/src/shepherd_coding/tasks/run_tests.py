"""RunTests — programmatic @task that runs pytest."""

from __future__ import annotations

import re
import shutil

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task

from shepherd_coding.findings import CodeFinding, Severity, Source
from shepherd_coding.models import ToolRunResult

from ._tool_runner import run_tool


@task
class RunTests(BaseModel):
    """Run pytest and parse failures into Issues.

    Programmatic task — no LLM needed.
    """

    workspace_path: Input(str) = Field(description="Project root")
    test_paths: Input(list[str]) = Field(default=[], description="Specific test paths")

    result: Output(ToolRunResult) = Field(default=None)

    def execute(self) -> None:
        pytest_path = shutil.which("pytest") or "pytest"
        cmd = [pytest_path, "--tb=line", "-q", "--no-header"]
        if self.test_paths:
            cmd.extend(self.test_paths)

        self.result = run_tool(
            binary="pytest",
            tool_name="pytest",
            cmd=cmd,
            cwd=self.workspace_path,
            timeout=300,
            parse_output=_parse_pytest_output,
        )


def _parse_pytest_output(output: str) -> list[CodeFinding]:
    findings: list[CodeFinding] = []
    pattern = re.compile(r"FAILED\s+(.+?)::(\S+)")
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            file_path, test_name = m.groups()
            findings.append(
                CodeFinding(
                    category="test_failure",
                    description=f"Test failed: {test_name}",
                    file_path=file_path,
                    severity=Severity.ERROR,
                    source=Source.PROGRAMMATIC,
                    evidence=line.strip(),
                )
            )
    return findings
