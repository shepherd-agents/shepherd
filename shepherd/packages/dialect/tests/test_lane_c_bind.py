"""Lane C, LC-1: named multi-binding acquisition (``ws.bind`` / ``ws[name]``) + bind-time disjoint validation.

The pure validation paths (reserved/duplicate name, outside-workspace, overlapping/nested roots) run
everywhere — they fail closed *before* any carrier work. The workspace-backed happy path (named handles
over a real selected binding) is macOS-gated (clonefile carrier), matching the v0.1.x support matrix.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from shepherd_dialect.workspace_control import ShepherdWorkspace
from shepherd_dialect.workspace_control.errors import WorkspaceControlError

if TYPE_CHECKING:
    from pathlib import Path

_macos = pytest.mark.skipif(sys.platform != "darwin", reason="clonefile carrier is macOS-only")


def _inert_workspace(workspace_path: Path | None) -> ShepherdWorkspace:
    """An ``ShepherdWorkspace`` with an inert ``mg`` — enough for the pre-carrier validation paths,
    which all fail closed before ``named_subroot_git_repo`` touches the substrate."""
    return ShepherdWorkspace(object(), workspace_path=workspace_path)


# --- bind-time validation (cross-platform; fails closed before any carrier work) -----------------


def test_reserved_and_empty_binding_names_fail_closed(tmp_path: Path) -> None:
    ws = _inert_workspace(tmp_path)
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="backend", name="workspace")  # reserved: the selected-binding name
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="backend", name="")


def test_duplicate_binding_name_fails_closed(tmp_path: Path) -> None:
    ws = _inert_workspace(tmp_path)
    ws._bound_roots["backend"] = os.path.realpath(str(tmp_path / "backend"))
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="other", name="backend")


def test_bind_root_outside_workspace_fails_closed(tmp_path: Path) -> None:
    ws = _inert_workspace(tmp_path)
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="/etc", name="etc")


def test_nested_and_identical_roots_fail_at_bind_time(tmp_path: Path) -> None:
    ws = _inert_workspace(tmp_path)
    ws._bound_roots["backend"] = os.path.realpath(str(tmp_path / "backend"))
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="backend/vendor", name="vendor")  # nested child = sub-root = Tier-3
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="backend", name="backend2")  # identical root


def test_relative_root_requires_workspace_path() -> None:
    ws = _inert_workspace(None)
    with pytest.raises(WorkspaceControlError):
        ws.bind(root="backend", name="backend")


def test_unknown_getitem_fails_closed(tmp_path: Path) -> None:
    ws = _inert_workspace(tmp_path)
    with pytest.raises(WorkspaceControlError):
        _ = ws["nope"]


# --- workspace-backed happy path (macOS: clonefile carrier) --------------------------------------


def _make_workspace(root: Path) -> ShepherdWorkspace:
    from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
    from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

    from shepherd_dialect.run_driver import ShepherdRunDriver
    from shepherd_dialect.workspace_control import (
        ShepherdRunLedgerDriver,
        ShepherdTaskArtifactDriver,
        ShepherdTaskLedgerDriver,
    )
    from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled

    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": "clonefile"})
    mg = VcsCore(
        str(root),
        substrates=[
            MarkerSubstrate(context),
            FilesystemSubstrate(context),
            TaskTraceSubstrateDriver(),
            ShepherdTaskLedgerDriver(),
            ShepherdTaskArtifactDriver(),
            ShepherdRunLedgerDriver(),
            ShepherdRunDriver(),
        ],
        store=store,
    )
    with _seal_and_select_enabled():
        mg.activate()
    return ShepherdWorkspace(mg, trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite", workspace_path=root)


def _seed_selected(ws: ShepherdWorkspace) -> None:
    from shepherd_dialect.workspace_control.feature_flags import _seal_and_select_enabled

    with _seal_and_select_enabled():
        ws.mg.exec("filesystem", "write", scope=ws.mg.ground, path="base.txt", content=b"base\n")


@_macos
def test_bind_two_disjoint_subroots_returns_named_handles(tmp_path: Path) -> None:
    from shepherd_runtime.nucleus import GitRepo

    ws = _make_workspace(tmp_path / "wc")
    try:
        _seed_selected(ws)
        (ws.workspace_path / "docs").mkdir()
        (ws.workspace_path / "backend").mkdir()

        docs = ws.bind(root="docs", name="docs")
        backend = ws.bind(root="backend", name="backend")

        assert isinstance(docs, GitRepo)
        assert isinstance(backend, GitRepo)
        # distinct binding names; full declared authority (per-parameter grants clamp at spawn)
        assert docs.binding == "docs"
        assert backend.binding == "backend"
        assert docs.authority == frozenset({"read", "write"})
        # whole-workspace custody ⇒ shared basis; the *name* + recorded root distinguish the view
        assert docs.basis == backend.basis
        # the workspace records name -> realpath(root) (the binding_roots map LC-2/LC-3 consume)
        assert ws._bound_roots["backend"] == os.path.realpath(str(ws.workspace_path / "backend"))
        # ws[name] round-trips to the exact handle
        assert ws["docs"] is docs
        assert ws["backend"] is backend
        # single-binding git_repo() is unchanged / additive
        assert ws.git_repo().binding == "workspace"
    finally:
        ws.close()


@_macos
def test_overlapping_bind_over_real_workspace_fails_at_bind_time(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path / "wc")
    try:
        _seed_selected(ws)
        (ws.workspace_path / "backend").mkdir()
        ws.bind(root="backend", name="backend")
        with pytest.raises(WorkspaceControlError):
            ws.bind(root="backend/vendor", name="vendor")
    finally:
        ws.close()
