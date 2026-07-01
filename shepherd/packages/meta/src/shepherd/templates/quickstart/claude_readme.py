"""Optional live Claude workspace-control quickstart.

Run from an initialized Shepherd workspace on a jail-capable host:
    sp doctor claude
    python claude_readme.py
"""

from __future__ import annotations

import json
import shutil
import sys

import shepherd as sp

TASK_ID = "quickstart.claude_readme"
TASK_SOURCE = '''
def update_readme(repo, goal: str, output_path: str = "README.md"):
    """Use Claude to make the README clearer for a first-time developer.

    Keep the edit small. Preserve existing factual claims. Write the proposed
    README to output_path inside the retained workspace output.
    """
    raise RuntimeError("Claude owns this task body at runtime")
'''


def _live_ready() -> tuple[bool, str]:
    from shepherd_dialect import claude_auth_mode

    if shutil.which("claude") is None:
        return False, "`claude` is not on PATH"
    if claude_auth_mode() is None:
        return False, "no ANTHROPIC_API_KEY and no signed-in `claude` CLI"
    try:
        from vcs_core.runtime_api import native_jail_available
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        return False, f"could not check native jail: {exc}"
    if not native_jail_available():
        return False, "native jail is unavailable"
    return True, "ready"


def main() -> None:
    """Run the optional live Claude retained-output demo."""
    ready, reason = _live_ready()
    if not ready:
        sys.stdout.write(json.dumps({"skipped": True, "reason": reason}, indent=2, sort_keys=True) + "\n")
        return

    workspace = sp.open(".")
    try:
        workspace.tasks.register_source(
            task_id=TASK_ID,
            module="shepherd_quickstart_claude_tasks",
            source_text=TASK_SOURCE,
            entrypoint="update_readme",
            may_default="ReadWrite",
        )
        run = workspace.run(
            TASK_ID,
            repo=workspace.git_repo(),
            args={"goal": "tighten the quickstart section"},
            may="ReadWrite",
            placement="jail",
            runtime={"provider": "claude"},
        )
        output = run.output()
        sys.stdout.write(
            json.dumps(
                {
                    "run_ref": run.run_ref,
                    "status": run.status,
                    "changed_paths": list(output.changed_paths),
                    "state": output.state,
                    "next": [
                        "sp run changeset --latest",
                        f"sp run select {run.run_ref}",
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
