"""Guard: seal→retain→select round-trips with VCS_CORE_SEAL_AND_SELECT explicitly absent (T1 W1.3).

The flag was retired (seal-and-select is unconditional). This guard runs the full round-trip in a
*subprocess* whose environment has the variable explicitly stripped, so a regression that
re-introduces env gating — at import time or in the settlement path — fails loudly here rather than
silently depending on an ambient variable no one sets anymore.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EXAMPLES = _REPO_ROOT / "examples" / "workspace-handles"

_ROUND_TRIP = textwrap.dedent(
    """
    import os, sys
    assert "VCS_CORE_SEAL_AND_SELECT" not in os.environ, "guard requires the flag absent"
    sys.path.insert(0, {examples!r})
    from _support import (
        CANDIDATE_TASK_ID,
        copy_git_repo,
        demo_workspace,
        register_candidate_task,
        seed_selected_workspace,
    )

    with demo_workspace(None, keep=False) as ws:
        register_candidate_task(ws)
        repo = seed_selected_workspace(ws)
        task = ws.tasks.task(CANDIDATE_TASK_ID)
        run = task.run(repo=copy_git_repo(repo), args={{"label": "only", "score": 1}}, placement="advisory")
        output = run.output()                      # seal + retain
        assert output.state == "unconsumed", output.state
        result = ws.select(output)                 # select
        assert result.settlement.action == "selected", result.settlement.action
        assert output.refresh().state == "selected"
        print("ROUND_TRIP_OK")
    """
).format(examples=str(_EXAMPLES))


def test_seal_retain_select_round_trips_with_flag_absent() -> None:
    env = {k: v for k, v in os.environ.items() if k != "VCS_CORE_SEAL_AND_SELECT"}
    proc = subprocess.run(
        [sys.executable, "-c", _ROUND_TRIP],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        check=False,
    )
    assert proc.returncode == 0, f"round-trip failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "ROUND_TRIP_OK" in proc.stdout, proc.stdout
