"""Tests for SimpleWorkspace content_hash, content_equals, and state_hash.

This module tests the content-addressable identity features of SimpleWorkspace,
following the specification in DESIGN-simpleworkspace-content-hash.md.
"""

from __future__ import annotations

import warnings
from datetime import datetime

import pytest
from shepherd_contexts.simple_workspace import (
    FileChangeset,
    FileDelta,
    FileEntry,
    FileManifest,
    SimpleWorkspace,
)
from shepherd_contexts.simple_workspace.effects import SimpleWorkspaceChangesetCaptured


@pytest.fixture
def sample_manifest() -> FileManifest:
    """A minimal manifest for testing."""
    entries = (
        FileEntry(
            path="file.txt",
            size_bytes=100,
            mtime_ns=123456789,
            content_hash="a" * 64,
        ),
    )
    return FileManifest(entries=entries, created_at=datetime.now())


@pytest.fixture
def sample_changeset() -> FileChangeset:
    """A minimal changeset for testing."""
    delta = FileDelta(
        path="new_file.txt",
        operation="create",
        new_content_hash="b" * 64,
        new_size_bytes=50,
    )
    return FileChangeset(deltas=(delta,))


class TestContentHash:
    """Tests for SimpleWorkspace.content_hash."""

    def test_same_content_same_hash(self, sample_manifest):
        """Identical workspaces produce identical hashes."""
        ws1 = SimpleWorkspace(path="/path/a", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(path="/path/b", base_manifest=sample_manifest)
        assert ws1.content_hash == ws2.content_hash

    def test_different_path_same_hash(self, sample_manifest):
        """Path doesn't affect content_hash."""
        ws1 = SimpleWorkspace(path="/sandbox-1/", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(path="/sandbox-2/", base_manifest=sample_manifest)
        assert ws1.content_hash == ws2.content_hash

    def test_changeset_changes_hash(self, sample_manifest, sample_changeset):
        """Adding a changeset changes the hash."""
        ws1 = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            pending_changesets=(sample_changeset,),
        )
        assert ws1.content_hash != ws2.content_hash

    def test_changeset_order_matters(self, sample_manifest):
        """Different changeset order produces different hash."""
        cs_a = FileChangeset(deltas=(FileDelta(path="a.txt", operation="create", new_content_hash="a" * 64),))
        cs_b = FileChangeset(deltas=(FileDelta(path="b.txt", operation="create", new_content_hash="b" * 64),))

        ws1 = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            pending_changesets=(cs_a, cs_b),
        )
        ws2 = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            pending_changesets=(cs_b, cs_a),
        )
        assert ws1.content_hash != ws2.content_hash

    def test_empty_changeset_ignored(self, sample_manifest):
        """Empty changesets don't change hash."""
        ws1 = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        empty = FileChangeset(deltas=())
        ws2 = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            pending_changesets=(empty,),
        )
        assert ws1.content_hash == ws2.content_hash

    def test_none_manifest_equals_empty(self):
        """None manifest equals empty manifest."""
        ws1 = SimpleWorkspace(path="/repo", base_manifest=None)
        ws2 = SimpleWorkspace(
            path="/repo",
            base_manifest=FileManifest(entries=(), created_at=datetime.now()),
        )
        assert ws1.content_hash == ws2.content_hash

    def test_hash_length(self, sample_manifest):
        """Hash is 12 hex characters."""
        ws = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        assert len(ws.content_hash) == 12
        assert all(c in "0123456789abcdef" for c in ws.content_hash)

    def test_hash_deterministic(self, sample_manifest):
        """Same workspace produces same hash across calls."""
        ws = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        assert ws.content_hash == ws.content_hash

    def test_golden_empty_workspace(self):
        """Golden test for empty workspace hash stability."""
        ws = SimpleWorkspace(path="/repo", base_manifest=None)
        # sha256(b"empty").hexdigest()[:12] = "2e1cfa82b035"
        # Verified via capability spike 2026-01-24
        expected = "2e1cfa82b035"
        assert ws.content_hash == expected

    def test_content_hash_correct_after_model_copy(self, sample_manifest, sample_changeset):
        """content_hash recomputes correctly after model_copy().

        This verifies that @computed_field + @property works correctly
        with Pydantic's model_copy(), unlike @model_validator which does
        NOT trigger on model_copy().
        """
        ws1 = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        original_hash = ws1.content_hash

        # Add changeset via model_copy
        ws2 = ws1.model_copy(update={"pending_changesets": (sample_changeset,)})

        # Hash should change (new changeset added)
        assert ws2.content_hash != original_hash

        # Original unchanged (immutable)
        assert ws1.content_hash == original_hash


