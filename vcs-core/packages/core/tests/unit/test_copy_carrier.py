"""The portable copy CarrierBackend — the universal reversibility floor.

Backend-level coverage mirroring ``test_clonefile_carrier.py`` but WITHOUT the
macOS gate: a plain recursive copy of the base, copy-vs-base diff (add/modify/
delete + exec bit), child-scope parent-relative diff + commit-into-parent,
discard, push materialization, symlink rejection, and the unmaterialized-parent
/ missing-destination regressions. Runs on every platform (macOS, Linux, WSL) —
the same contract the overlay and clonefile carriers satisfy.
"""

from __future__ import annotations

import pytest
from vcs_core._copy_carrier import CopyCarrierBackend
from vcs_core._substrate_runtime import CarrierBackend


def _make(tmp_path, seed=None):
    base = tmp_path / "base"
    base.mkdir(exist_ok=True)
    for rel, content in (seed or {"a.txt": b"A", "sub/b.txt": b"B"}).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return CopyCarrierBackend(workspace=ws, state_root=tmp_path / "state", base_lowerdir=base)


def test_copy_conforms_and_copies_base_into_ground(tmp_path) -> None:
    backend = _make(tmp_path)
    assert isinstance(backend, CarrierBackend)  # structurally satisfies the carrier protocol
    backend.create_layer("ground", parent_scope_id=None)
    wp = backend.working_path("ground")
    assert (wp / "a.txt").read_bytes() == b"A"
    assert (wp / "sub" / "b.txt").read_bytes() == b"B"
    assert backend.has_layer("ground")
    assert backend.diff_layer("ground") == []  # a fresh copy equals its base


def test_copy_diff_detects_add_modify_delete_and_exec_bit(tmp_path) -> None:
    backend = _make(tmp_path)
    backend.create_layer("ground", parent_scope_id=None)
    backend.write_file("ground", "c.txt", b"C")  # add
    backend.write_file("ground", "a.txt", b"A2", mode=0o100755)  # modify + exec bit
    backend.delete_file("ground", "sub/b.txt")  # delete
    diff = {path: (content, mode) for path, content, mode in backend.diff_layer("ground")}
    assert diff["c.txt"] == (b"C", 0o100644)
    assert diff["a.txt"] == (b"A2", 0o100755)
    assert diff["sub/b.txt"] == (None, 0)


def test_copy_child_scope_diff_is_parent_relative_and_commits(tmp_path) -> None:
    backend = _make(tmp_path)
    backend.create_layer("ground", parent_scope_id=None)
    backend.create_layer("task", parent_scope_id="ground")
    backend.write_file("task", "a.txt", b"EDITED")  # change vs ground (== base)
    assert backend.diff_layer("task") == [("a.txt", b"EDITED", 0o100644)]  # only the child's delta
    backend.commit_layer("task", into_scope_id="ground")
    assert backend.read_file("ground", "a.txt") == b"EDITED"  # applied into ground
    assert not backend.has_layer("task")  # child discarded on commit


def test_copy_discard_drops_layer_leaves_ground(tmp_path) -> None:
    backend = _make(tmp_path)
    backend.create_layer("ground", parent_scope_id=None)
    backend.create_layer("task", parent_scope_id="ground")
    assert backend.has_layer("task")
    backend.discard_layer("task")
    assert not backend.has_layer("task")
    assert backend.has_layer("ground")  # ground untouched by a child discard


def test_copy_push_materializes_diff_to_real_workspace(tmp_path) -> None:
    backend = _make(tmp_path)
    backend.create_layer("ground", parent_scope_id=None)
    backend.write_file("ground", "out.txt", b"OUT")
    backend.write_file("ground", "a.txt", b"A-NEW")
    backend.push_layer("ground")
    ws = tmp_path / "ws"
    assert (ws / "out.txt").read_bytes() == b"OUT"  # the delta materialized to the real workspace
    assert (ws / "a.txt").read_bytes() == b"A-NEW"
    assert backend.diff_layer("ground") == []  # ground reset to base after push


def test_copy_diff_rejects_symlinks(tmp_path) -> None:
    """Symlinks are unsupported, consistent with the overlay and clonefile carriers:
    a symlink in a scope surfaces as UnsupportedOverlayEntryError at diff time,
    never silently skipped or mis-captured."""
    from vcs_core._errors import UnsupportedOverlayEntryError

    backend = _make(tmp_path)
    backend.create_layer("ground", parent_scope_id=None)
    (backend.working_path("ground") / "link").symlink_to("a.txt")
    with pytest.raises(UnsupportedOverlayEntryError):
        backend.diff_layer("ground")


def test_copy_child_of_unmaterialized_parent_copies_the_base(tmp_path) -> None:
    """A child whose parent never materialized a copy of its own (ground, in
    normal operation) copies the BASE snapshot — "a child scope copies its
    parent's working tree" holds transitively, so a body sees the pre-existing
    workspace files rather than an empty tree."""
    backend = _make(tmp_path)
    # No create_layer("ground") — the parent works directly on the base.
    backend.create_layer("run", parent_scope_id="ground")
    wp = backend.working_path("run")
    assert (wp / "a.txt").read_bytes() == b"A"
    assert (wp / "sub" / "b.txt").read_bytes() == b"B"
    assert backend.diff_layer("run") == []  # the fresh child equals its effective base


def test_copy_commit_into_missing_destination_materializes_full_tree(tmp_path) -> None:
    """Committing a child into a destination with no layer materializes the
    destination as a FULL tree first — never a delta-only dir a later fork would
    mistake for the whole workspace."""
    backend = _make(tmp_path)
    backend.create_layer("run", parent_scope_id="ground")
    backend.write_file("run", "new.txt", b"NEW")
    backend.commit_layer("run", into_scope_id="ground")
    ground = backend.working_path("ground")
    assert (ground / "new.txt").read_bytes() == b"NEW"  # the delta
    assert (ground / "a.txt").read_bytes() == b"A"  # ... and the full base tree
    # A subsequent fork composes on the merged state.
    backend.create_layer("run2", parent_scope_id="ground")
    wp2 = backend.working_path("run2")
    assert (wp2 / "new.txt").read_bytes() == b"NEW"
    assert (wp2 / "a.txt").read_bytes() == b"A"


def test_copy_layers_are_independent_of_the_base(tmp_path) -> None:
    """A copy layer is an independent tree: editing it never mutates the base
    (the reversibility guarantee that lets discard just drop the layer)."""
    base = tmp_path / "base"
    backend = _make(tmp_path)
    backend.create_layer("ground", parent_scope_id=None)
    backend.write_file("ground", "a.txt", b"MUTATED")
    assert (base / "a.txt").read_bytes() == b"A"  # base untouched by the layer edit
