"""Tests verifying prerequisites for cache effect replay.

These tests ensure that ExecutionContext.apply_effect() implementations
do NOT filter by context_id, which is essential for cache replay to work.

Background:
-----------
When a task result is cached with CacheMode.FULL, effects are stored with
their original context_id (e.g., "workspace:/tmp/sandbox-abc123:a1b2c3d4").
On cache hit, these effects are replayed into a NEW execution context that
has a DIFFERENT context_id (e.g., "workspace:/tmp/sandbox-xyz789:a1b2c3d4").

If apply_effect() filtered by context_id, replayed effects would be silently
ignored, breaking state reconstruction.

Solution:
---------
Contexts trust the scope's binding_name routing to deliver only relevant
effects. The scope routes effects by binding_name (stable), not context_id
(which changes per execution). See SessionState for the canonical pattern.

See: DESIGN-effect-replay.md Phase 0
"""

# Import effects not exported by shepherd package
from shepherd_contexts import KVStoreContext, WorkspaceRef
from shepherd_contexts.kvstore.effects import KeyDeleted, KeySet
from shepherd_contexts.session.effects import SessionCreated
from shepherd_contexts.simple_workspace.context import SimpleWorkspace
from shepherd_contexts.simple_workspace.delta import FileChangeset, FileDelta
from shepherd_contexts.simple_workspace.effects import SimpleWorkspaceChangesetCaptured
from shepherd_contexts.workspace.effects import WorkspacePatchCaptured
from shepherd_core.effects import DiffPatch

# =============================================================================
# WorkspaceRef Prerequisites
# =============================================================================


class TestWorkspaceRefReplayPrerequisites:
    """WorkspaceRef must process effects regardless of context_id."""

    def test_apply_effect_processes_mismatched_context_id(self):
        """WorkspaceRef.apply_effect() must not filter by context_id.

        This is the core prerequisite for cache replay. When replaying
        cached effects, the context_id will differ from the current
        workspace's context_id (different sandbox path, machine, etc.).
        """
        workspace = WorkspaceRef(
            path="/repo",
            base_commit="a" * 40,
            frozen_context_id="workspace:/repo:aaaaaaaa",
        )

        # Effect has DIFFERENT context_id (simulating cache replay)
        effect = WorkspacePatchCaptured(
            context_id="workspace:/other/path:bbbbbbbb",  # Mismatched!
            binding_name="workspace",
            patch=DiffPatch(
                patch="diff --git a/test.py b/test.py\n+hello",
                files_changed=("test.py",),
            ),
            files_changed=("test.py",),
        )

        new_workspace = workspace.apply_effect(effect)

        # Effect MUST be processed despite context_id mismatch
        assert len(new_workspace.pending_patches) == 1
        assert new_workspace.pending_patches[0].files_changed == ("test.py",)

    def test_apply_effect_processes_none_context_id(self):
        """WorkspaceRef.apply_effect() must handle effects with no context_id."""
        workspace = WorkspaceRef(
            path="/repo",
            base_commit="a" * 40,
        )

        # Effect has no context_id at all
        effect = WorkspacePatchCaptured(
            binding_name="workspace",
            patch=DiffPatch(
                patch="diff content",
                files_changed=("file.py",),
            ),
            files_changed=("file.py",),
        )

        new_workspace = workspace.apply_effect(effect)

        assert len(new_workspace.pending_patches) == 1

    def test_apply_effect_still_validates_effect_type(self):
        """WorkspaceRef.apply_effect() should still ignore non-workspace effects."""
        workspace = WorkspaceRef(
            path="/repo",
            base_commit="a" * 40,
        )

        # KeySet is not a workspace effect - should be ignored
        effect = KeySet(
            context_id="kvstore:test",
            key="test_key",
            new_value="test_value",
        )

        new_workspace = workspace.apply_effect(effect)

        # Should return self unchanged (effect type doesn't match)
        assert new_workspace is workspace
        assert len(new_workspace.pending_patches) == 0

    def test_multiple_effects_with_different_context_ids(self):
        """WorkspaceRef should accumulate patches from effects with different context_ids."""
        workspace = WorkspaceRef(
            path="/repo",
            base_commit="a" * 40,
        )

        # Simulate replaying effects from different cached executions
        effects = [
            WorkspacePatchCaptured(
                context_id=f"workspace:/sandbox-{i}:{'a' * 8}",
                binding_name="workspace",
                patch=DiffPatch(
                    patch=f"diff for file{i}.py",
                    files_changed=(f"file{i}.py",),
                ),
                files_changed=(f"file{i}.py",),
            )
            for i in range(3)
        ]

        for effect in effects:
            workspace = workspace.apply_effect(effect)

        assert len(workspace.pending_patches) == 3


