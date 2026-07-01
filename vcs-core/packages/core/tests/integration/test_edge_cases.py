"""Edge-case and error-path tests for vcs-core.

Probes scenarios not covered by the existing 89-test suite:
double merge, merge-after-discard, fork-from-archive, empty merge,
deep nesting, large batch, session lock contention, deactivate with
open scopes, push with no pending work, and delete-then-recreate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vcs_core._errors import (
    ActivationError,
    StaleScopeError,
)
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.scope_stack import ScopeStack
from vcs_core.store import Store
from vcs_core.substrates import DeclarativeFilesystemSubstrate
from vcs_core.vcscore import VcsCore

from ..support.builders import make_marker_filesystem_vcscore, make_store

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mg(workspace: Path) -> VcsCore:
    """Activated VcsCore with marker + filesystem substrates."""
    m = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        yield m
    finally:
        m.deactivate()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# (a) Double merge: merge the same scope twice
# ---------------------------------------------------------------------------


class TestDoubleMerge:
    """Merging a scope that was already merged should fail cleanly."""

    def test_store_double_merge_raises_stale_scope(self, workspace: Path) -> None:
        """At the Store level, the ref is deleted after the first merge,
        so the second merge should raise StaleScopeError."""
        store = make_store(workspace)
        store.create_root_commit()

        task = store.fork(Store.GROUND_REF, "task-double")
        store._emit_effect(task, "Test", {}, substrate="agent")
        store.merge(task, Store.GROUND_REF)

        # Second merge: ref is gone
        with pytest.raises(StaleScopeError):
            store.merge(task, Store.GROUND_REF)

    def test_vcscore_double_merge_raises(self, mg: VcsCore) -> None:
        """At the VcsCore level, double merge should also fail."""
        task = mg.fork(mg.ground, "task-double-mg")
        mg.merge(task, mg.ground)

        # The scope was removed from _active_scopes; the store ref is gone.
        # Depending on which check fires first, we get either KeyError
        # (from _active_scopes cleanup) or StaleScopeError (from store).
        with pytest.raises((StaleScopeError, KeyError)):
            mg.merge(task, mg.ground)


# ---------------------------------------------------------------------------
# (b) Merge after discard
# ---------------------------------------------------------------------------


class TestMergeAfterDiscard:
    """Merging a scope that was discarded should raise StaleScopeError."""

    def test_store_merge_after_discard(self, workspace: Path) -> None:
        store = make_store(workspace)
        store.create_root_commit()

        task = store.fork(Store.GROUND_REF, "task-discard-merge")
        store._emit_effect(task, "Test", {}, substrate="agent")
        store.discard(task)

        with pytest.raises(StaleScopeError):
            store.merge(task, Store.GROUND_REF)

    def test_vcscore_merge_after_discard(self, mg: VcsCore) -> None:
        task = mg.fork(mg.ground, "task-discard-merge-mg")
        mg.discard(task)

        # Scope already removed from _active_scopes by discard()
        with pytest.raises((StaleScopeError, KeyError)):
            mg.merge(task, mg.ground)


# ---------------------------------------------------------------------------
# (c) Fork from a discarded (archived) scope
# ---------------------------------------------------------------------------


class TestForkFromDiscardedScope:
    """Forking from an archived scope's ref should fail because the
    active scope ref was deleted and replaced with an archive ref."""

    def test_store_fork_from_archived_ref_fails(self, workspace: Path) -> None:
        """The original scope ref is deleted on discard. Attempting to
        fork from it should raise KeyError (ref not found)."""
        repo_path = str(workspace / ".vcscore")
        store = Store(repo_path)
        store.create_root_commit()

        task = store.fork(Store.GROUND_REF, "task-archived")
        store._emit_effect(task, "Test", {}, substrate="agent")
        archive_ref = store.discard(task)

        # The active ref is gone
        with pytest.raises(KeyError):
            store.fork(task.ref, "child-from-dead")

    def test_store_fork_from_archive_ref_succeeds(self, workspace: Path) -> None:
        """But forking from the archive ref *does* work -- it's a valid
        Git ref. This is useful for forensic inspection."""
        repo_path = str(workspace / ".vcscore")
        store = Store(repo_path)
        store.create_root_commit()

        task = store.fork(Store.GROUND_REF, "task-archived2")
        store._emit_effect(task, "Test", {}, substrate="agent")
        archive_ref = store.discard(task)

        # Fork from the archive ref directly
        child = store.fork(archive_ref, "forensic-child")
        assert child.name == "forensic-child"


# ---------------------------------------------------------------------------
# (d) Empty scope merge: fork and immediately merge (no effects)
# ---------------------------------------------------------------------------


class TestEmptyScopeMerge:
    """Fork, immediately merge with no effects emitted. The ScopeMerge
    structural effect should still be produced by VcsCore.merge()."""

    def test_store_empty_merge_produces_no_scope_merge(self, workspace: Path) -> None:
        """Store.merge() is a raw fast-forward -- no ScopeMerge effect.
        That effect is added by VcsCore.merge(), not Store.merge()."""
        repo_path = str(workspace / ".vcscore")
        store = Store(repo_path)
        store.create_root_commit()

        task = store.fork(Store.GROUND_REF, "task-empty")
        # No _emit_effect calls
        store.merge(task, Store.GROUND_REF)

        merges = store.filter_effects(effect_type="ScopeMerge")
        assert len(merges) == 0  # Store doesn't emit ScopeMerge

    def test_vcscore_empty_merge_produces_scope_merge(self, mg: VcsCore) -> None:
        """VcsCore.merge() emits a ScopeMerge effect even for empty scopes."""
        task = mg.fork(mg.ground, "task-empty-mg")
        mg.merge(task, mg.ground)

        merges = mg.filter_effects(effect_type="ScopeMerge")
        assert len(merges) == 1
        assert merges[0].metadata["merged_into"] == "ground"

    def test_empty_merge_advances_ground(self, mg: VcsCore) -> None:
        """An empty merge should still advance the ground ref (ScopeMerge
        commit is created), so commits_ahead > 0 before push."""
        task = mg.fork(mg.ground, "task-empty-adv")
        mg.merge(task, mg.ground)

        status = mg.status()
        assert status.commits_ahead > 0
        assert status.local_changes == 0  # no file changes


# ---------------------------------------------------------------------------
# (e) Deep nesting: 10 levels deep, merge all the way back up
# ---------------------------------------------------------------------------


class TestDeepNesting:
    """Fork 10 levels deep, then merge all the way back up."""

    def test_scope_stack_10_levels(self, mg: VcsCore) -> None:
        ss = ScopeStack(mg)
        marker = mg.lifecycle_substrates[0]

        # Fork 10 levels deep, emitting a marker at each level
        for i in range(10):
            ss.begin_scope(f"level-{i}")
            marker._pipeline.set_scope(ss.current)
            marker.mark(f"Marker-{i}", {"level": i})

        assert ss.depth == 10

        # Merge all 10 levels back up
        for i in range(10):
            ss.commit_scope()

        assert ss.depth == 0
        assert ss.current == mg.ground

        # All 10 markers should be visible from ground
        markers = mg.filter_effects(effect_type="Marker")
        assert len(markers) == 10

        # All 10 ScopeMerge effects should exist
        merges = mg.filter_effects(effect_type="ScopeMerge")
        assert len(merges) == 10

    def test_direct_api_10_levels(self, mg: VcsCore) -> None:
        """Same test using direct VcsCore API (no ScopeStack)."""
        scopes = []
        parent = mg.ground

        for i in range(10):
            scope = mg.fork(parent, f"deep-{i}")
            mg.store._emit_effect(scope, "Ping", {"depth": i}, substrate="agent")
            scopes.append((scope, parent))
            parent = scope

        # Merge in reverse order
        for scope, parent_scope in reversed(scopes):
            mg.merge(scope, parent_scope)

        log = mg.log(max_count=200)
        pings = [e for e in log if e.metadata.get("type") == "Ping"]
        assert len(pings) == 10


# ---------------------------------------------------------------------------
# (f) Large batch: 100 file changes in one scope
# ---------------------------------------------------------------------------


class TestLargeBatch:
    """Record 100 file changes in a single scope, verify all tracked."""

    def test_100_file_changes(self, mg: VcsCore) -> None:
        task = mg.fork(mg.ground, "task-100files")
        fs = mg.lifecycle_substrates[1]
        fs._pipeline.set_scope(task)

        changes = [(f"src/file_{i:03d}.py", f"content_{i}".encode()) for i in range(100)]
        oids = fs.record_changes(changes)
        assert len(oids) == 100

        mg.merge(task, mg.ground)

        # All files should appear in the diff
        diff = mg.diff()
        assert len(diff.files) == 100

        # All should be FileCreate
        creates = mg.filter_effects(effect_type="FileCreate")
        assert len(creates) == 100

        # Status should show 100 local changes
        status = mg.status()
        assert status.local_changes == 100

        # Push should materialize all of them
        plan = mg.push(dry_run=True)
        assert plan.total_operations == 100


# ---------------------------------------------------------------------------
# (g) Session lock contention
# ---------------------------------------------------------------------------


class TestSessionLockContention:
    """Two VcsCore instances on the same repo: second should fail."""

    def test_second_activate_raises_activation_error(self, workspace: Path) -> None:
        m1 = VcsCore(str(workspace))
        m1.activate()

        m2 = VcsCore(str(workspace))
        with pytest.raises(ActivationError):
            m2.activate()

        # Cleanup: deactivate m1
        m1.deactivate()

    def test_after_deactivate_second_can_activate(self, workspace: Path) -> None:
        """After the first deactivates, a second instance should succeed."""
        m1 = VcsCore(str(workspace))
        m1.activate()
        m1.deactivate()

        m2 = VcsCore(str(workspace))
        m2.activate()  # Should not raise
        m2.deactivate()


# ---------------------------------------------------------------------------
# (h) Deactivate while scopes are open
# ---------------------------------------------------------------------------


class TestDeactivateWithOpenScopes:
    """Deactivate while child scopes are still open."""

    def test_deactivate_with_open_scopes_succeeds(self, mg: VcsCore) -> None:
        """deactivate() releases the lock but does NOT error on open scopes.
        The open scope's ref remains in the Git repo (orphaned)."""
        task = mg.fork(mg.ground, "task-orphan")
        mg.deactivate()

        # ground is now None
        with pytest.raises(RuntimeError, match="not activated"):
            _ = mg.ground

    def test_orphaned_scope_ref_persists(self, workspace: Path) -> None:
        """After deactivate with an open scope, the scope's Git ref still
        exists in the bare repo. A new session can see it."""
        import pygit2

        m = VcsCore(str(workspace))
        m.activate()
        task = m.fork(m.ground, "task-orphan-ref")
        m.deactivate()

        # Inspect the bare repo directly
        repo = pygit2.Repository(str(workspace / ".vcscore"))
        assert "refs/vcscore/scopes/task-orphan-ref" in repo.references

    def test_push_after_deactivate_reactivate_with_merged_scopes(self, workspace: Path) -> None:
        """Reactivate after deactivate where all scopes were merged.
        No orphaned refs remain, so push() succeeds."""
        m1 = VcsCore(str(workspace))
        m1.activate()
        task = m1.fork(m1.ground, "task-orphan-push")
        # Make a file change so ground moves ahead
        m1.store._emit_effect(task, "Test", {}, substrate="agent")
        m1.merge(task, m1.ground)
        m1.deactivate()

        # New session -- no orphaned refs since task was merged
        m2 = VcsCore(str(workspace))
        m2.activate()
        plan = m2.push()
        assert plan.commits_ahead >= 0
        m2.deactivate()


