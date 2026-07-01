"""ValidateIssues — batch LLM-powered @task for concern validation (Phase 3).

Receives a batch of issues and validates them against the codebase in a
single LLM call. Returns a verdict per issue: confirmed, dropped, or
inconclusive.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts.workspace import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

from shepherd_coding.models import IssueVerdict  # noqa: TC001

_VALIDATION_GUIDANCE = """\
You are a senior code reviewer validating potential issues found by an automated analyzer.

You will receive a numbered list of issues. For EACH issue:

1. Read the cited code (if a file path is mentioned) and search for related code
   (callers, tests, type definitions) to understand context.
2. Assess whether the concern is real given the full evidence.
3. Return a verdict for each issue number.

Verdicts:
- "confirmed": The issue is real and should be fixed. Cite specific code as evidence.
- "dropped": False positive. Explain why (e.g., the code is correct, the pattern is intentional).
- "inconclusive": Cannot determine from static analysis alone.

You MUST return a verdict for every issue number in the batch.
You MUST provide evidence for each verdict — a verdict without evidence is not actionable.
"""


@task(guidance=_VALIDATION_GUIDANCE)
class ValidateIssues(BaseModel):
    """Validate a batch of issues against the codebase.

    LLM-powered task with full read-only workspace access. Processes
    multiple issues in a single call to reduce round-trip latency.
    """

    issues_text: Input(str) = Field(
        description="Numbered list of issues to validate, e.g.:\n"
        "1. [correctness] description...\n"
        "2. [doc_gap] description...",
    )

    workspace: Context[WorkspaceRef]

    verdicts: Output(list[IssueVerdict]) = Field(
        default=[],
        description="One verdict per issue number in the input batch",
    )