# =============================================================================
# KVStoreContext Prerequisites
# =============================================================================


class TestKVStoreContextReplayPrerequisites:
    """KVStoreContext must process effects regardless of context_id."""

    def test_apply_effect_processes_mismatched_context_id_keyset(self):
        """KVStoreContext.apply_effect() must not filter KeySet by context_id."""
        store = KVStoreContext(data={"existing": "value"})
        original_context_id = store.context_id

        # Effect has DIFFERENT context_id (simulating cache replay)
        effect = KeySet(
            context_id="kvstore:different_id",  # Mismatched!
            binding_name="kvstore",
            key="new_key",
            new_value="new_value",
        )

        new_store = store.apply_effect(effect)

        # Effect MUST be processed despite context_id mismatch
        assert new_store.data.get("new_key") == "new_value"
        # Original data preserved
        assert new_store.data.get("existing") == "value"

    def test_apply_effect_processes_mismatched_context_id_keydeleted(self):
        """KVStoreContext.apply_effect() must not filter KeyDeleted by context_id."""
        store = KVStoreContext(data={"key_to_delete": "value", "keep": "this"})

        # Effect has DIFFERENT context_id
        effect = KeyDeleted(
            context_id="kvstore:different_id",  # Mismatched!
            binding_name="kvstore",
            key="key_to_delete",
        )

        new_store = store.apply_effect(effect)

        # Deletion MUST be processed despite context_id mismatch
        assert "key_to_delete" not in new_store.data
        assert new_store.data.get("keep") == "this"

    def test_apply_effect_processes_none_context_id(self):
        """KVStoreContext.apply_effect() must handle effects with no context_id."""
        store = KVStoreContext(data={})

        effect = KeySet(
            binding_name="kvstore",
            key="test",
            new_value="test_value",
        )

        new_store = store.apply_effect(effect)

        assert new_store.data.get("test") == "test_value"

    def test_apply_effect_still_validates_effect_type(self):
        """KVStoreContext.apply_effect() should ignore non-kvstore effects."""
        store = KVStoreContext(data={"unchanged": "value"})

        # WorkspacePatchCaptured is not a kvstore effect
        effect = WorkspacePatchCaptured(
            context_id="workspace:test",
            binding_name="workspace",
            patch=DiffPatch(patch="diff", files_changed=("f.py",)),
            files_changed=("f.py",),
        )

        new_store = store.apply_effect(effect)

        # Should return self unchanged
        assert new_store is store

    def test_multiple_effects_with_different_context_ids(self):
        """KVStoreContext should apply effects from different context_ids."""
        store = KVStoreContext(data={})

        effects = [
            KeySet(
                context_id=f"kvstore:session_{i}",
                binding_name="kvstore",
                key=f"key_{i}",
                new_value=f"value_{i}",
            )
            for i in range(3)
        ]

        for effect in effects:
            store = store.apply_effect(effect)

        assert len(store.data) == 3
        assert store.data["key_0"] == "value_0"
        assert store.data["key_1"] == "value_1"
        assert store.data["key_2"] == "value_2"


# =============================================================================
# SimpleWorkspace Prerequisites
# =============================================================================


