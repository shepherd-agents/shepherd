"""ADVERSARIAL refutation suite for P-030 Lane C per-binding grant enforcement.

Independent reviewer. Job: REFUTE the soundness claim. Each test states an attack and
asserts the OBSERVED behaviour (not the desired one). A test named ``..._REFUTED`` documents
a hole (its body asserts the hole is real); a test named ``..._survived`` documents that the
attack failed and the invariant held. No source or existing test is modified.

Public facade: ws.bind / ws.run(bindings=...) / run.output() / ws.select. Internal seams are
reached only where a public attack is not expressible (and are labelled as such).
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

_JAIL_ONLY = pytest.mark.skipif(not native_jail_available(), reason="native jail backend is not available")


_TASK_SOURCE = """
from __future__ import annotations

import os
from pathlib import Path

from shepherd_dialect.workspace_control import May, ReadOnly, ReadWrite
from shepherd_runtime.nucleus import GitRepo


def alias_raw_write(backend: May[GitRepo, ReadWrite], BACKEND: May[GitRepo, ReadOnly]):
    # T4/T2: two bindings differing only by CASE point at the same physical dir on
    # case-insensitive APFS. `BACKEND` is declared ReadOnly. Raw-FS write to its root
    # bypasses the (advisory) handle; the syscall jail is the real gate.
    Path(BACKEND.root, "pwned.txt").write_text("unauthorized\\n", encoding="utf-8")
    return {"ro_root": str(BACKEND.root), "rw_root": str(backend.root)}


def alias_handle_write(backend: May[GitRepo, ReadWrite], BACKEND: May[GitRepo, ReadOnly]):
    # Same alias, but via the in-body handle for the ReadWrite name — writes land in the
    # shared physical dir which the ReadOnly name also views.
    backend.write("pwned_via_rw.txt", b"landed\\n")
    return {}


def ro_write_when_sibling_rw(docs: May[GitRepo, ReadWrite], backend: May[GitRepo, ReadOnly]):
    # T5/T2: a HETEROGENEOUS run (docs RW, backend RO). Raw-FS write under the RO `backend`
    # root. The any-writable settlement collapse must NOT leak to the jail: the RO root's
    # write must still be refused at the syscall even though a sibling binding is RW.
    Path(backend.root, "leak.py").write_text("nope\\n", encoding="utf-8")
    return {}


def two_rw_unbound_write(docs: May[GitRepo, ReadWrite], backend: May[GitRepo, ReadWrite]):
    # T3: both bindings RW, but write to a managed path under NEITHER root. Deny-closed:
    # the jail's writable_roots is the union of the two sub-roots, not the whole clone.
    Path(os.getcwd(), "unbound.txt").write_text("nope\\n", encoding="utf-8")
    return {}


def escape_via_dotdot(docs: May[GitRepo, ReadWrite], backend: May[GitRepo, ReadWrite]):
    # T3: try to climb out of the RW sub-root with `..` and write a sibling managed path.
    Path(backend.root, "..", "escaped.txt").write_text("nope\\n", encoding="utf-8")
    return {}


def backend_landing(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadWrite]):
    backend.write("candidate.py", b"# fix\\n")
    return {}


def all_ro(docs: May[GitRepo, ReadOnly], backend: May[GitRepo, ReadOnly]):
    return {"docs": docs.authority, "backend": backend.authority}
"""

_QUALNAMES = (
    "alias_raw_write",
    "alias_handle_write",
    "ro_write_when_sibling_rw",
    "two_rw_unbound_write",
    "escape_via_dotdot",
    "backend_landing",
    "all_ro",
)


def _make_workspace(root: Path) -> ShepherdWorkspace:
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
    mg.activate()
    return ShepherdWorkspace(
        mg,
        trace_store_path=root / ".vcscore" / "shepherd" / "trace.sqlite",
        workspace_path=root,
    )


def _register(workspace: ShepherdWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module_path = tmp_path / "lanec_refute_tasks.py"
    module_path.write_text(_TASK_SOURCE, encoding="utf-8")
    sys.modules.pop("lanec_refute_tasks", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    for qualname in _QUALNAMES:
        workspace.tasks.register(f"lanec_refute_tasks:{qualname}", may_default="Permissive")


def _seed(workspace: ShepherdWorkspace) -> None:
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="docs/guide.md", content=b"g\n")
    workspace.mg.exec("filesystem", "write", scope=workspace.mg.ground, path="backend/app.py", content=b"a\n")


def _ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShepherdWorkspace:
    workspace = _make_workspace(tmp_path / "ws")
    _register(workspace, tmp_path, monkeypatch)
    _seed(workspace)
    return workspace


def _fs_is_case_insensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    return (tmp_path / "caseprobe").exists()


# ==========================================================================================
# TARGET 4 / TARGET 2 — case-insensitive alias defeats validate_disjoint_roots.
# ==========================================================================================


@_JAIL_ONLY
def test_T4_case_alias_bind_refused_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (was a REFUTED hole): binding two roots that differ only by case — `backend`
    (RW) and `BACKEND` (RO) — to the SAME physical dir on case-insensitive APFS is now refused at
    bind time. The aliasing run that let a write to the ReadOnly root land (and be selected into
    live state) can no longer be constructed: the disjoint guard fails closed on the alias.
    """
    workspace = _ws(tmp_path, monkeypatch)
    if not _fs_is_case_insensitive(tmp_path):
        pytest.skip("filesystem is case-sensitive; the case-alias vector does not apply here")
    try:
        (workspace.workspace_path / "backend").mkdir()
        workspace.bind(root="backend", name="backend")
        with pytest.raises(WorkspaceControlError, match=r"overlap|alias|nest"):
            workspace.bind(root="BACKEND", name="alias")
    finally:
        workspace.close()


