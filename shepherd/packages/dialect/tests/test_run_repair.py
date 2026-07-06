"""Layer 3: the manual floor + honest error for an interrupted run's orphaned operations.

- `shepherd run repair` reclaims a dead prior run's orphaned operation refs (3a).
- the run path reclaims them automatically at run-start so "just run it again" works (3c).
- when the wedge does surface, the error names `shepherd run repair`, not the bare
  vcs-core `archive_orphaned_operations()` function (3b).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click
import pytest
from click.testing import CliRunner
from vcs_core import OrphanedOperationsError, Store
from vcs_core._lock import release_session_lock

from shepherd_dialect.cli import main
from shepherd_dialect.workspace_control import ShepherdWorkspace
from shepherd_dialect.workspace_control.workspace import (
    ORPHANED_OPERATIONS_REMEDY,
    reclaim_dead_orphaned_operations_before_run,
)

if TYPE_CHECKING:
    from pathlib import Path


def _init_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    Store(str(root / ".vcscore"))  # create the repo the discover() path activates
    return root


def _discover(root: Path) -> ShepherdWorkspace:
    return ShepherdWorkspace.discover(root, activate=True, backend="copy")


def _plant_dead_orphaned_operation(mg: object, *, handle_id: str) -> None:
    """Leave an orphaned ground operation ref, as a run killed mid-operation would.

    Mirrors vcs-core's `_abandon_session_with_open_ground_operation`: open an operation,
    then abandon the session (release the lock) without closing it.
    """
    with mg._lock:  # type: ignore[attr-defined]
        mg._pipeline.reset()  # type: ignore[attr-defined]
        mg._pipeline.begin_operation(handle_id=handle_id, kind="test.operation", scope=mg.ground)  # type: ignore[attr-defined]
    mg._pipeline.reset()  # type: ignore[attr-defined]
    mg._active_scopes.clear()  # type: ignore[attr-defined]
    mg._scope_parents.clear()  # type: ignore[attr-defined]
    mg._isolated_scopes.clear()  # type: ignore[attr-defined]
    mg._restored_scopes.clear()  # type: ignore[attr-defined]
    mg._patch_manager.uninstall_all()  # type: ignore[attr-defined]
    for substrate in reversed(mg.lifecycle_substrates):  # type: ignore[attr-defined]
        substrate.deactivate()
    release_session_lock(mg._repo_path, mg._session_id)  # type: ignore[attr-defined]


# --- 3a: `shepherd run repair` -------------------------------------------------------------


def test_run_repair_reclaims_orphaned_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _init_workspace(tmp_path)
    _plant_dead_orphaned_operation(_discover(root).mg, handle_id="op-repair-cli")

    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["run", "repair"])
    assert result.exit_code == 0, result.output
    assert "Reclaimed 1 interrupted run(s)" in result.output
    assert "op-repair-cli" in result.output

    # a second repair is a clean no-op — the wedge is gone
    again = CliRunner().invoke(main, ["run", "repair"])
    assert again.exit_code == 0, again.output
    assert "Nothing to repair" in again.output


def test_run_repair_nothing_to_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _init_workspace(tmp_path)
    _discover(root).close()  # initialise, no orphan

    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["run", "repair"])
    assert result.exit_code == 0, result.output
    assert "Nothing to repair" in result.output


# --- 3c: auto-reclaim at run-start -----------------------------------------------------------


def test_reclaim_before_run_clears_a_dead_orphan(tmp_path: Path) -> None:
    root = _init_workspace(tmp_path)
    _plant_dead_orphaned_operation(_discover(root).mg, handle_id="op-run-start")

    ws = _discover(root)
    try:
        assert ws.mg.list_orphaned_operations()  # detected at open — the wedge condition
        reclaim_dead_orphaned_operations_before_run(ws.mg)
        assert ws.mg.list_orphaned_operations() == ()  # ...reclaimed before the run proceeds
    finally:
        ws.close()


def test_reclaim_before_run_is_fail_soft() -> None:
    """A declined reclaim (e.g. entangled orphaned scope) must never block a run start."""

    class _Mg:
        def list_orphaned_operations(self) -> tuple[str, ...]:
            return ("op-blocked",)

        def archive_orphaned_operations(self) -> list[str]:
            raise RuntimeError("recovery blocked by an entangled orphaned scope")

    reclaim_dead_orphaned_operations_before_run(_Mg())  # does not raise


# --- 3b: the honest error names a real command ----------------------------------------------


def test_query_maps_orphaned_operations_error_to_repair_remedy() -> None:
    from shepherd_dialect.cli import _query

    def boom() -> None:
        raise OrphanedOperationsError(attempted="start run", operations=["interrupted-run"])

    with pytest.raises(click.ClickException) as excinfo:
        _query(boom)
    message = str(excinfo.value)
    assert "shepherd run repair" in message
    assert message == ORPHANED_OPERATIONS_REMEDY
    assert "archive_orphaned_operations" not in message  # the bare function name is gone