class TestSimpleWorkspaceReplayPrerequisites:
    """SimpleWorkspace must process effects regardless of context_id."""

    def test_apply_effect_processes_mismatched_context_id(self):
        """SimpleWorkspace.apply_effect() must not filter by context_id."""
        workspace = SimpleWorkspace(
            path="/workspace",
            _frozen_context_id="simple_workspace:/workspace:abc123",
        )

        # Create a changeset with file deltas
        changeset = FileChangeset(
            deltas=(
                FileDelta(
                    path="test.py",
                    operation="create",
                    encoding="full",
                    content=b"print('hello')",
                    new_content_hash="abc123",
                    new_size_bytes=14,
                ),
            ),
            source_step="test_step",
        )

        # Effect has DIFFERENT context_id (simulating cache replay)
        effect = SimpleWorkspaceChangesetCaptured(
            context_id="simple_workspace:/other/path:xyz789",  # Mismatched!
            binding_name="workspace",
            changeset=changeset,
        )

        new_workspace = workspace.apply_effect(effect)

        # Effect MUST be processed despite context_id mismatch
        assert len(new_workspace.pending_changesets) == 1
        assert new_workspace.pending_changesets[0].files_changed == ("test.py",)

    def test_apply_effect_processes_none_context_id(self):
        """SimpleWorkspace.apply_effect() must handle effects with no context_id."""
        workspace = SimpleWorkspace(path="/workspace")

        changeset = FileChangeset(
            deltas=(
                FileDelta(
                    path="readme.md",
                    operation="create",
                    encoding="full",
                    content=b"# README",
                ),
            ),
        )

        effect = SimpleWorkspaceChangesetCaptured(
            binding_name="workspace",
            changeset=changeset,
        )

        new_workspace = workspace.apply_effect(effect)

        assert len(new_workspace.pending_changesets) == 1

    def test_apply_effect_still_validates_effect_type(self):
        """SimpleWorkspace.apply_effect() should ignore non-workspace effects."""
        workspace = SimpleWorkspace(path="/workspace")

        effect = KeySet(
            context_id="kvstore:test",
            key="test",
            new_value="value",
        )

        new_workspace = workspace.apply_effect(effect)

        # Should return self unchanged
        assert new_workspace is workspace
        assert len(new_workspace.pending_changesets) == 0

    def test_apply_effect_ignores_empty_changeset(self):
        """SimpleWorkspace.apply_effect() should ignore effects with empty changeset."""
        workspace = SimpleWorkspace(path="/workspace")

        effect = SimpleWorkspaceChangesetCaptured(
            context_id="simple_workspace:/other:xyz",
            binding_name="workspace",
            changeset=None,  # No changeset
        )

        new_workspace = workspace.apply_effect(effect)

        # Should return self unchanged (no changeset to apply)
        assert new_workspace is workspace


# =============================================================================
# Cross-Context Contract Consistency
# =============================================================================


class TestCrossContextConsistency:
    """All contexts should follow the same apply_effect() contract."""

    def test_all_contexts_ignore_mismatched_effect_types(self):
        """All contexts should return self for non-matching effect types."""
        # Create instances of each context type
        contexts = [
            WorkspaceRef(path="/repo", base_commit="a" * 40),
            KVStoreContext(data={}),
            SimpleWorkspace(path="/workspace"),
        ]

        # Effect that none of them handle
        effect = SessionCreated(
            session_id="test-session",
            transcript_path="/tmp/transcript.json",
        )

        for ctx in contexts:
            result = ctx.apply_effect(effect)
            assert result is ctx, f"{type(ctx).__name__} should return self for SessionCreated"

    def test_all_contexts_are_immutable(self):
        """apply_effect() should always return a new instance (or self), never mutate."""
        workspace = WorkspaceRef(path="/repo", base_commit="a" * 40)
        original_patches = workspace.pending_patches

        effect = WorkspacePatchCaptured(
            context_id="different",
            binding_name="workspace",
            patch=DiffPatch(patch="diff", files_changed=("f.py",)),
            files_changed=("f.py",),
        )

        new_workspace = workspace.apply_effect(effect)

        # Original should be unchanged
        assert workspace.pending_patches == original_patches
        assert len(workspace.pending_patches) == 0

        # New instance has the change
        assert len(new_workspace.pending_patches) == 1
        assert workspace is not new_workspace