def test_T4_validate_disjoint_roots_rejects_fs_aliases(tmp_path: Path) -> None:
    """Regression (was a REFUTED hole): validate_disjoint_roots now treats filesystem-aliased
    roots as overlapping — case-insensitive APFS, Unicode NFC/NFD, and hardlink aliases to the
    same directory — so an alias cannot smuggle a ReadOnly subtree into a ReadWrite root's
    writable set. Genuinely disjoint siblings still pass.
    """
    import unicodedata

    from shepherd_dialect.confinement import OverlappingBoundRootsError, validate_disjoint_roots

    (tmp_path / "backend").mkdir()
    (tmp_path / "docs").mkdir()
    # genuinely disjoint siblings still pass (no false positive)
    assert validate_disjoint_roots([str(tmp_path / "backend"), str(tmp_path / "docs")])

    if _fs_is_case_insensitive(tmp_path):
        lower = tmp_path / "backend"
        upper = tmp_path / "BACKEND"  # same inode on APFS, distinct realpath strings
        assert lower.samefile(upper), "precondition: the two names are the same inode"
        lower, upper = str(lower), str(upper)
        with pytest.raises(OverlappingBoundRootsError):
            validate_disjoint_roots([lower, upper])
        # nested case-alias: a RO `BACKEND/vendor` inside a RW `backend` is sub-root semantics
        (tmp_path / "backend" / "vendor").mkdir()
        with pytest.raises(OverlappingBoundRootsError):
            validate_disjoint_roots([str(tmp_path / "backend"), str(tmp_path / "BACKEND" / "vendor")])

    # Unicode NFC/NFD alias — caught by the fold lens regardless of filesystem case-sensitivity
    # (the two byte forms name the same entry on a normalizing FS).
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    if nfc != nfd:
        with pytest.raises(OverlappingBoundRootsError):
            validate_disjoint_roots([str(tmp_path / nfc), str(tmp_path / nfd)])


# ==========================================================================================
# TARGET 2 / TARGET 5 — any-writable settlement must NOT leak to the jail on a heterogeneous run.
# ==========================================================================================


@_JAIL_ONLY
def test_T2_heterogeneous_RO_sibling_write_refused_at_jail_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs RW / backend RO. A raw write under the RO `backend` root must be refused at the
    syscall even though the run collapses to any-writable for SETTLEMENT. Attack succeeds
    (REFUTED) if the write lands; else the invariant holds (survived)."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        with pytest.raises(RunStartError, match=r"PermissionError|Operation not permitted|Read-only"):
            workspace.run("lanec_refute_tasks.ro_write_when_sibling_rw", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert record.enforcement == "jail"
        # the refused write never reached managed state, and nothing is selectable.
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "backend/leak.py") is None
        assert workspace.runs.outputs() == ()
    finally:
        workspace.close()


# ==========================================================================================
# TARGET 3 — deny-closed writable roots.
# ==========================================================================================


@_JAIL_ONLY
def test_T3_two_rw_unbound_write_fails_closed_survived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both bindings RW, but a write to a managed path under NEITHER root. The union of
    writable roots must not include the whole clone."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        with pytest.raises(RunStartError, match=r"PermissionError|Operation not permitted|Read-only"):
            workspace.run("lanec_refute_tasks.two_rw_unbound_write", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "unbound.txt") is None
    finally:
        workspace.close()


