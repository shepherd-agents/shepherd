"""Run N retained candidates and select the highest-scoring output."""

# ruff: noqa: INP001

from __future__ import annotations

import argparse
import json
from typing import Any

from _support import (
    CANDIDATE_TASK_ID,
    basis_summary,
    candidate_text,
    changeset_stat,
    copy_git_repo,
    demo_workspace,
    enforcement_of,
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

        # N retained candidates. placement="auto": the native syscall jail on a jail-capable
        # host, advisory in the in-process dev column — the recorded enforcement (below) says
        # which actually happened, never a hard-coded assumption.
        runs = [
            task.run(repo=copy_git_repo(repo), args={"label": label, "score": score}, placement="auto")
            for label, score in (("alpha", 10), ("winner", 99), ("omega", 20))
        ]
        outputs = [run.output() for run in runs]

        # Judge each candidate by its Changeset (the settlement-spelling review surface): every
        # candidate changed `candidate.txt`, and its recorded score is read through that changeset.
        review = {
            output.output_id: {"stat": changeset_stat(output), "score": _score(candidate_text(output))}
            for output in outputs
        }
        winner = max(outputs, key=lambda output: review[output.output_id]["score"])
        losers = [output for output in outputs if output.output_id != winner.output_id]

        # Settle in the settlement vocabulary: winner selected, losers explicitly released/discarded.
        selection = workspace.select(winner)
        released = workspace.release(losers[0].refresh())
        discarded = workspace.discard(losers[1].refresh())
        selected_repo = workspace.git_repo()

        summary: dict[str, Any] = {
            "example": "workspace-handles.best_of_n",
            "workspace": str(workspace.workspace_path),
            "enforcement": [enforcement_of(run) for run in runs],
            "winner": output_summary(winner),
            "losers": [output_summary(output) for output in losers],
            "settlements": {
                "selected": selection.settlement.action,
                "released": released.settlement.action,
                "discarded": discarded.settlement.action,
            },
            "selected_basis": basis_summary(selected_repo.basis),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))


def _score(text: str) -> int:
    return int(text.split(":", maxsplit=1)[0])


if __name__ == "__main__":
    main()
