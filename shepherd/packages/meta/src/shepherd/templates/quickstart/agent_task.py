"""Your first agent task: a function with no body, implemented by a Claude agent.

Run from an initialized workspace (`shepherd init`; `shepherd doctor claude`
checks readiness). The agent's work is kept as a retained output for you to
review — nothing touches your files unless you `shepherd run select` it.
"""

import shutil
import sys

from shepherd_dialect import claude_auth_mode

import shepherd as sp

# The ask. Change it and re-run — the contract below stays the same.
PROMPT = "a mesmerizing spinning 3D ASCII donut animation in the terminal"


# The signature is the permission surface: the grant on `repo` is what lets the
# agent write the bound repository (see "Permissions" in the README).
def write_program(repo: sp.May[sp.GitRepo, sp.ReadWrite], prompt: str, output_path: str = "program.py") -> None:
    """Write a small, self-contained Python program that does what `prompt` asks.

    Save it to output_path. It must run with plain `python3`, read no input,
    and finish on its own within about ten seconds.
    """


if shutil.which("claude") is None or claude_auth_mode() is None:
    sys.exit("not ready — run `shepherd doctor claude` to see what's missing")

workspace = sp.open(".")
try:
    workspace.tasks.register(write_program, task_id="quickstart.write_program", may_default="ReadWrite")
    run = workspace.run(
        "quickstart.write_program",
        repo=workspace.git_repo(),
        args={"prompt": PROMPT, "output_path": "donut.py"},
        placement="jail",
        runtime={"provider": "claude"},
    )
    changed = ", ".join(run.output().changed_paths)
    print(f"retained: {run.run_ref} wrote {changed} (nothing applied to your files)")
    print()
    print("run the agent's program straight from the retained output:")
    print("  shepherd run changeset --latest --read donut.py | python3 -")
    print()
    print("keep it, or not:")
    print(f"  shepherd run select {run.run_ref}")
    print(f"  shepherd run discard {run.run_ref}")
finally:
    workspace.close()
