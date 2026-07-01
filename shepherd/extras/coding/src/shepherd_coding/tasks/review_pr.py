"""ReviewPR task for AI-powered code review of pull requests."""

from typing import Literal

from pydantic import BaseModel, Field
from shepherd_contexts.workspace.ref import WorkspaceRef
from shepherd_runtime.task.authoring import Context, Input, Output, task

from ..contexts import GitHubContext
from ..models import PRDetails, ReviewFinding

_GUIDANCE = """\
You are an experienced software engineer reviewing a pull request.

PRIORITY HIERARCHY (review in this order):
1. Security vulnerabilities and data exposure
2. Correctness bugs — logic errors, null dereferences, race conditions
3. Performance regressions — O(n²) loops, unnecessary allocations, missing indexes
4. Maintainability — unclear intent, missing error handling, code duplication
5. Testing gaps — untested error paths, missing edge cases
6. Style — ONLY if not covered by an automated linter

NOISE CONTROL (critical):
- Produce at most 5 findings. Fewer is better. Zero is valid for clean PRs.
- Never duplicate what linters or formatters catch (formatting, import order, naming conventions).
- Every finding must be actionable — the author should know exactly what to change.
- If unsure about a finding, set confidence to "low". Low-confidence findings may be filtered.
- Do NOT comment on: correct code that could be written differently, subjective style preferences, \
changes you'd make but that aren't wrong.

READING THE DIFFS:
- The `diff_text` input contains the unified diffs for every changed file, formatted with clear \
file headers. This is your primary input — read it carefully.
- Each file section starts with "## File: <path>" followed by the unified diff.
- The `details` input contains structured PR metadata (title, author, labels, file list) but \
NOT the diff content. Use it for context, not for code review.
- Lines starting with + are additions. Lines starting with - are removals. Lines starting with \
@@ are hunk headers showing line numbers.

USING THE WORKSPACE:
- You have read access to the full repository via Read, Glob, and Grep tools.
- Use workspace tools when the diff alone is insufficient:
  * Check call sites when a function signature changes
  * Verify test coverage for new code paths
  * Understand patterns in surrounding code
- Do NOT crawl the entire repo. Read specific files when needed.
- Do NOT review unchanged files.

SEVERITY DEFINITIONS:
- blocker: Must fix before merge. Bugs, security holes, data loss risks.
- warning: Should fix. Performance issues, missing error handling, test gaps.
- suggestion: Consider fixing. Better approaches, minor improvements.
- nit: Trivial. Take it or leave it.

OUTPUT REQUIREMENTS:
- Each finding must have: severity, category, file_path, line_start, title, body.
- file_path must be relative to the repo root (matching paths in the diff headers).
- line_start must reference a line number from the diff hunk headers (the @@ lines).
- title: one clear sentence. body: explanation + what to change.
"""


@task(guidance=_GUIDANCE)
class ReviewPR(BaseModel):
    """Analyze a pull request and provide a comprehensive code review.

    This is an LLM-powered task. The model receives pre-formatted diffs
    and PR metadata, and has workspace access to read any file in the
    repository for additional context.
    """

    # Inputs
    details: Input(PRDetails) = Field(description="Structured PR metadata (title, author, labels, file list)")
    diff_text: Input(str) = Field(
        default="",
        description="Formatted unified diffs for all changed files, with file headers",
    )
    focus_areas: Input(list[str] | None) = Field(
        default=None,
        description="Specific areas to focus the review on (e.g., 'security', 'performance')",
    )
    guidelines: Input(str | None) = Field(
        default=None,
        description="Repo-specific review standards to follow",
    )
    verification_results: Input(str | None) = Field(
        default=None,
        description="Build/test results from VerifyPR. When present, cite specific failures.",
    )

    # Contexts
    github: Context[GitHubContext]
    workspace: Context[WorkspaceRef]

    # Outputs
    summary: Output(str) = Field(
        default="",
        description="High-level summary of the PR and its quality",
    )
    findings: Output(list[ReviewFinding]) = Field(
        default=[],
        description="Structured review findings with severity, category, file path, and line references",
    )
    approval: Output(Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]) = Field(
        default="COMMENT",
        description="Review decision (informational — humans make the final call)",
    )
    score: Output(float) = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Overall quality score from 0.0 (critical issues) to 1.0 (excellent)",
    )


# WorkspaceRef uses Path in its type hints. When @task creates the Taskified
# subclass, Path isn't in the generated class's namespace. Rebuild the model
# with Path available so Pydantic can resolve the forward reference.
from pathlib import Path as _Path

ReviewPR.model_rebuild(_types_namespace={"Path": _Path})

__all__ = ["ReviewPR"]
