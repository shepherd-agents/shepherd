"""P-030 Lane C LC-5 — the deny-closed acceptance gate (the flagship promotion gate).

These cross the PUBLIC facade only — ``ws.bind`` / ``ws.run(bindings=...)`` / ``run.output()`` /
``run.changeset(name=...)`` / ``ws.select`` — over the real ``vcs_core`` SPI, with no owner-path
reach-through. The flagship declares its permission surface in the task signature
(``docs: May[GitRepo, ReadOnly]``, ``backend: May[GitRepo, ReadWrite]``) and the assertions prove it
is enforced at the native syscall jail, not by convention (Lane C plan §5, A1-A7).

Jail-dependent assertions are gated on ``native_jail_available()``; on this macOS host the Seatbelt
jail runs them for real. A1-A3/A6/A7 are jailed; A4 (bind-time) and A5 (device honesty) run
unconditionally. (Numbering note: these A-numbers are the test-file gate assertions; the v0.2
surface spec §8 numbers its assertions differently — cite this file's names in release evidence.)

The jailed legs also carry ``workspace_native_jail`` so the native-jail lane
(``make test-dialect-workspace-native-jail``) collects this flagship gate exactly — never
green-because-unselected; A4/A5 stay in the scenarios lane, which excludes that marker.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from vcs_core import FilesystemSubstrate, MarkerSubstrate, Store, VcsCore, build_builtin_substrate_context
from vcs_core.runtime_api import native_jail_available
from vcs_core.runtime_substrate import TaskTraceSubstrateDriver

from shepherd_dialect.run_driver import ShepherdRunDriver
from shepherd_dialect.workspace_control import (
    RunStartError,
    ShepherdRunLedgerDriver,
    ShepherdTaskArtifactDriver,
    ShepherdTaskLedgerDriver,
    ShepherdWorkspace,
    WorkspaceControlError,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.workspace_scenario

_JAIL_ONLY = pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available on this host")
# Stacked with _JAIL_ONLY on every jailed leg: the marker routes collection (native-jail lane);
# the skipif keeps the leg honest on jail-less hosts. Two concerns, two marks.
_NATIVE_JAIL = pytest.mark.workspace_native_jail

# One module carrying every gate task. Each `May[GitRepo, ...]` parameter declares its per-binding
# grant in the signature; the handles are injected by parameter name at run time.
_TASK_SOURCE = """
from __future__ import annotations

import os
from pathlib import Path

from shepherd_dialect.workspace_control import May, ReadOnly, ReadWrite
from shepherd_runtime.nucleus import GitRepo


def apply_documented_fix(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadWrite], issue: str):
    backend.write("candidate.py", f"# fix for {issue}\\n".encode())
    return {"issue": issue, "docs_authority": docs.authority, "backend_binding": backend.binding}


def raw_docs_write(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadWrite]):
    # Raw filesystem I/O under the ReadOnly root — NOT the handle. The jail must refuse this.
    Path(docs.root, "leak.md").write_text("nope\\n", encoding="utf-8")
    return {"wrote": True}


def handle_docs_write(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadWrite]):
    # The in-body handle layer must refuse a write under a ReadOnly binding (the second layer).
    docs.write("leak.md", b"nope\\n")
    return {"wrote": True}


def unbound_write(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadWrite]):
    # A managed path inside NO bound root — must fail closed at the jail (deny-closed).
    Path(os.getcwd(), "unbound.txt").write_text("nope\\n", encoding="utf-8")
    return {"wrote": True}


def two_writable(docs: May[GitRepo, ReadWrite], backend: May[GitRepo, ReadWrite]):
    backend.write("candidate.py", b"# fix\\n")
    return {"ok": True}


def read_only_pair(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadOnly]):
    return {"docs": docs.authority, "backend": backend.authority}
