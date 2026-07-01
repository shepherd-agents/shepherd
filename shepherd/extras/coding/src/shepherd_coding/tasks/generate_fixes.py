"""GenerateFixes — batch LLM-powered @task for fix generation (Phase 4).

Receives a batch of confirmed issues and fixes them by directly editing
files in the workspace. The LLM has full write + bash access: it reads
code, writes fixes, and runs verification commands to self-check before
reporting results. The pipeline commits or rolls back based on the outcome.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts.workspace import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

_FIX_GUIDANCE = """\
You are a code repair agent with full access to the workspace. You will
receive a numbered list of confirmed issues. Fix them by directly editing
files using the Edit and Write tools.

Procedure:
1. Read the relevant source files to understand the code in context.
2. For each issue, determine the minimal change needed and apply it
   using Edit or Write. You may create new files (e.g., test files).
3. After applying all fixes, run the verification command (if provided)
   to check that your changes don't break anything.
4. If verification fails, diagnose which fix caused the failure, revert
   it, and remove its number from fixes_applied.
5. Report the final state: which issues you fixed and whether
   verification passed.

Rules:
- Fix ONLY the stated issues. Do not refactor, clean up, or improve
  surrounding code.
- All changes must be syntactically valid and preserve existing style.
- If you cannot confidently fix an issue, SKIP it — a wrong fix is
  worse than no fix.
- Do NOT use workarounds (e.g., # noqa, # type: ignore) to suppress
  issues — fix the underlying problem.
"""


@task(guidance=_FIX_GUIDANCE)
class GenerateFixes(BaseModel):
    """Fix a batch of confirmed issues by directly modifying workspace files.

    LLM-powered task with full write + bash access. Edits files in place,
    runs verification, and reports which issues were resolved.
    """

    issues_text: Input(str) = Field(
        description="Numbered list of confirmed issues to fix, e.g.:\n"
        "1. [correctness] description...\n"
        "2. [doc_gap] description...",
    )
    verify_command: Input(str) = Field(
        default="",
        description="Shell command to verify fixes (e.g., 'ruff check . && pytest tests/'). "
        "Run this after applying fixes to confirm nothing is broken.",
    )

    workspace: Context[WorkspaceRef]

    fixes_applied: Output(list[int]) = Field(
        default=[],
        description="1-based indices of issues successfully fixed",
    )
    summary: Output(str) = Field(
        default="",
        description="Human-readable summary of changes made",
    )
    tests_passed: Output(bool) = Field(
        default=False,
        description="Whether the verification command passed after all fixes",
    )