class TestContentEquals:
    """Tests for content_equals() method."""

    def test_content_equals_same_content(self, sample_manifest):
        """content_equals returns True for same content."""
        ws1 = SimpleWorkspace(path="/path/a", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(path="/path/b", base_manifest=sample_manifest)
        assert ws1.content_equals(ws2)

    def test_content_equals_different_content(self, sample_manifest, sample_changeset):
        """content_equals returns False for different content."""
        ws1 = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            pending_changesets=(sample_changeset,),
        )
        assert not ws1.content_equals(ws2)


class TestResourceIdentity:
    """Tests for resource identity (__eq__ and __hash__)."""

    def test_different_path_not_equal(self, sample_manifest):
        """Workspaces at different paths are NOT equal."""
        ws1 = SimpleWorkspace(path="/path/a", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(path="/path/b", base_manifest=sample_manifest)
        assert ws1.content_hash == ws2.content_hash  # Content equal
        assert ws1 != ws2  # Resource NOT equal

    def test_dict_preserves_both_resources(self, sample_manifest):
        """Dict keyed by SimpleWorkspace preserves both resources."""
        ws1 = SimpleWorkspace(path="/sandbox-1/", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(path="/sandbox-2/", base_manifest=sample_manifest)

        modifications: dict[SimpleWorkspace, list[str]] = {}
        modifications[ws1] = ["file_a.txt"]
        modifications[ws2] = ["file_b.txt"]

        assert len(modifications) == 2
        assert modifications[ws1] == ["file_a.txt"]
        assert modifications[ws2] == ["file_b.txt"]


class TestStateHash:
    """Tests for state_hash() method."""

    def test_same_content_different_path_same_hash(self, sample_manifest):
        """Same content at different paths produces same state_hash."""
        ws1 = SimpleWorkspace(path="/sandbox-1/", base_manifest=sample_manifest)
        ws2 = SimpleWorkspace(path="/sandbox-2/", base_manifest=sample_manifest)
        assert ws1.state_hash() == ws2.state_hash()

    def test_different_capabilities_different_hash(self, sample_manifest):
        """Different capabilities produce different state_hash."""
        ws_ro = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            capabilities=frozenset({"read"}),
        )
        ws_rw = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            capabilities=frozenset({"read", "write"}),
        )
        assert ws_ro.state_hash() != ws_rw.state_hash()

    def test_content_hash_unchanged_by_capabilities(self, sample_manifest):
        """Capabilities don't affect content_hash (only state_hash)."""
        ws_ro = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            capabilities=frozenset({"read"}),
        )
        ws_rw = SimpleWorkspace(
            path="/repo",
            base_manifest=sample_manifest,
            capabilities=frozenset({"read", "write"}),
        )
        assert ws_ro.content_hash == ws_rw.content_hash
        assert ws_ro.state_hash() != ws_rw.state_hash()

    def test_state_hash_length(self, sample_manifest):
        """state_hash is 16 hex characters."""
        ws = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        assert len(ws.state_hash()) == 16
        assert all(c in "0123456789abcdef" for c in ws.state_hash())