# ---------------------------------------------------------------------------
# (i) Push with no pending work (ground == materialized)
# ---------------------------------------------------------------------------


class TestPushNoPendingWork:
    """Push when there's nothing to push."""

    def test_push_no_work_returns_empty_plan(self, mg: VcsCore) -> None:
        """push() with ground == materialized should return an empty plan
        with 0 commits ahead and no phases."""
        plan = mg.push()
        assert plan.commits_ahead == 0
        assert plan.total_operations == 0
        assert len(plan.phases) == 0

    def test_push_dry_run_no_work(self, mg: VcsCore) -> None:
        """Dry run with no work should also return an empty plan."""
        plan = mg.push(dry_run=True)
        assert plan.commits_ahead == 0
        assert plan.total_operations == 0

    def test_double_push_idempotent(self, mg: VcsCore) -> None:
        """Two consecutive pushes: second is a no-op."""
        task = mg.fork(mg.ground, "task-push-idem")
        fs = mg.lifecycle_substrates[1]
        fs._pipeline.set_scope(task)
        fs.record_changes([("idempotent.py", b"content")])
        mg.merge(task, mg.ground)

        plan1 = mg.push()
        assert plan1.commits_ahead > 0

        plan2 = mg.push()
        assert plan2.commits_ahead == 0
        assert plan2.total_operations == 0