@_JAIL_ONLY
def test_T3_dotdot_escape_from_rw_root_fails_closed_survived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `..` climb out of the RW sub-root to a sibling managed path must be refused."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        with pytest.raises(RunStartError, match=r"PermissionError|Operation not permitted|Read-only"):
            workspace.run("lanec_refute_tasks.escape_via_dotdot", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show("@latest")
        assert record is not None
        assert record.status == "failed"
        assert workspace.mg.store.read_workspace_file(workspace.mg.ground.ref, "escaped.txt") is None
    finally:
        workspace.close()


def test_T3_rebased_binding_grants_refuses_dotdot_seam_survived() -> None:
    """Internal seam (no public expression): a joined `..` relative grant root must refuse."""
    from shepherd_dialect.confinement import BindingRootGrant
    from shepherd_dialect.run_driver import _rebased_binding_grants

    with pytest.raises(ValueError, match=r"\.\.|clean working-path-relative"):
        _rebased_binding_grants([BindingRootGrant(binding="x", root="../escape", writable=True)], "/tmp/clone")
    # An absolute root is passed through as-is (documented) — confirm that is the behaviour,
    # so we can reason about whether the facade ever produces one (it relativizes, so no).
    passed = _rebased_binding_grants([BindingRootGrant(binding="x", root="/abs/root", writable=True)], "/tmp/clone")
    assert passed[0].root == "/abs/root"


def test_T3_bind_absolute_outside_workspace_refused_survived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute bind root outside the workspace fails closed at bind time."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        with pytest.raises(WorkspaceControlError, match="outside the workspace"):
            workspace.bind(root="/etc", name="etc")
    finally:
        workspace.close()


# ==========================================================================================
# TARGET 1 — no unconfined execution.
# ==========================================================================================


@_JAIL_ONLY
def test_T1_default_bindings_run_is_jailed_survived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A default (placement=auto) bindings run on a jail-capable host must resolve to the jail,
    never a silent in-process fallback."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        run = workspace.run("lanec_refute_tasks.backend_landing", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.enforcement == "jail"
        assert record.execution_evidence.resolved_placement == "jail"
        assert record.task_executions[0].executor_kind == "confined_process"
    finally:
        workspace.close()


def test_T1_readonly_binding_cannot_be_forced_advisory_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An RO binding under placement='advisory' must fail closed (an in-process device cannot
    enforce a ReadOnly grant) — no unconfined execution of a run that needs the jail."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        with pytest.raises(RunStartError, match="advisory"):
            workspace.run(
                "lanec_refute_tasks.backend_landing",
                bindings={"docs": docs, "backend": backend},
                placement="advisory",
            )
    finally:
        workspace.close()


# ==========================================================================================
# TARGET 5 — settlement rule integrity.
# ==========================================================================================


@_JAIL_ONLY
def test_T5_all_ro_run_select_refused_survived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-ReadOnly run's (empty) whole-delta output must be refused by the any-writable rule,
    not selected as a no-op."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        run = workspace.run("lanec_refute_tasks.all_ro", bindings={"docs": docs, "backend": backend})
        assert run.output().changed_paths == ()
        with pytest.raises(WorkspaceControlError, match="any-writable"):
            workspace.select(run.output())
        # apply is gated by the same any-writable rule (T1 W2.4 iv: parity across mutating verbs).
        with pytest.raises(WorkspaceControlError, match="any-writable"):
            workspace.apply(run.output())
        # release stays allowed (consume-once semantics unchanged).
        released = workspace.release(run.output())
        assert released.settlement.action in {"released", "discarded"}
    finally:
        workspace.close()


@_JAIL_ONLY
def test_T5_settlement_gate_reads_per_binding_not_scalar_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted per_binding_authority evidence for a heterogeneous run records the TRUE
    per-binding authorities (docs RO, backend RW), not a single amplified scalar."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        docs = workspace.bind(root="docs", name="docs")
        backend = workspace.bind(root="backend", name="backend")
        run = workspace.run("lanec_refute_tasks.backend_landing", bindings={"docs": docs, "backend": backend})
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.authority_context is not None
        pba = record.authority_context.per_binding_authority
        assert pba is not None
        assert pba["docs"]["authority"] == "readonly"
        assert pba["backend"]["authority"] == "readwrite"
    finally:
        workspace.close()


# ==========================================================================================
# TARGET 6 — single-binding regression.
# ==========================================================================================


def test_T6_single_binding_has_no_per_binding_evidence_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v0.1 single-binding run (repo=) records no per_binding_authority, so the multi-binding
    settlement gate never engages — single-binding behaviour is unchanged."""
    workspace = _ws(tmp_path, monkeypatch)
    try:
        # register a single-repo task
        module_path = tmp_path / "single_task.py"
        module_path.write_text(
            "from shepherd_runtime.nucleus import GitRepo\n\n"
            "def propose(repo: GitRepo, label: str):\n"
            "    repo.write('c.txt', label.encode())\n"
            "    return {'label': label}\n",
            encoding="utf-8",
        )
        sys.modules.pop("single_task", None)
        monkeypatch.syspath_prepend(str(tmp_path))
        workspace.tasks.register("single_task:propose", may_default="ReadWrite")
        repo = workspace.git_repo()
        run = workspace.run("single_task.propose", repo=repo, args={"label": "x"})
        record = workspace.runs.show(run.run_ref)
        assert record is not None
        assert record.authority_context is not None
        assert record.authority_context.per_binding_authority is None
    finally:
        workspace.close()
