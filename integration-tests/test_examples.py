"""Smoke tests for checked-in examples."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples" / "workspace-handles"


_ENFORCEMENTS = {"jail", "advisory"}  # A6: whichever actually happened, never hard-coded


def test_best_of_n_example_runs_as_script() -> None:
    """The best-of-N example is executable, judges by changeset, and reports explicit settlement."""
    summary = _run_example(EXAMPLES / "best_of_n.py")

    assert summary["example"] == "workspace-handles.best_of_n"
    assert summary["settlements"] == {
        "discarded": "discarded",
        "released": "released",
        "selected": "selected",
    }
    assert summary["winner"]["state"] == "selected"
    assert "winner" in summary["winner"]["text"]
    # Judged by Changeset (the settlement-spelling review surface).
    assert summary["winner"]["changeset"]["changed_paths"] == ["candidate.txt"]
    assert {loser["state"] for loser in summary["losers"]} == {"released", "discarded"}
    # Placement honesty (A6): enforcement is recorded, not assumed — jail on a jail-capable host,
    # advisory in the dev column.
    assert set(summary["enforcement"]) <= _ENFORCEMENTS
    assert len(summary["enforcement"]) == 3
    assert {"world_oid", "store_id", "resource_id", "head"} <= set(summary["selected_basis"])


def test_apply_onto_moved_workspace_example_runs_as_script() -> None:
    """The apply example settles one candidate then applies a disjoint one onto the advanced parent."""
    summary = _run_example(EXAMPLES / "apply_onto_moved_workspace.py")

    assert summary["example"] == "workspace-handles.apply_onto_moved_workspace"
    # Disjoint candidates, reviewed by changeset.
    assert summary["reviewed"]["docs"]["changed_paths"] == ["docs.md"]
    assert summary["reviewed"]["code"]["changed_paths"] == ["code.py"]
    # select advances the parent; apply three-way-merges the disjoint delta onto it.
    assert summary["settlements"]["selected"] == "selected"
    assert summary["settlements"]["applied"] == "applied"
    assert summary["settlements"]["applied_is_three_way"] is True
    assert summary["settlements"]["applied_authority_outcome"] == "allowed"
    assert summary["selected"]["state"] == "selected"
    assert summary["applied"]["state"] == "applied"
    # The apply published a new world beyond the select.
    assert summary["basis_after_apply"]["world_oid"] != summary["basis_after_select"]["world_oid"]
    assert set(summary["enforcement"]) <= _ENFORCEMENTS


def test_retry_until_acceptable_example_runs_as_script() -> None:
    """The retry example is executable and reports explicit settlement."""
    summary = _run_example(EXAMPLES / "retry_until_acceptable.py")

    assert summary["example"] == "workspace-handles.retry_until_acceptable"
    assert summary["settlements"] == {
        "released": "released",
        "selected": "selected",
    }
    assert summary["rejected"]["state"] == "released"
    assert summary["rejected"]["text"] == "10:first:rejected\n"
    assert summary["accepted"]["state"] == "selected"
    assert summary["accepted"]["text"] == "90:second:accepted\n"
    assert {"world_oid", "store_id", "resource_id", "head"} <= set(summary["selected_basis"])


def _run_example(script: Path) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    value = json.loads(proc.stdout)
    assert isinstance(value, dict)
    return value
