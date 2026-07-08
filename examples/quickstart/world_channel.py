"""Deterministic workspace-control quickstart.

Run from an initialized Shepherd workspace:
    sp init
    python world_channel.py
    sp run show --latest
    sp run trace --latest --events
"""

from __future__ import annotations

import json
import sys

import shepherd as sp


# A task is a signature + docstring: the contract a sandboxed agent fulfils. The
# grant on `repo` is the whole permission surface — nothing else authorizes the write.
@sp.task
def write_note(repo: sp.GitRepo, topic: str, output_path: str, output_text: str) -> None:
    """Write one quickstart note into a retained workspace output."""


def main() -> None:
    """Run a deterministic retained-output workspace demo."""
    with sp.open(".") as workspace:
        workspace.tasks.register(write_note)
        run = workspace.run(
            write_note,
            repo=workspace.git_repo(),
            topic="quickstart",
            output_path="SHEPHERD_QUICKSTART.txt",
            output_text="Hello from a Shepherd retained output.\n",
            placement="advisory",
            runtime={"provider": "static"},
        )
        output = run.output()
        changeset = output.changeset()
        # This smoke lane proves retained custody without advancing the selected workspace.
        settlement = output.release()
        sys.stdout.write(
            json.dumps(
                {
                    "run_ref": run.run_ref,
                    "status": run.status,
                    "output_state": output.refresh().state,
                    "changed_paths": list(changeset.changed_paths),
                    "preview": output.read_text("SHEPHERD_QUICKSTART.txt"),
                    "settlement": settlement.settlement.action,
                    "inspect": [
                        "sp run list",
                        "sp run show --latest",
                        "sp run trace --latest --events",
                        "sp run changeset --latest",
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
