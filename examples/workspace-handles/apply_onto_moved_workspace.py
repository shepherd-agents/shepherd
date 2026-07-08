"""Apply a candidate onto a workspace that already moved on.

`select` is fast-forward-only: once you settle one candidate, the parent advances, and a second
candidate forked from the older basis can no longer be selected. `apply` lifts that — it
three-way-merges a candidate's delta onto the advanced parent when the two change sets are
path-disjoint (and fails closed on any overlap; no content synthesis at the boundary).

This is the settlement-spelling companion to best-of-N: two candidates that touched *different*
files, reviewed by their changesets, the first selected and the second **applied** onto the
now-advanced workspace.
"""

# ruff: noqa: INP001

from __future__ import annotations

import argparse
import json
from typing import Any

from _support import (
    basis_summary,
    changeset_stat,
    copy_git_repo,
    demo_workspace,
    enforcement_of,
    seed_selected_workspace,
)

# A path-parameterized task: candidates write DIFFERENT files, so their deltas are disjoint and
# the second can be applied onto the first without conflict.
PATH_TASK_ID = "examples.workspace_handles.write_path"
PATH_TASK_SOURCE = """
from shepherd_runtime.nucleus import GitRepo


def write_path(repo: GitRepo, path: str, text: str):
    repo.write(path, text.encode())
    return {"path": path}
"""


def main() -> None:
    """Run the example and print a JSON summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Workspace directory to use instead of a temporary workspace.")
    parser.add_argument("--keep", action="store_true", help="Keep a generated temporary workspace for inspection.")
    args = parser.parse_args()

    with demo_workspace(args.workspace, keep=args.keep) as workspace:
        workspace.tasks.register_source(
            task_id=PATH_TASK_ID,
            module="examples_workspace_handles_apply_tasks",
            source_text=PATH_TASK_SOURCE,
            entrypoint="write_path",
            may_default="ReadWrite",
        )
        repo = seed_selected_workspace(workspace)
        task = workspace.tasks.task(PATH_TASK_ID)

        # placement="auto": jailed on a jail-capable host, advisory in the dev column (see enforcement).
        docs_run = task.run(repo=copy_git_repo(repo), path="docs.md", text="docs edit", placement="auto")
        code_run = task.run(repo=copy_git_repo(repo), path="code.py", text="code edit", placement="auto")
        docs_out, code_out = docs_run.output(), code_run.output()

        # Review each candidate by its changeset — disjoint paths (docs.md vs code.py).
        docs_review = changeset_stat(docs_out)
        code_review = changeset_stat(code_out)

        # Select the docs candidate: the workspace advances past both candidates' fork basis.
        selection = workspace.select(docs_out)
        after_select_basis = workspace.git_repo().basis

        # Apply the code candidate onto the ADVANCED workspace. `select` would fail closed here
        # (drifted basis); `apply` three-way-merges the disjoint delta.
        application = workspace.apply(code_out.refresh())
        settled_repo = workspace.git_repo()

        summary: dict[str, Any] = {
            "example": "workspace-handles.apply_onto_moved_workspace",
            "workspace": str(workspace.workspace_path),
            "enforcement": [enforcement_of(docs_run), enforcement_of(code_run)],
            "reviewed": {"docs": docs_review, "code": code_review},
            "selected": {"output_id": docs_out.output_id, "state": docs_out.refresh().state},
            "applied": {"output_id": code_out.output_id, "state": code_out.refresh().state},
            "settlements": {
                "selected": selection.settlement.action,
                "applied": application.settlement.action,
                "applied_is_three_way": application.settlement.applied_head != application.settlement.candidate_head,
                "applied_authority_outcome": application.authority_outcome,
            },
            "basis_after_select": basis_summary(after_select_basis),
            "basis_after_apply": basis_summary(settled_repo.basis),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
