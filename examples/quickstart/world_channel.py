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

TASK_ID = "quickstart.write_note"
TASK_SOURCE = '''
import shepherd as sp

def write_note(repo: sp.May[sp.GitRepo, sp.ReadWrite], topic: str, output_path: str, output_text: str):
    """Write one quickstart note into a retained workspace output."""
    raise RuntimeError("provider-owned task bodies are prompts/contracts, not local Python")
'''


def main() -> None:
    """Run a deterministic retained-output workspace demo."""
    workspace = sp.open(".")
    try:
        workspace.tasks.register_source(
            task_id=TASK_ID,
            module="shepherd_quickstart_tasks",
            source_text=TASK_SOURCE,
            entrypoint="write_note",
            may_default="ReadWrite",
        )
        run = workspace.run(
            TASK_ID,
            repo=workspace.git_repo(),
            args={
                "topic": "quickstart",
                "output_path": "SHEPHERD_QUICKSTART.txt",
                "output_text": "Hello from a Shepherd retained output.\\n",
            },
            placement="advisory",
            runtime={"provider": "static"},
        )
        output = run.output()
        changeset = output.changeset().inspect()
        settlement = output.release()
        sys.stdout.write(
            json.dumps(
                {
                    "run_ref": run.run_ref,
                    "status": run.status,
                    "output_state": output.refresh().state,
                    "changed_paths": changeset["changed_paths"],
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
    finally:
        workspace.close()


if __name__ == "__main__":
    main()
