"""AnalyzeCode — LLM-powered @task for code quality analysis.

Used for doc_gaps, correctness, consistency, and coverage_gap analysis.
The analyzer category and prompt are configured via inputs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts.workspace import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

from shepherd_coding.findings import CodeFinding  # noqa: TC001

_ANALYZER_GUIDANCE = """\
You are a code quality analyzer reviewing a pull request diff.

Your task is to identify specific, actionable issues in the changed code.
For each issue found, provide structured output with:
- category: the issue category you were asked to check
- description: a clear, specific description of the problem
- file_path: the exact file containing the issue
- line_range: approximate start and end line numbers
- severity: "error" for bugs/breakage, "warning" for style/improvement

IMPORTANT:
- Only flag issues you are confident about.
- Each finding must cite specific code that demonstrates the problem.
- Do NOT flag pre-existing issues — only issues in the changed code.
- Do NOT flag style preferences — only concrete problems.
"""


@task(guidance=_ANALYZER_GUIDANCE)
class AnalyzeCode(BaseModel):
    """Analyze changed code for quality issues in a specific category.

    LLM-powered task — the framework sends the inputs as a prompt and
    extracts structured outputs from the response.
    """

    diff_text: Input(str) = Field(description="Unified diff of the changes to analyze")
    category: Input(str) = Field(
        description="Issue category to check: doc_gap, correctness, consistency, or coverage_gap"
    )
    focus_prompt: Input(str) = Field(
        default="",
        description="Category-specific analysis instructions",
    )

    workspace: Context[WorkspaceRef]

    findings: Output(list[CodeFinding]) = Field(
        default=[],
        description="List of issues found in the changed code",
    )
