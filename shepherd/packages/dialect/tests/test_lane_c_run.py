"""Lane C, LC-2: ``workspace.run(bindings=...)`` public surface + the fail-closed handle guard.

These exercise run-target resolution — mutual exclusion (`repo` xor `bindings`) and the guard that a
`bindings` handle must be one produced by *this* workspace's `ws.bind`. All of this resolves before
any carrier work, so the suite runs cross-platform on an inert workspace. The LC-2 fence is gone
(LC-4 unfenced the path): a *validated* multi-binding run now proceeds to per-binding staging +
jail-enforced execution — that positive path is covered end-to-end by the deny-closed acceptance
gate (``test_lane_c_acceptance_gate.py``), which needs a real substrate + jail.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from shepherd_runtime.nucleus import GitRepo, GitRepoBasis

from shepherd_dialect.workspace_control import ShepherdWorkspace
from shepherd_dialect.workspace_control.errors import WorkspaceControlError

if TYPE_CHECKING:
    from pathlib import Path

_TASK = "spike.lanec.two_repo"


def _inert(ws_path: Path) -> ShepherdWorkspace:
    """An ``ShepherdWorkspace`` with an inert ``mg`` — enough for run-target resolution, which fails
    closed (or hands to LC-3) before ``_run_retained_workspace`` touches the substrate."""
    return ShepherdWorkspace(object(), workspace_path=ws_path)


def _fake_handle(name: str) -> GitRepo:
    basis = GitRepoBasis(world_oid="w", store_id="s", resource_id="r", head="h")
    return GitRepo(binding=name, basis=basis, authority=frozenset({"read", "write"}))


def _bind_fake(ws: ShepherdWorkspace, name: str, root: str) -> GitRepo:
    """Simulate a completed ``ws.bind`` without a carrier: record the root + a named handle."""
    handle = _fake_handle(name)
    ws._bound_roots[name] = os.path.realpath(root)
    ws._bound_handles[name] = handle
    return handle


def test_run_requires_exactly_one_of_repo_or_bindings(tmp_path: Path) -> None:
    ws = _inert(tmp_path)
    with pytest.raises(WorkspaceControlError, match="exactly one"):
        ws.run(_TASK)  # neither
    handle = _fake_handle("workspace")
    with pytest.raises(WorkspaceControlError, match="exactly one"):
        ws.run(_TASK, repo=handle, bindings={"backend": handle})  # both


def test_run_empty_bindings_fails_closed(tmp_path: Path) -> None:
    ws = _inert(tmp_path)
    with pytest.raises(WorkspaceControlError, match="non-empty"):
        ws.run(_TASK, bindings={})


def test_run_bindings_unbound_name_fails_closed(tmp_path: Path) -> None:
    ws = _inert(tmp_path)
    handle = _fake_handle("backend")
    with pytest.raises(WorkspaceControlError, match="not bound on this workspace"):
        ws.run(_TASK, bindings={"backend": handle})  # 'backend' was never ws.bind'd


def test_run_bindings_foreign_handle_fails_closed(tmp_path: Path) -> None:
    ws = _inert(tmp_path)
    _bind_fake(ws, "backend", str(tmp_path / "backend"))
    foreign = _fake_handle("workspace")  # e.g. a raw git_repo() handle, or one from another ws
    with pytest.raises(WorkspaceControlError, match="not produced by this workspace"):
        ws.run(_TASK, bindings={"backend": foreign})  # right key, wrong handle → never run unconfined


def test_run_valid_bindings_no_longer_hits_the_lc2_fence(tmp_path: Path) -> None:
    ws = _inert(tmp_path)
    backend = _bind_fake(ws, "backend", str(tmp_path / "backend"))
    docs = _bind_fake(ws, "docs", str(tmp_path / "docs"))
    # LC-4 unfenced the path: a validated multi-binding run proceeds past run-target resolution into
    # per-binding staging/execution (which the inert `mg` cannot serve, so it raises there — NOT the
    # removed "not wired yet (LC-3)" fence). The positive jailed path lives in the acceptance gate.
    with pytest.raises(Exception) as excinfo:
        ws.run(_TASK, bindings={"backend": backend, "docs": docs})
    assert "not wired yet" not in str(excinfo.value)
    assert "LC-3" not in str(excinfo.value)


def test_workspace_task_run_has_bindings_parity(tmp_path: Path) -> None:
    ws = _inert(tmp_path)
    _bind_fake(ws, "backend", str(tmp_path / "backend"))
    task = ws.tasks.task(_TASK)
    # WorkspaceTask.run mirrors ShepherdWorkspace.run: the same exactly-one mutual-exclusion rule,
    # enforced fail-closed before any substrate work.
    with pytest.raises(WorkspaceControlError, match="exactly one"):
        task.run()
