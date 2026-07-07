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


# A bodyless task: the docstring is the contract Claude fulfils under the jail.
@sp.task
def update_readme(repo: sp.May[sp.GitRepo, sp.ReadWrite], goal: str, output_path: str = "README.md") -> None:
    """Use Claude to make the README clearer for a first-time developer.

    Keep the edit small. Preserve existing factual claims. Write the proposed
    README to output_path inside the retained workspace output.
    """


def _live_ready() -> tuple[bool, str]:
    from shepherd_dialect import claude_auth_status

    if shutil.which("claude") is None:
        return False, "`claude` is not on PATH"
    auth = claude_auth_status()
    if not auth.ok:
        # Honest about an expired/absent login instead of launching a doomed run.
        return False, auth.detail
    try:
        from shepherd_dialect import native_jail_available
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
        workspace.tasks.register(update_readme)
        run = workspace.run(
            update_readme,
            repo=workspace.git_repo(),
            args={"goal": "tighten the quickstart section"},
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
