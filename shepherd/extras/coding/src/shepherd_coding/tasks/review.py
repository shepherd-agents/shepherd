"""Review task — unified code review for PRs and branches.

Replaces both ``ReviewPR`` (PR pipeline) and ``ReviewBranch`` (branch
workflow) with a single task decoupled from GitHub-specific types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from shepherd_contexts.workspace.ref import WorkspaceRef
from shepherd_runtime.task.authoring import Context, Input, Output, task

from shepherd_coding.findings import CodeFinding  # noqa: TC001

_GUIDANCE = """\
You are an experienced software engineer reviewing code changes.

PRIORITY HIERARCHY (review in this order):
1. Security vulnerabilities and data exposure
2. Correctness bugs — logic errors, null dereferences, race conditions
3. Performance regressions — O(n^2) loops, unnecessary allocations, missing indexes
4. Maintainability — unclear intent, missing error handling, code duplication
5. Testing gaps — untested error paths, missing edge cases
6. Style — ONLY if not covered by an automated linter

NOISE CONTROL (critical):
- Produce at most 5 findings. Fewer is better. Zero is valid for clean changes.
- Never duplicate what linters or formatters catch.
- Every finding must be actionable.
- If unsure about a finding, set confidence to "low".
- Do NOT comment on correct code that could be written differently.

READING THE DIFFS:
- The `diff_text` input contains unified diffs for the changed files.
- Lines starting with + are additions, - are removals, @@ are hunk headers.

USING THE WORKSPACE:
- You have read access to the full repository via Read, Glob, and Grep tools.
- Use workspace tools when the diff alone is insufficient.
- Do NOT crawl the entire repo. Read specific files when needed.

SEVERITY DEFINITIONS:
- blocker: Must fix before merge. Bugs, security holes, data loss risks.
- error: Tool-confirmed problem (type error, test failure).
- warning: Should fix. Performance issues, missing error handling, test gaps.
- suggestion: Consider fixing. Better approaches, minor improvements.
- nit: Trivial. Take it or leave it.

OUTPUT REQUIREMENTS:
- Each finding must have: category, severity, file_path, line_range, title, description.
- file_path must be relative to the repo root.
"""


@task(guidance=_GUIDANCE)
class Review(BaseModel):
    """Analyze code changes and produce structured review findings.

    Works for both PR reviews (with pr_context) and branch reviews
    (without pr_context).
    """

    diff_text: Input(str) = Field(default="", description="Unified diffs for all changed files")
    file_change_summary: Input(str) = Field(default="", description="File list with change status and line counts")
    pr_context: Input(str | None) = Field(default=None, description="Formatted PR metadata. None for branch reviews.")
    focus_areas: Input(list[str] | None) = Field(default=None, description="Areas to focus on")
    guidelines: Input(str | None) = Field(default=None, description="Repo-specific review standards")
    verification_results: Input(str | None) = Field(default=None, description="Build/test results")

    workspace: Context(WorkspaceRef) = None  # type: ignore[assignment]

    summary: Output(str) = Field(default="", description="High-level summary")
    findings: Output(list[CodeFinding]) = Field(default=[], description="Structured review findings")
    approval: Output(Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]) = Field(default="COMMENT")
    score: Output(float) = Field(default=0.0, ge=0.0, le=1.0, description="Quality score 0.0-1.0")


from pathlib import Path as _Path

Review.model_rebuild(_types_namespace={"Path": _Path})

__all__ = ["Review"]
