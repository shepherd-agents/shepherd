"""Retry retained candidates until one inspects as acceptable."""

# ruff: noqa: INP001

from __future__ import annotations

import argparse
import json
from typing import Any

from _support import (
    CANDIDATE_TASK_ID,
    basis_summary,
    candidate_text,
    copy_git_repo,
    demo_workspace,
    output_summary,
    register_candidate_task,
    seed_selected_workspace,
)


def main() -> None:
    """Run the example and print a JSON summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Workspace directory to use instead of a temporary workspace.")
    parser.add_argument("--keep", action="store_true", help="Keep a generated temporary workspace for inspection.")
    args = parser.parse_args()

    with demo_workspace(args.workspace, keep=args.keep) as workspace:
        register_candidate_task(workspace)
        repo = seed_selected_workspace(workspace)
        task = workspace.tasks.task(CANDIDATE_TASK_ID)

        first = task.run(
            repo=copy_git_repo(repo),
            args={"label": "first", "score": 10, "accepted": False},
            placement="advisory",
        )
        first_output = first.output()
        if "accepted" in candidate_text(first_output):
            raise RuntimeError("first candidate was expected to be rejected")
        release = workspace.release(first_output)

        second = task.run(
            repo=copy_git_repo(repo),
            args={"label": "second", "score": 90, "accepted": True},
            placement="advisory",
        )
        second_output = second.output()
        if "accepted" not in candidate_text(second_output):
            raise RuntimeError("second candidate was expected to be accepted")
        selection = workspace.select(second_output)
        selected_repo = workspace.git_repo()

        summary: dict[str, Any] = {
            "example": "workspace-handles.retry_until_acceptable",
            "workspace": str(workspace.workspace_path),
            "rejected": output_summary(first_output),
            "accepted": output_summary(second_output),
            "settlements": {
                "released": release.settlement.action,
                "selected": selection.settlement.action,
            },
            "selected_basis": basis_summary(selected_repo.basis),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