# ---------------------------------------------------------------------------
# (j) File deletion then recreation in the same scope
# ---------------------------------------------------------------------------


class TestDeleteThenRecreate:
    """Delete a file then create it again in the same scope."""

    def test_delete_then_recreate_same_scope(self, workspace: Path) -> None:
        """Create a file, merge. Then in a new scope, delete and recreate it.
        The final workspace should contain the file with new content."""
        repo_path = str(workspace / ".vcscore")
        store = Store(repo_path)
        store.create_root_commit()
        fs = DeclarativeFilesystemSubstrate(build_builtin_substrate_context(store))

        # Create file in first scope
        task1 = store.fork(Store.GROUND_REF, "task-create")
        fs._pipeline.set_scope(task1)
        fs.record_changes([("target.py", b"original")])
        store.merge(task1, Store.GROUND_REF)

        # Delete and recreate in second scope
        task2 = store.fork(Store.GROUND_REF, "task-recreate")
        fs._pipeline.set_scope(task2)
        fs.record_changes([("target.py", None)])  # delete
        fs.record_changes([("target.py", b"recreated")])  # recreate
        store.merge(task2, Store.GROUND_REF)

        # File should exist with new content
        assert store.file_exists_in_workspace(Store.GROUND_REF, "target.py")

        # Verify content via pygit2
        import pygit2

        repo = pygit2.Repository(repo_path)
        tip = repo.references[Store.GROUND_REF].peel(pygit2.Commit)
        ws = repo.get(tip.tree["workspace"].id)
        blob = repo.get(ws["target.py"].id)
        assert blob.data == b"recreated"

    def test_delete_then_recreate_effect_types(self, workspace: Path) -> None:
        """The delete should be FileDelete, and the recreate should be
        FileCreate (not FilePatch), since the file doesn't exist after
        deletion."""
        repo_path = str(workspace / ".vcscore")
        store = Store(repo_path)
        store.create_root_commit()
        fs = DeclarativeFilesystemSubstrate(build_builtin_substrate_context(store))

        # Create file
        task1 = store.fork(Store.GROUND_REF, "task-create-fx")
        fs._pipeline.set_scope(task1)
        fs.record_changes([("fx.py", b"original")])
        store.merge(task1, Store.GROUND_REF)

        # Delete then recreate
        task2 = store.fork(Store.GROUND_REF, "task-recreate-fx")
        fs._pipeline.set_scope(task2)
        fs.record_changes([("fx.py", None)])  # delete
        fs.record_changes([("fx.py", b"new content")])  # recreate
        store.merge(task2, Store.GROUND_REF)

        # Check effect types
        deletes = store.filter_effects(effect_type="FileDelete")
        assert any(e.metadata.get("path") == "fx.py" for e in deletes)

        creates = store.filter_effects(effect_type="FileCreate")
        assert any(e.metadata.get("path") == "fx.py" for e in creates)

    def test_vcscore_delete_recreate_integration(self, mg: VcsCore) -> None:
        """Full VcsCore-level test: create, merge, then delete+recreate."""
        fs = mg.lifecycle_substrates[1]

        # Create
        t1 = mg.fork(mg.ground, "task-cr1")
        fs._pipeline.set_scope(t1)
        fs.record_changes([("file.py", b"v1")])
        mg.merge(t1, mg.ground)

        # Delete + recreate
        t2 = mg.fork(mg.ground, "task-cr2")
        fs._pipeline.set_scope(t2)
        fs.record_changes([("file.py", None)])
        fs.record_changes([("file.py", b"v2")])
        mg.merge(t2, mg.ground)

        # Diff from materialized should show the file (it went from
        # v1 at materialized to v2 at ground)
        diff = mg.diff()
        paths = {f.path for f in diff.files}
        assert "file.py" in paths