"""


def _make_workspace(root: Path) -> ShepherdWorkspace:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(str(root / ".vcscore"))
    # Carrier follows the platform pairing (Seatbelt x clonefile on macOS,
    # Landlock x fuse-overlayfs on Linux — the same split as test_jailed_run.py
    # vs test_jailed_run_linux.py), so the gate's jailed legs execute on both
    # platforms instead of failing on the macOS-only clonefile backend.
    carrier_backend = "clonefile" if sys.platform == "darwin" else "fuse"
    context = build_builtin_substrate_context(store=store, workspace=root, config={"backend": carrier_backend})
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
    mg.activate()
    return ShepherdWorkspace(
        mg,
        trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite",
        workspace_path=root,
    )


def _register_gate_tasks(workspace: ShepherdWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = tmp_path / "lanec_gate_tasks.py"
    module_path.write_text(_TASK_SOURCE, encoding="utf-8")
    sys.modules.pop("lanec_gate_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    for qualname in (
        "apply_documented_fix",
        "raw_docs_write",
        "handle_docs_write",
        "unbound_write",
        "two_writable",
        "read_only_pair",
    ):
        workspace.tasks.register(f"lanec_gate_tasks:{qualname}", may_default="Permissive")


def _seed_bound_workspace(workspace: ShepherdWorkspace) -> None:
    """Seed `docs/` and `backend/` content into ground so the run clone mirrors them."""
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="docs/guide.md", content=b"guide\n")
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="backend/app.py", content=b"app\n")


def _bound_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShepherdWorkspace:
    workspace = _make_workspace(tmp_path / "ws")
    _register_gate_tasks(workspace, tmp_path, monkeypatch)
    _seed_bound_workspace(workspace)
    return workspace


# --- A1: positive — RW write lands under the jail --------------------------------------------


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a1_backend_write_lands_on_jailed_placement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        run = workspace.run(
            "lanec_gate_tasks.apply_documented_fix",
            args={"issue": "#142"},
            bindings={"docs": docs, "backend": backend},
        )
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.execution_evidence.enforcement_basis == "launch_confined_attempted"
        # The ReadWrite `backend/` write landed; the whole-delta output carries it.
        assert run.output().changed_paths == ("backend/candidate.py",)
        assert run.output().read_file("backend/candidate.py") == (b"# fix for #142\n", 0o100644)
    finally:
        workspace.close()


# --- A2: ReadOnly refusal at the syscall AND at the handle layer ------------------------------


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a2_readonly_root_raw_write_refused_at_the_syscall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        with pytest.raises(RunStartError, match=r"PermissionError|Operation not permitted|Read-only file system"):
            workspace.run("lanec_gate_tasks.raw_docs_write", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "jail"
        # The refused write is absent from managed state, and no output was published.
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/leak.md") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a2_readonly_handle_write_refused_at_the_handle_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        with pytest.raises(RunStartError, match="PermissionError"):
            workspace.run("lanec_gate_tasks.handle_docs_write", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.task_executions[0].error is not None
        assert record.task_executions[0].error["type"] == "PermissionError"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "docs/leak.md") is None
    finally:
        workspace.close()


# --- A3: deny-closed — a managed path inside no bound root fails closed -----------------------


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a3_unattributed_managed_write_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        with pytest.raises(RunStartError, match=r"PermissionError|Operation not permitted|Read-only file system"):
            workspace.run("lanec_gate_tasks.unbound_write", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "jail"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "unbound.txt") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


# --- A4: bind-time — overlapping / nested binds refused before any run -----------------------


def test_a4_overlapping_and_nested_binds_refused_at_bind_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        workspace.bind(root="backend/", name="backend")
        # A nested root is sub-root semantics (Tier-3) — refused fail-closed at bind time.
        with pytest.raises(WorkspaceControlError):
            workspace.bind(root="backend/service", name="nested")
        # The same root under a different name is also refused (equal roots overlap).
        with pytest.raises(WorkspaceControlError):
            workspace.bind(root="backend/", name="dup")
        # A reserved / duplicate name is refused too.
        with pytest.raises(WorkspaceControlError):
            workspace.bind(root="docs/", name="backend")
    finally:
        workspace.close()


# --- A5: device honesty — advisory labelling, RO+advisory refused ----------------------------


def test_a5_all_writable_advisory_run_records_advisory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        run = workspace.run(
            "lanec_gate_tasks.two_writable",
            bindings={"docs": docs, "backend": backend},
            placement="advisory",
        )
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.status == "retained"
        assert record.enforcement == "advisory"
        assert record.execution_evidence.resolved_placement == "advisory"
        assert record.task_executions[0].executor_kind == "in_process"
        assert run.output().changed_paths == ("backend/candidate.py",)
    finally:
        workspace.close()


def test_a5_readonly_binding_under_advisory_refused_at_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        # A ReadOnly grant cannot be honestly enforced on an in-process device — refuse fail-closed.
        with pytest.raises(RunStartError, match="advisory"):
            workspace.run(
                "lanec_gate_tasks.apply_documented_fix",
                args={"issue": "#1"},
                bindings={"docs": docs, "backend": backend},
                placement="advisory",
            )
    finally:
        workspace.close()


# --- A6: settlement — any-writable may select; consume-once; all-RO refuses -------------------


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a6_any_writable_selects_once_then_consume_once_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        run = workspace.run(
            "lanec_gate_tasks.apply_documented_fix",
            args={"issue": "#142"},
            bindings={"docs": docs, "backend": backend},
        )
        # any-writable: at least one binding (backend) is ReadWrite → select is allowed.
        selected = workspace.select(run.output())
        assert selected.settlement.action == "selected"
        # consume-once: the whole-delta output settles exactly once.
        with pytest.raises((WorkspaceControlError, RunStartError, ValueError)):
            workspace.select(run.output())
    finally:
        workspace.close()


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a6_all_readonly_run_select_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        run = workspace.run("lanec_gate_tasks.read_only_pair", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.status == "retained"
        assert run.output().changed_paths == ()
        # every binding was ReadOnly → the any-writable rule refuses selecting the whole delta.
        with pytest.raises(WorkspaceControlError, match="any-writable"):
            workspace.select(run.output())
    finally:
        workspace.close()


# --- A7: per-binding view — a free prefix-filter over the whole-delta changeset ---------------


@_NATIVE_JAIL
@_JAIL_ONLY
def test_a7_per_binding_changeset_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _bound_workspace(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs/", name="docs")
        backend = workspace.bind(root="backend/", name="backend")
        run = workspace.run(
            "lanec_gate_tasks.apply_documented_fix",
            args={"issue": "#142"},
            bindings={"docs": docs, "backend": backend},
        )
        assert run.changeset(name="backend").changed_paths == ("backend/candidate.py",)
        assert run.changeset(name="backend").binding == "backend"
        assert run.changeset(name="docs").changed_paths == ()
        # The whole-workspace view still shows the whole delta.
        assert run.changeset().changed_paths == ("backend/candidate.py",)
        # An unknown binding / output name fails closed.
        with pytest.raises(WorkspaceControlError):
            run.changeset(name="does-not-exist")
    finally:
        workspace.close()
