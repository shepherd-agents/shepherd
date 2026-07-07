"""Capability-C Slice 1: the `retained` scope-registry status.

`retained` is a ref-owning status — a sealed-but-undisposed scope keeps its ref
on disk and is adoptable — but it is not runtime-open. Corruption detection
treats it like `live` for ref ownership, NOT like `merged`/`discarded` (a
reclaimed ref), while recovery must not treat it as abandoned live work.

`retained` is an unconditionally legitimate status (seal-and-select is always
on): a structurally-sound retained record is never a registry mismatch, while a
retained record with broken parentage or a missing ref still fails closed. These
tests pin that recognition contract; the seal lifecycle coverage asserts the
write path itself.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import get_args

import pytest
from click.testing import CliRunner
from vcs_core import WORLD_TRANSITION_SCHEMA, InvalidRepositoryStateError, WorldSnapshot
from vcs_core._app import (
    AppCommandBlocked,
    AppOpenMode,
    AppScopeNotFound,
    VcsCoreApp,
    _build_scope_index,
)
from vcs_core._projection_store import (
    REF_OWNING_SCOPE_STATUSES,
    RUNTIME_OPEN_SCOPE_STATUSES,
    SCOPE_REGISTRY_CURRENT_REF,
    TERMINAL_SCOPE_STATUSES,
    ScopeRegistryEntry,
    ScopeRegistryStatus,
)
from vcs_core._query_readiness import ReadinessRequest
from vcs_core._recovery_inventory import (
    recovery_inventory_snapshot_for_store,
    scope_ref_recovery_classification,
)
from vcs_core.cli import main
from vcs_core.git_store import build_tree, create_signature
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


def _entry(
    task,
    *,
    status: ScopeRegistryStatus,
    parent_ref: str = Store.GROUND_REF,
) -> ScopeRegistryEntry:
    assert task.world_id is not None
    return ScopeRegistryEntry(
        name=task.name,
        ref=task.ref,
        instance_id=task.instance_id,
        creation_oid=task.creation_oid,
        parent_ref=parent_ref,
        world_id=task.world_id,
        isolation_mode="shared",
        status=status,
    )


def _publish_retained(store: Store, *, parent_ref: str = Store.GROUND_REF):
    task = store.fork(Store.GROUND_REF, "task")
    base = store.require_scope_registry_projection()
    assert store.publish_scope_registry_projection(
        entries=(_entry(task, status="retained", parent_ref=parent_ref),),
        expected_head_oid=base.head_oid,
    )
    return task


def _write_invalid_scope_registry_projection(store: Store) -> None:
    manifest = {
        "family": "scope-registry",
        "version": 1,
        "completeness": "complete",
        "source": "not-a-list",
        "source_digest": "invalid",
    }
    tree_oid = build_tree(
        store._repo,
        None,
        [("meta/projection.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))],
    )
    sig = create_signature("projection")
    commit_oid = store._repo.create_commit(
        None,
        sig,
        sig,
        "projection:scope-registry",
        tree_oid,
        [],
    )
    if SCOPE_REGISTRY_CURRENT_REF in store._repo.references:
        store._repo.references[SCOPE_REGISTRY_CURRENT_REF].set_target(commit_oid)
    else:
        store._repo.references.create(SCOPE_REGISTRY_CURRENT_REF, commit_oid)


def _publish_empty_v2_scope_world(mg: VcsCore, ref: str, *, operation_id: str) -> None:
    manager = mg._world_storage()
    world_oid = manager.create_unsafe_world(
        snapshot=WorldSnapshot(),
        transition={
            "schema": WORLD_TRANSITION_SCHEMA,
            "operation_id": operation_id,
            "parent_worlds": [],
        },
        operation_final={
            "schema": "vcscore/operation-final/v2",
            "operation_id": operation_id,
            "selected": {},
            "candidate_commits": [],
            "candidate_outcomes": [],
            "head_selections": [],
            "selection_evidence": [],
        },
    )
    assert manager.publish_root_world(ref=ref, world_oid=world_oid)


def test_ref_owning_runtime_open_and_terminal_statuses_partition_the_literal() -> None:
    # Exhaustiveness guard: every status is classified exactly once. A future
    # status added to the Literal but not to a partition set trips here.
    all_statuses = set(get_args(ScopeRegistryStatus))
    assert all_statuses == REF_OWNING_SCOPE_STATUSES | TERMINAL_SCOPE_STATUSES
    assert REF_OWNING_SCOPE_STATUSES.isdisjoint(TERMINAL_SCOPE_STATUSES)
    assert RUNTIME_OPEN_SCOPE_STATUSES < REF_OWNING_SCOPE_STATUSES
    assert "retained" in REF_OWNING_SCOPE_STATUSES
    assert "retained" not in RUNTIME_OPEN_SCOPE_STATUSES
    assert "live" in RUNTIME_OPEN_SCOPE_STATUSES


def test_retained_status_round_trips(store: Store) -> None:
    # Recognition is unconditional: the parse validator/serializer accept
    # `retained` regardless of the flag (else the entry is silently dropped on
    # load). Only *legitimacy* is flag-gated.
    task = store.fork(Store.GROUND_REF, "task")
    retained = _entry(task, status="retained")
    base = store.require_scope_registry_projection()
    assert store.publish_scope_registry_projection(entries=(retained,), expected_head_oid=base.head_oid)

    snapshot = store.load_scope_registry_projection()
    assert snapshot is not None
    assert snapshot.entries == (retained,)
    assert snapshot.entries_by_name["task"].status == "retained"


def test_retained_ref_on_disk_is_not_corruption(store: Store) -> None:
    # Flag on: a retained scope legitimately keeps its ref (it is adoptable),
    # unlike a merged/discarded scope whose ref-on-disk *is* corruption.
    _publish_retained(store)
    assert store.scope_registry_projection_mismatches() == ()


def test_retained_parentage_is_still_checked(store: Store) -> None:
    # Exempting retained from `ref_exists_registry_non_live` must NOT exempt it
    # from parentage validation — a sealed candidate's parent linkage must be sound.
    _publish_retained(store, parent_ref="refs/vcscore/scopes/missing-parent")
    mismatches = store.scope_registry_projection_mismatches()
    assert [m.kind for m in mismatches] == ["parentage_disagrees"]


def test_retained_missing_ref_is_flagged(store: Store) -> None:
    # A retained entry whose ref vanished is still corruption (its ref should exist).
    task = _publish_retained(store)
    store.discard(task)  # removes the ref from disk; the registry still says retained
    mismatches = store.scope_registry_projection_mismatches()
    assert [m.kind for m in mismatches] == ["registry_live_ref_missing"]


def test_live_scope_has_no_registry_mismatch(store: Store) -> None:
    # A normal live scope is a healthy registry entry (no mismatch).
    task = store.fork(Store.GROUND_REF, "task")
    base = store.require_scope_registry_projection()
    assert store.publish_scope_registry_projection(
        entries=(_entry(task, status="live"),), expected_head_oid=base.head_oid
    )
    assert store.scope_registry_projection_mismatches() == ()


def test_retained_ref_is_protected_from_orphan_archival(mg: VcsCore) -> None:
    # Flag on: a retained scope's ref must be in the protected ref-owning set, so
    # `archive-orphaned-scopes` (which reclaims any ref NOT in that set) cannot destroy a
    # sealed best-of-N candidate. Mirrors the recovery-inventory orphan handling.
    task = _publish_retained(mg.store)
    classification = scope_ref_recovery_classification(mg.store, mg.store.repo_path)
    assert task.ref in classification.protected_ref_owning_refs
    assert task.ref not in classification.orphaned_scope_refs


def test_no_untriaged_live_status_comparisons() -> None:
    import vcs_core

    src_root = Path(vcs_core.__file__).resolve().parent

    found: set[tuple[str, str]] = set()
    for path in src_root.rglob("*.py"):
        found.update(_live_status_literal_sites(src_root, path))

    allowed = {
        # fork/restore write a freshly-created scope as "live" — correct by definition.
        ("_vcscore_lifecycle.py::_publish_scope_registry_fork_locked", 'status="live",'),
        ("_vcscore_lifecycle.py::_publish_scope_registry_status_locked", 'status="live",'),
        # the fork gate is intentionally live-only: a retained sibling must NOT block a
        # new fork (G3 free).
        (
            "_vcscore_lifecycle.py::_assert_can_fork_from_registry",
            'if entry.status == "live" and entry.parent_ref == parent.ref:',
        ),
        # _build_scope_index restores only live scopes as runtime handles; retained is
        # surfaced separately via ScopeIndex.retained (see the comment there).
        ("_app.py::_build_scope_index", 'remaining = [entry for entry in snapshot.entries if entry.status == "live"]'),
        # The JSON parser enumerates every supported registry status literal; this is
        # validation of the wire format, not an active-status policy decision.
        (
            "_projection_store.py::_scope_registry_entry_from_json",
            'if status not in ("live", "merged", "retained", "discarded"):',
        ),
    }
    assert found == allowed, (
        "Un-triaged `live` status comparison(s) — for each NEW site decide: genuinely "
        "live-only/runtime-open (add to the allow-list with a justification) or should "
        "it use the ref-owning classifier (include retained)?\n"
        f"  unexpected (new, un-triaged): {sorted(found - allowed)}\n"
        f"  missing (allow-list stale):   {sorted(allowed - found)}"
    )


def _live_status_literal_sites(src_root: Path, path: Path) -> set[tuple[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(lines), filename=str(path))
    visitor = _LiveStatusLiteralVisitor(path.relative_to(src_root), lines)
    visitor.visit(tree)
    return visitor.found


class _LiveStatusLiteralVisitor(ast.NodeVisitor):
    def __init__(self, relpath: Path, lines: list[str]) -> None:
        self._relpath = relpath
        self._lines = lines
        self._context: list[str] = []
        self.found: set[tuple[str, str]] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_context(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_context(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_context(node, node.name)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            if not isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn):
                continue
            left = operands[index]
            right = operands[index + 1]
            if (_is_status_expr(left) and _contains_live_literal(right)) or (
                _contains_live_literal(left) and _is_status_expr(right)
            ):
                self._record(node.lineno)
                break
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "status" and _contains_live_literal(keyword.value):
                self._record(keyword.value.lineno)
        self.generic_visit(node)

    def _visit_context(self, node: ast.AST, name: str) -> None:
        self._context.append(name)
        try:
            self.generic_visit(node)
        finally:
            self._context.pop()

    def _record(self, lineno: int) -> None:
        context = "::".join(str(part) for part in (self._relpath, *self._context))
        self.found.add((context, self._lines[lineno - 1].strip()))


def _is_status_expr(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "status") or (
        isinstance(node, ast.Attribute) and node.attr == "status"
    )


def _contains_live_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == "live"
    if isinstance(node, ast.List | ast.Set | ast.Tuple):
        return any(_contains_live_literal(item) for item in node.elts)
    return False


def test_retained_scope_survives_archive_orphaned_scopes(mg: VcsCore) -> None:
    # End-to-end protection (recovery-inventory candidate generation + protected-ref
    # exclusion together, exactly as `_app.archive_orphaned_scopes` wires them):
    # archive-orphaned-scopes must NOT reclaim a sealed candidate's ref.
    task = _publish_retained(mg.store)
    classification = scope_ref_recovery_classification(mg.store, mg.store.repo_path)
    archived = mg.archive_orphaned_scopes(exclude_refs=classification.protected_ref_owning_refs)
    assert task.ref not in archived
    assert task.name not in archived
    assert mg.store.ref_exists(task.ref)
    snapshot = mg.store.load_scope_registry_projection()
    assert snapshot is not None
    assert snapshot.entries_by_name[task.name].status == "retained"


def test_recovery_app_keeps_valid_retained_scope(
    mg: VcsCore,
    workspace: Path,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)

    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.RECOVERY) as app:
        archived = app.archive_orphaned_scopes()

    assert archived == []
    reopened = VcsCore.from_config(str(workspace))
    assert reopened.store.ref_exists(task.ref)
    entry = reopened.store.scope_registry_entry(task.name)
    assert entry is not None
    assert entry.status == "retained"


def test_valid_retained_is_not_recovery_orphan_for_store(
    mg: VcsCore,
) -> None:
    task = _publish_retained(mg.store)

    inventory = recovery_inventory_snapshot_for_store(mg.store.repo_path, mg.store)
    orphaned = tuple(
        item
        for item in inventory.items
        if item.domain == "recovery" and item.kind == "orphaned_scope_ref" and item.locator == task.ref
    )

    assert orphaned == ()


def test_valid_retained_absent_from_owner_and_legacy_recovery_snapshots(
    mg: VcsCore,
    workspace: Path,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)
    reopened = VcsCore.from_config(str(workspace))
    reopened.activate()
    try:
        orphaned_items = tuple(
            item
            for item in reopened.recovery_inventory().items
            if item.domain == "recovery" and item.kind == "orphaned_scope_ref" and item.locator == task.ref
        )

        assert orphaned_items == ()
        assert task.ref not in reopened.recovery_snapshot().orphaned_scope_refs
    finally:
        reopened.deactivate(warn_on_open_scopes=False)


def test_valid_retained_readiness_has_no_orphaned_scope_blocker(
    mg: VcsCore,
    workspace: Path,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)
    reopened = VcsCore.from_config(str(workspace))
    reopened.activate()
    try:
        result = reopened.query_readiness(
            ReadinessRequest.create(
                command="vcscore.push-status",
                requested_freshness="locked",
                allow_best_effort=False,
            )
        )
        orphaned_items = tuple(
            item
            for item in result.snapshot.items
            if item.domain == "recovery" and item.kind == "orphaned_scope_ref" and item.locator == task.ref
        )
        orphaned_blockers = tuple(
            blocker for blocker in result.blockers if blocker.item_id == f"recovery:orphaned_scope:{task.ref}"
        )

        assert orphaned_items == ()
        assert orphaned_blockers == ()
    finally:
        reopened.deactivate(warn_on_open_scopes=False)


def test_valid_retained_assess_push_does_not_raise_orphaned_scope(
    mg: VcsCore,
    workspace: Path,
) -> None:
    _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)
    reopened = VcsCore.from_config(str(workspace))
    reopened.activate()
    try:
        reopened.assess_push()
    finally:
        reopened.deactivate(warn_on_open_scopes=False)


def test_direct_archive_orphaned_scopes_does_not_archive_valid_retained(
    mg: VcsCore,
    workspace: Path,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)
    reopened = VcsCore.from_config(str(workspace))
    reopened.activate()
    try:
        assert task.ref not in reopened.list_orphaned_scope_refs()

        archived = reopened.archive_orphaned_scopes()

        assert archived == []
        assert reopened.store.ref_exists(task.ref)
        entry = reopened.store.scope_registry_entry(task.name)
        assert entry is not None
        assert entry.status == "retained"
    finally:
        reopened.deactivate(warn_on_open_scopes=False)


def test_valid_retained_repo_status_succeeds(
    mg: VcsCore,
    workspace: Path,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)

    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.CONTROL) as app:
        summary = app.repo_status()

    assert task.name in {entry.name for entry in summary.retained_scopes}
    assert all(blocker.kind != "orphaned_scope" for blocker in summary.blockers)


def test_cli_status_reports_retained_not_orphaned(
    mg: VcsCore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(main, ["status"])

    assert result.exit_code == 0, result.output
    assert "Retained scopes:" in result.output
    assert f"  {task.name}" in result.output
    assert "Orphaned scopes:" not in result.output


def test_recovery_cli_does_not_report_valid_retained_as_orphaned(
    mg: VcsCore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _publish_retained(mg.store)
    mg.deactivate(warn_on_open_scopes=False)
    monkeypatch.chdir(workspace)

    result = CliRunner().invoke(main, ["recovery"])

    assert result.exit_code == 0, result.output
    assert "Orphaned scopes:" not in result.output or f"    {task.name}" not in result.output


def test_unreadable_scope_registry_still_fails_closed_as_recovery(mg: VcsCore) -> None:
    task = mg.fork(mg.ground, "task")
    v2_only_ref = "refs/vcscore/scopes/v2-only"
    _publish_empty_v2_scope_world(mg, v2_only_ref, operation_id="seed-v2-only")
    _write_invalid_scope_registry_projection(mg.store)

    inventory = recovery_inventory_snapshot_for_store(mg.store.repo_path, mg.store)
    orphaned_refs = {
        item.locator for item in inventory.items if item.domain == "recovery" and item.kind == "orphaned_scope_ref"
    }

    assert task.ref in orphaned_refs
    assert v2_only_ref in orphaned_refs


def test_recovery_app_keeps_retained_scope_with_parentage_mismatch(
    mg: VcsCore,
    workspace: Path,
) -> None:
    # A retained scope with broken parent linkage is still blocked — the
    # parentage mismatch is the genuine structural fault (retained-ness itself
    # is legitimate).
    _publish_retained(mg.store, parent_ref="refs/vcscore/scopes/missing-parent")
    mg.deactivate(warn_on_open_scopes=False)

    with VcsCoreApp.open_existing(str(workspace), mode=AppOpenMode.RECOVERY) as app:
        blockers = app.push_blockers()
        scope_registry_blockers = tuple(blocker for blocker in blockers if blocker.kind == "scope_registry_mismatch")
        assert {blocker.detail for blocker in scope_registry_blockers} == {
            "Registry parent linkage disagrees with the 'retained' scope ref topology.",
        }
        with pytest.raises(AppCommandBlocked) as excinfo:
            app.archive_orphaned_scopes()

    assert excinfo.value.command == "archive-orphaned-scopes"
    assert {blocker.detail for blocker in excinfo.value.blockers} == {
        "Registry parent linkage disagrees with the 'retained' scope ref topology.",
    }


def test_direct_archive_orphaned_scopes_blocks_retained_with_parentage_mismatch(
    mg: VcsCore,
    workspace: Path,
) -> None:
    _publish_retained(mg.store, parent_ref="refs/vcscore/scopes/missing-parent")
    mg.deactivate(warn_on_open_scopes=False)
    reopened = VcsCore.from_config(str(workspace))
    reopened.activate()
    try:
        with pytest.raises(InvalidRepositoryStateError, match="readiness blocked by task"):
            reopened.archive_orphaned_scopes()
        mismatches = reopened.store.scope_registry_projection_mismatches()
        assert {mismatch.kind for mismatch in mismatches} == {"parentage_disagrees"}
    finally:
        reopened.deactivate(warn_on_open_scopes=False)


def test_legacy_three_status_snapshot_unchanged(store: Store) -> None:
    # Regression: a pre-retained registry (only live/merged/discarded) round-trips
    # unchanged. The merged/discarded refs left on disk by `fork` surface as the
    # ordinary `ref_exists_registry_non_live` reclaim signal — never anything
    # retained-shaped.
    live = store.fork(Store.GROUND_REF, "live-task")
    merged = store.fork(Store.GROUND_REF, "merged-task")
    discarded = store.fork(Store.GROUND_REF, "discarded-task")
    base = store.require_scope_registry_projection()
    assert store.publish_scope_registry_projection(
        entries=(
            _entry(live, status="live"),
            _entry(merged, status="merged"),
            _entry(discarded, status="discarded"),
        ),
        expected_head_oid=base.head_oid,
    )
    snapshot = store.load_scope_registry_projection()
    assert snapshot is not None
    assert {e.name: e.status for e in snapshot.entries} == {
        "live-task": "live",
        "merged-task": "merged",
        "discarded-task": "discarded",
    }
    kinds = {m.kind for m in store.scope_registry_projection_mismatches()}
    assert kinds <= {"ref_exists_registry_non_live"}


def test_retained_scope_indexed_as_retained_not_terminal(mg: VcsCore) -> None:
    # Direct read-surface guard for `_build_scope_index` / ScopeIndex bucketing: a sealed
    # scope must surface via the new `.retained` bucket (active + adoptable) and has no live
    # runtime handle, so it is absent from `.entries`. Crucially it must NOT be lumped into
    # the *terminal* bucket — under the old `status != "live"` filter it would have been, and
    # resolving it would wrongly raise AppScopeTerminalState. With the fix, resolution raises
    # AppScopeNotFound (no live handle) instead — the public-behaviour witness of the split.
    task = _publish_retained(mg.store)
    index = _build_scope_index(mg)
    assert task.name in {entry.name for entry in index.retained}
    assert task.name not in {entry.name for entry in index.entries}
    with pytest.raises(AppScopeNotFound):
        index.resolve_scope(task.name)