class TestManifestAnchor:
    """Tests for manifest anchor computation edge cases."""

    def test_missing_content_hash_uses_fallback(self):
        """Missing content_hash uses size fallback with warning."""
        entry = FileEntry(
            path="file.txt",
            size_bytes=100,
            mtime_ns=123,
            content_hash=None,  # Missing!
        )
        manifest = FileManifest(entries=(entry,), created_at=datetime.now())
        ws = SimpleWorkspace(path="/repo", base_manifest=manifest)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _ = ws.content_hash  # Trigger computation

            assert len(w) == 1
            assert "missing content_hash" in str(w[0].message)
            assert "size fallback" in str(w[0].message)

    def test_manifest_order_independent(self):
        """Manifest entries are sorted, so order doesn't affect hash."""
        entry_a = FileEntry(path="a.txt", size_bytes=10, mtime_ns=1, content_hash="a" * 64)
        entry_b = FileEntry(path="b.txt", size_bytes=20, mtime_ns=2, content_hash="b" * 64)

        manifest1 = FileManifest(entries=(entry_a, entry_b), created_at=datetime.now())
        manifest2 = FileManifest(entries=(entry_b, entry_a), created_at=datetime.now())

        ws1 = SimpleWorkspace(path="/repo", base_manifest=manifest1)
        ws2 = SimpleWorkspace(path="/repo", base_manifest=manifest2)

        assert ws1.content_hash == ws2.content_hash

    def test_different_mtime_same_hash(self):
        """Mtime doesn't affect content_hash (not content)."""
        entry1 = FileEntry(path="f.txt", size_bytes=10, mtime_ns=111, content_hash="x" * 64)
        entry2 = FileEntry(path="f.txt", size_bytes=10, mtime_ns=999, content_hash="x" * 64)

        manifest1 = FileManifest(entries=(entry1,), created_at=datetime.now())
        manifest2 = FileManifest(entries=(entry2,), created_at=datetime.now())

        ws1 = SimpleWorkspace(path="/repo", base_manifest=manifest1)
        ws2 = SimpleWorkspace(path="/repo", base_manifest=manifest2)

        assert ws1.content_hash == ws2.content_hash

    def test_different_mode_different_hash(self):
        """Different file mode produces different hash."""
        entry1 = FileEntry(path="f.txt", size_bytes=10, mtime_ns=1, mode=0o644, content_hash="x" * 64)
        entry2 = FileEntry(path="f.txt", size_bytes=10, mtime_ns=1, mode=0o755, content_hash="x" * 64)

        manifest1 = FileManifest(entries=(entry1,), created_at=datetime.now())
        manifest2 = FileManifest(entries=(entry2,), created_at=datetime.now())

        ws1 = SimpleWorkspace(path="/repo", base_manifest=manifest1)
        ws2 = SimpleWorkspace(path="/repo", base_manifest=manifest2)

        assert ws1.content_hash != ws2.content_hash


class TestEffectReplay:
    """Tests for effect replay preserving hashes."""

    def test_effect_replay_preserves_content_hash(self, sample_manifest):
        """Replaying effects should produce identical content_hash."""
        # Original workspace
        ws_original = SimpleWorkspace(path="/sandbox-1/", base_manifest=sample_manifest)

        # Create a changeset effect
        changeset = FileChangeset(
            deltas=(
                FileDelta(
                    path="new_file.txt",
                    operation="create",
                    new_content_hash="c" * 64,
                    new_size_bytes=42,
                ),
            )
        )
        effect = SimpleWorkspaceChangesetCaptured(
            changeset=changeset,
            files_changed=("new_file.txt",),
        )

        # Apply effect to original
        ws_after = ws_original.apply_effect(effect)
        original_content_hash = ws_after.content_hash
        original_state_hash = ws_after.state_hash()

        # Simulate resume: fresh workspace at DIFFERENT path, replay effect
        ws_fresh = SimpleWorkspace(path="/sandbox-2/", base_manifest=sample_manifest)
        ws_replayed = ws_fresh.apply_effect(effect)

        # Hashes should match despite different paths
        assert ws_replayed.content_hash == original_content_hash
        assert ws_replayed.state_hash() == original_state_hash

        # But resource identity differs
        assert ws_after != ws_replayed  # Different paths

    def test_effect_replay_verifies_changeset_appended(self, sample_manifest):
        """Verify apply_effect correctly appends changeset."""
        ws = SimpleWorkspace(path="/repo", base_manifest=sample_manifest)
        assert len(ws.pending_changesets) == 0

        changeset = FileChangeset(deltas=(FileDelta(path="file.txt", operation="modify", new_content_hash="d" * 64),))
        effect = SimpleWorkspaceChangesetCaptured(
            changeset=changeset,
            files_changed=("file.txt",),
        )

        ws_after = ws.apply_effect(effect)

        # Verify changeset was appended
        assert len(ws_after.pending_changesets) == 1
        assert ws_after.pending_changesets[0] is changeset


class TestFileManifestIdentity:
    """Regression pins for the C2 fix (tz-aware, comparison-exempt created_at)."""

    def test_file_manifest_equality_ignores_created_at(self):
        """Two manifests of identical file state are equal despite distinct timestamps."""
        entries = (FileEntry(path="f.txt", size_bytes=10, mtime_ns=1, content_hash="x" * 64),)
        m1 = FileManifest(entries=entries)  # default_factory timestamps differ per-instance
        m2 = FileManifest(entries=entries)
        assert m1 == m2
        assert hash(m1) == hash(m2)

    def test_file_manifest_created_at_is_tz_aware(self):
        m = FileManifest(entries=())
        assert m.created_at.tzinfo is not None
