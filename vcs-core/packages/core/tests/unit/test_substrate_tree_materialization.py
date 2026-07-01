"""Tranche 3 tests: materialization reads from the substrate workspace tree.

Tree-backed substrate revisions embed the workspace bytes as a Git tree at
``workspace/``. After Tranche 2 wired the capture/scan/adopt/overlay paths to
produce tree-backed revisions, Tranche 3 has the filesystem substrate prefer
that tree as the byte source for materialization, falling back to the scalar
coord workspace for digest-only revisions or when no v2 ground world exists.

These tests cover:

- the low-level reader (``read_substrate_workspace_file``);
- the VcsCore byte-source method picking tree-backed when the ground world's
  workspace head is tree-backed;
- the scalar fallback for digest-only revisions and pre-v2 installs;
- end-to-end materialization succeeds when scalar bytes are unavailable but
  the substrate tree is, proving the materializer's dependency on scalar
  GROUND_REF is compensable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pygit2
import pytest
from vcs_core._substrate_tree_read import read_substrate_workspace_file
from vcs_core._world_storage_installation import (
    DEFAULT_WORKSPACE_STORE_ID,
    open_or_init_default_world_storage,
)
from vcs_core._world_substrate_adapters import (
    WorkspaceSubstrateAdapter,
    workspace_state_revision_payload,
)
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


# --- read_substrate_workspace_file ---


def test_read_substrate_workspace_file_returns_bytes_and_mode(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    Store(str(repo_path))
    manager = open_or_init_default_world_storage(repo_path)
    workspace = WorkspaceSubstrateAdapter(manager)
    substrate = manager.store(DEFAULT_WORKSPACE_STORE_ID)
    # Author a tree in the substrate (no alternates needed for this path).
    builder = substrate.repo.TreeBuilder()
    blob_oid = substrate.repo.create_blob(b"alpha\n")
    builder.insert("a.txt", blob_oid, pygit2.GIT_FILEMODE_BLOB)
    tree_oid = str(builder.write())
    payload = workspace_state_revision_payload(
        ({"path": "a.txt", "state": "present", "mode": 0o100644, "content_digest": _digest(b"alpha\n")},),
        byte_authority="tree-backed",
    )
    bundle = workspace.create_scan_candidate(
        operation_id="op-tree", payload=payload, parents=(), workspace_tree_oid=tree_oid
    )

    result = read_substrate_workspace_file(substrate.repo, bundle.candidate.head, "a.txt")
    assert result is not None
    content, mode = result
    assert content == b"alpha\n"
    assert mode == pygit2.GIT_FILEMODE_BLOB


def test_read_substrate_workspace_file_returns_none_when_workspace_tree_unreachable(
    tmp_path,
) -> None:
    """If the substrate cannot resolve the ``workspace/`` tree (e.g. alternates
    removed), the reader must return ``None``, not raise ``KeyError``.

    The substrate revision's root tree always lives in the substrate's own ODB,
    but the embedded ``workspace/`` subtree may live in the scalar coord store
    and be reached only through ``objects/info/alternates``. Pulling the
    alternates link out must degrade to a clean None so the materializer's
    scalar fallback kicks in cleanly.
    """
    repo_path = tmp_path / ".vcscore"
    store = Store(str(repo_path))
    manager = open_or_init_default_world_storage(repo_path)
    workspace = WorkspaceSubstrateAdapter(manager)
    substrate = manager.store(DEFAULT_WORKSPACE_STORE_ID)
    # Author the workspace tree in the SCALAR coord store — the substrate's
    # alternates points at ``repo_path``, so the tree is visible to substrate
    # only through alternates.
    scalar_repo = store._repo
    scalar_blob = scalar_repo.create_blob(b"alpha\n")
    scalar_builder = scalar_repo.TreeBuilder()
    scalar_builder.insert("a.txt", scalar_blob, pygit2.GIT_FILEMODE_BLOB)
    scalar_tree_oid = str(scalar_builder.write())
    payload = workspace_state_revision_payload(
        ({"path": "a.txt", "state": "present", "mode": 0o100644, "content_digest": _digest(b"alpha\n")},),
        byte_authority="tree-backed",
    )
    bundle = workspace.create_scan_candidate(
        operation_id="op-alt", payload=payload, parents=(), workspace_tree_oid=scalar_tree_oid
    )
    # Sanity check: alternates makes the read work.
    served = read_substrate_workspace_file(substrate.repo, bundle.candidate.head, "a.txt")
    assert served == (b"alpha\n", pygit2.GIT_FILEMODE_BLOB)

    # Now sever the alternates link and open a FRESH pygit2 handle on the
    # substrate so libgit2 does not retain a cached alternates view.
    alternates_path = (
        Path(substrate.repo.path) / "objects" / "info" / "alternates"
    )
    assert alternates_path.exists()
    alternates_path.unlink()
    fresh_repo = pygit2.Repository(substrate.repo.path)

    # The workspace tree is no longer reachable from the substrate. The reader
    # must return None gracefully rather than letting a KeyError escape.
    assert read_substrate_workspace_file(fresh_repo, bundle.candidate.head, "a.txt") is None


def test_read_substrate_workspace_file_returns_none_for_digest_only_revision(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    Store(str(repo_path))
    manager = open_or_init_default_world_storage(repo_path)
    workspace = WorkspaceSubstrateAdapter(manager)
    substrate = manager.store(DEFAULT_WORKSPACE_STORE_ID)
    payload = workspace_state_revision_payload(
        ({"path": "a.txt", "state": "present", "mode": 0o100644, "content_digest": _digest(b"a")},)
    )
    bundle = workspace.create_scan_candidate(operation_id="op-digest", payload=payload, parents=())

    assert read_substrate_workspace_file(substrate.repo, bundle.candidate.head, "a.txt") is None


def test_read_substrate_workspace_file_returns_none_for_unknown_path(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    Store(str(repo_path))
    manager = open_or_init_default_world_storage(repo_path)
    workspace = WorkspaceSubstrateAdapter(manager)
    substrate = manager.store(DEFAULT_WORKSPACE_STORE_ID)
    builder = substrate.repo.TreeBuilder()
    blob_oid = substrate.repo.create_blob(b"alpha\n")
    builder.insert("a.txt", blob_oid, pygit2.GIT_FILEMODE_BLOB)
    tree_oid = str(builder.write())
    payload = workspace_state_revision_payload(
        ({"path": "a.txt", "state": "present", "mode": 0o100644, "content_digest": _digest(b"alpha\n")},),
        byte_authority="tree-backed",
    )
    bundle = workspace.create_scan_candidate(
        operation_id="op-missing", payload=payload, parents=(), workspace_tree_oid=tree_oid
    )

    assert read_substrate_workspace_file(substrate.repo, bundle.candidate.head, "missing.txt") is None


def test_read_substrate_workspace_file_handles_nested_paths(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    Store(str(repo_path))
    manager = open_or_init_default_world_storage(repo_path)
    workspace = WorkspaceSubstrateAdapter(manager)
    substrate = manager.store(DEFAULT_WORKSPACE_STORE_ID)
    # Build src/main.py and an executable script.
    sub_blob = substrate.repo.create_blob(b"print('hi')\n")
    sub_builder = substrate.repo.TreeBuilder()
    sub_builder.insert("main.py", sub_blob, pygit2.GIT_FILEMODE_BLOB)
    src_tree_oid = sub_builder.write()
    exec_blob = substrate.repo.create_blob(b"#!/bin/sh\necho hi\n")
    root_builder = substrate.repo.TreeBuilder()
    root_builder.insert("src", src_tree_oid, pygit2.GIT_FILEMODE_TREE)
    root_builder.insert("run.sh", exec_blob, pygit2.GIT_FILEMODE_BLOB_EXECUTABLE)
    tree_oid = str(root_builder.write())
    payload = workspace_state_revision_payload(
        (
            {
                "path": "run.sh",
                "state": "present",
                "mode": 0o100755,
                "content_digest": _digest(b"#!/bin/sh\necho hi\n"),
            },
            {
                "path": "src/main.py",
                "state": "present",
                "mode": 0o100644,
                "content_digest": _digest(b"print('hi')\n"),
            },
        ),
        byte_authority="tree-backed",
    )
    bundle = workspace.create_scan_candidate(
        operation_id="op-nested", payload=payload, parents=(), workspace_tree_oid=tree_oid
    )

    sub_result = read_substrate_workspace_file(substrate.repo, bundle.candidate.head, "src/main.py")
    assert sub_result is not None
    assert sub_result[0] == b"print('hi')\n"
    assert sub_result[1] == pygit2.GIT_FILEMODE_BLOB

    exec_result = read_substrate_workspace_file(substrate.repo, bundle.candidate.head, "run.sh")
    assert exec_result is not None
    assert exec_result[0] == b"#!/bin/sh\necho hi\n"
    assert exec_result[1] == pygit2.GIT_FILEMODE_BLOB_EXECUTABLE


# --- VcsCore byte-source: tree-backed vs. fallback semantics ---


def test_vcscore_byte_source_returns_none_when_world_storage_uninitialized(tmp_path) -> None:
    """A VcsCore instance without any v2 ground world returns ``None`` so the
    filesystem substrate falls back to scalar reads. This pins the
    pre-Tranche-3 behavior under the new code path."""
    repo_path = tmp_path / ".vcscore"
    store = Store(str(repo_path))
    store.create_root_commit()
    mg = VcsCore(str(tmp_path), store=store)

    assert mg._read_v2_workspace_file_for_materialization("anything") is None


def test_vcscore_byte_source_returns_none_for_digest_only_ground_world(tmp_path) -> None:
    """When the ground world's workspace selection is digest-only, the byte
    source returns ``None`` to keep materialization on the scalar path until
    a tree-backed revision lands."""
    repo_path = tmp_path / ".vcscore"
    store = Store(str(repo_path))
    store.create_root_commit()
    mg = VcsCore(str(tmp_path), store=store)
    # Force lazy world-storage init without writing any selected world; the
    # byte source still has nothing to point at.
    mg._world_storage()
    assert mg._read_v2_workspace_file_for_materialization("anything") is None


# --- end-to-end: substrate tree wins over scalar when both differ ---


def test_filesystem_substrate_prefers_byte_source_over_scalar_ground(tmp_path) -> None:
    """When ``ground_workspace_byte_source`` returns content for a path, the
    filesystem substrate materializes that content - even if the scalar
    workspace happens to hold different bytes for the same path. This is the
    load-bearing assertion for Tranche 3: substrate tree is preferred.

    The test substitutes the byte source on a built VcsCore via
    ``dataclasses.replace`` and re-binds the filesystem substrate so the
    assertion isolates the new code path without depending on a full v2
    publication. The production wiring is exercised by the regression suite
    around capture-shadow materialization.
    """
    from dataclasses import replace

    from vcs_core._substrate_runtime import RuntimeBoundSubstrate

    from tests.support.builders import make_marker_filesystem_vcscore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        # Emit a scalar workspace change via the standard substrate API.
        mg.exec("filesystem", "write", scope=mg.ground, path="shared.txt", content=b"from-scalar\n")
        # Replace the byte source so it returns substrate content for shared.txt.
        new_binding = replace(
            mg._runtime,
            ground_workspace_byte_source=lambda path: (b"from-substrate\n", 0o100644)
            if path == "shared.txt"
            else None,
        )
        mg._runtime = new_binding
        for sub in mg.lifecycle_substrates:
            if isinstance(sub, RuntimeBoundSubstrate):
                sub.bind_runtime(new_binding)

        mg.push()

        materialized = (workspace / "shared.txt").read_bytes()
        assert materialized == b"from-substrate\n", (
            "filesystem substrate must prefer ground_workspace_byte_source over scalar GROUND_REF"
        )
    finally:
        mg.deactivate()


# --- diagnostic warning when tree-backed ground has a substrate-tree miss ---
#
# Tranche 1's manifest/tree correspondence validator should make this state
# unreachable in production: a tree-backed revision must contain every present
# manifest entry as a tree blob. The warning is observability, not enforcement.
# It catches drift if a future code path produces an inconsistent tree-backed
# manifest, without failing the materialization (scalar still has the bytes
# under alternates).


def _substitute_runtime(mg, **overrides) -> None:
    """Replace VcsCore._runtime in-place and re-bind filesystem substrates.

    Test-only helper for exercising materialization under different runtime
    bindings without rebuilding the full VcsCore.
    """
    from dataclasses import replace

    from vcs_core._substrate_runtime import RuntimeBoundSubstrate

    new_binding = replace(mg._runtime, **overrides)
    mg._runtime = new_binding
    for sub in mg.lifecycle_substrates:
        if isinstance(sub, RuntimeBoundSubstrate):
            sub.bind_runtime(new_binding)


def test_materialization_warns_on_tree_backed_substrate_miss(
    tmp_path, caplog, monkeypatch
) -> None:
    """Tree-backed ground + byte-source returns None for a diff path -> WARN.

    Tranche 1's validator enforces manifest/tree correspondence at write time,
    so reaching the scalar fallback under a tree-backed selection signals
    drift. The warning is the only signal; the materialization still succeeds
    via scalar (alternates make the bytes byte-equivalent on both surfaces).
    """
    import logging

    from vcs_core.substrates import STRICT_TREE_BACKED_MATERIALIZATION_ENV

    from tests.support.builders import make_marker_filesystem_vcscore

    # Pin default mode for this test: it specifically asserts the warning
    # path. Strict mode promotes the warning into a raise (covered separately
    # in ``test_strict_mode_raises_on_tree_backed_substrate_miss``).
    monkeypatch.delenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, raising=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="missing.txt", content=b"scalar\n")
        # Tree-backed ground, but the byte source returns None for this path -
        # simulating a manifest/tree inconsistency the validator would normally
        # have caught. The materializer falls back to scalar AND warns.
        _substitute_runtime(
            mg,
            ground_workspace_byte_source=lambda _path: None,
            ground_workspace_is_tree_backed=lambda: True,
        )
        with caplog.at_level(logging.WARNING, logger="vcs_core.substrates"):
            mg.push()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "no substrate-tree entry for diff path 'missing.txt'" in r.getMessage()
            for r in warnings
        ), f"expected substrate-tree-miss warning for missing.txt, got: {[r.getMessage() for r in warnings]}"
        # The materialization still succeeds via scalar fallback.
        assert (workspace / "missing.txt").read_bytes() == b"scalar\n"
    finally:
        mg.deactivate()


def test_materialization_does_not_warn_for_digest_only_ground(tmp_path, caplog) -> None:
    """Digest-only ground (or pre-v2 install) -> no warning on scalar fallback.

    Falling back to scalar is the expected, normal behavior for digest-only
    revisions and pre-v2 installs. The warning would be noise here, which
    would defeat the signal value for the real tree-backed-drift case.
    """
    import logging

    from tests.support.builders import make_marker_filesystem_vcscore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="normal.txt", content=b"scalar\n")
        # Default runtime: no v2 byte source, ground reports not-tree-backed.
        # This is the pre-v2-install state in a fresh VcsCore.
        with caplog.at_level(logging.WARNING, logger="vcs_core.substrates"):
            mg.push()

        substrate_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "substrate-tree entry for diff path" in r.getMessage()
        ]
        assert not substrate_warnings, (
            f"digest-only/no-v2 fallback must not emit substrate-tree warnings, "
            f"got: {[r.getMessage() for r in substrate_warnings]}"
        )
        assert (workspace / "normal.txt").read_bytes() == b"scalar\n"
    finally:
        mg.deactivate()


def test_ground_workspace_is_tree_backed_returns_false_without_world_storage(tmp_path) -> None:
    """Pre-v2 VcsCore: the tree-backed query returns False, matching the
    byte-source's ``None`` return."""
    repo_path = tmp_path / ".vcscore"
    store = Store(str(repo_path))
    store.create_root_commit()
    mg = VcsCore(str(tmp_path), store=store)
    assert mg._ground_workspace_is_tree_backed() is False
    assert mg._read_v2_workspace_file_for_materialization("anything") is None


# --- strict-mode gate (Phase E pre-removal) ---
#
# The strict-mode env var promotes the substrate-tree-miss warning into a
# raise. CI runs under strict mode to catch drift before scalar removal.


def test_strict_mode_helper_reads_env_var(monkeypatch) -> None:
    """The strict-mode helper reads the env var and accepts only ``1``/``true``
    (case-insensitive) as truthy."""
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        _strict_tree_backed_materialization_enabled,
    )

    monkeypatch.delenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, raising=False)
    assert _strict_tree_backed_materialization_enabled() is False

    for truthy in ("1", "true", "TRUE", "True", "  true  "):
        monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, truthy)
        assert _strict_tree_backed_materialization_enabled() is True, (
            f"expected {truthy!r} to enable strict mode"
        )

    for falsey in ("0", "false", "no", "off", "", "yes", "anything"):
        monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, falsey)
        assert _strict_tree_backed_materialization_enabled() is False, (
            f"expected {falsey!r} to leave strict mode disabled"
        )


def test_strict_mode_raises_on_tree_backed_substrate_miss(tmp_path, monkeypatch) -> None:
    """With strict mode enabled, a tree-backed ground + byte-source miss raises
    ``InvalidRepositoryStateError`` instead of falling back silently.

    This is the Phase E pre-removal gate: CI runs under strict mode so any
    drift that would have produced a warning becomes a hard failure.
    """
    from vcs_core._errors import InvalidRepositoryStateError
    from vcs_core.substrates import STRICT_TREE_BACKED_MATERIALIZATION_ENV

    from tests.support.builders import make_marker_filesystem_vcscore

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="missing.txt", content=b"scalar\n")
        _substitute_runtime(
            mg,
            ground_workspace_byte_source=lambda _path: None,
            ground_workspace_is_tree_backed=lambda: True,
        )
        with pytest.raises(
            InvalidRepositoryStateError,
            match=r"tree-backed ground world has no substrate-tree entry "
            r"for diff path 'missing.txt'",
        ):
            mg.push()
    finally:
        mg.deactivate()


def test_strict_mode_silent_when_byte_source_serves(tmp_path, monkeypatch, caplog) -> None:
    """With strict mode enabled and the byte source serving the path, the
    materialization succeeds cleanly with no warning and no raise."""
    import logging

    from vcs_core.substrates import STRICT_TREE_BACKED_MATERIALIZATION_ENV

    from tests.support.builders import make_marker_filesystem_vcscore

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "1")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="served.txt", content=b"from-substrate\n")
        # Byte source serves the path; strict mode should not trigger.
        _substitute_runtime(
            mg,
            ground_workspace_byte_source=lambda path: (b"from-substrate\n", 0o100644)
            if path == "served.txt"
            else None,
            ground_workspace_is_tree_backed=lambda: True,
        )
        with caplog.at_level(logging.WARNING, logger="vcs_core.substrates"):
            mg.push()
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "substrate-tree entry for diff path" in r.getMessage()
        ]
        assert not warnings, f"strict mode + served byte source must not warn, got: {warnings}"
        assert (workspace / "served.txt").read_bytes() == b"from-substrate\n"
    finally:
        mg.deactivate()


def test_strict_mode_does_not_raise_for_digest_only_ground(tmp_path, monkeypatch) -> None:
    """Strict mode only constrains tree-backed grounds. A digest-only or
    pre-v2 ground falling back to scalar is the expected, normal behavior and
    must not raise even under strict mode."""
    from vcs_core.substrates import STRICT_TREE_BACKED_MATERIALIZATION_ENV

    from tests.support.builders import make_marker_filesystem_vcscore

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="normal.txt", content=b"scalar\n")
        # Default runtime: not tree-backed. Materialization must succeed cleanly.
        mg.push()
        assert (workspace / "normal.txt").read_bytes() == b"scalar\n"
    finally:
        mg.deactivate()


# --- scalar-fallback counter (Phase E "fallback never fires" instrumentation) ---
#
# These tests cover the test-observable counter that the v2-sole-authority
# acceptance gate (PLAN-authority-flip.md step 3) uses to assert the
# materializer did not reach into scalar. The counter is incremented in the
# same ``else`` branch that produces the warning / raise; strict mode
# short-circuits before the bump.


def test_scalar_fallback_counter_does_not_increment_when_byte_source_serves(
    tmp_path, monkeypatch
) -> None:
    """When the substrate byte source serves the path, the counter stays at 0."""
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        reset_scalar_fallback_invocations,
        scalar_fallback_invocations,
    )

    from tests.support.builders import make_marker_filesystem_vcscore

    monkeypatch.delenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, raising=False)
    reset_scalar_fallback_invocations()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="served.txt", content=b"from-substrate\n")
        _substitute_runtime(
            mg,
            ground_workspace_byte_source=lambda path: (b"from-substrate\n", 0o100644)
            if path == "served.txt"
            else None,
            ground_workspace_is_tree_backed=lambda: True,
        )
        mg.push()
        assert scalar_fallback_invocations() == 0, (
            "byte source served every path; counter must stay at 0"
        )
    finally:
        mg.deactivate()


# Note on the missing "digest-only ground" counter test.
#
# A natural fourth case would be "digest-only/pre-v2 ground -> scalar fallback
# fires -> counter increments." It is omitted because the standard test
# fixture (``make_marker_filesystem_vcscore`` + ``activate=True``) already
# wires the v2 byte source via ``VcsCore._read_v2_workspace_file_for_materialization``
# and reports ``ground_workspace_is_tree_backed()`` as True from the start.
# So even on a fresh fixture, byte_source serves the path and the counter
# stays at 0 (which is the *desired* post-Phase-E behavior). Exercising a
# genuine digest-only fallback through this builder would require substituting
# the runtime back to the BuiltInRuntimeBinding default (byte_source returns
# None unconditionally), at which point the case is structurally the same as
# ``test_scalar_fallback_counter_increments_on_tree_backed_drift`` below.


def test_scalar_fallback_counter_increments_on_tree_backed_drift(
    tmp_path, monkeypatch
) -> None:
    """Tree-backed ground + byte-source miss (non-strict) -> warning + scalar
    fallback + counter increment. This is the silent-drift case the counter is
    designed to catch when strict mode is off."""
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        reset_scalar_fallback_invocations,
        scalar_fallback_invocations,
    )

    from tests.support.builders import make_marker_filesystem_vcscore

    monkeypatch.delenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, raising=False)
    reset_scalar_fallback_invocations()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="missing.txt", content=b"scalar\n")
        _substitute_runtime(
            mg,
            ground_workspace_byte_source=lambda _path: None,
            ground_workspace_is_tree_backed=lambda: True,
        )
        mg.push()
        assert scalar_fallback_invocations() >= 1, (
            "tree-backed drift fell back to scalar but counter did not increment"
        )
    finally:
        mg.deactivate()


def test_scalar_fallback_counter_does_not_increment_when_strict_mode_raises(
    tmp_path, monkeypatch
) -> None:
    """Strict mode raises before the bump call, so the counter stays at 0
    even though the materializer reached the fallback branch."""
    from vcs_core._errors import InvalidRepositoryStateError
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        reset_scalar_fallback_invocations,
        scalar_fallback_invocations,
    )

    from tests.support.builders import make_marker_filesystem_vcscore

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")
    reset_scalar_fallback_invocations()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = make_marker_filesystem_vcscore(workspace, activate=True)
    try:
        mg.exec("filesystem", "write", scope=mg.ground, path="missing.txt", content=b"scalar\n")
        _substitute_runtime(
            mg,
            ground_workspace_byte_source=lambda _path: None,
            ground_workspace_is_tree_backed=lambda: True,
        )
        with pytest.raises(InvalidRepositoryStateError):
            mg.push()
        assert scalar_fallback_invocations() == 0, (
            "strict mode raised but the counter was bumped anyway"
        )
    finally:
        mg.deactivate()
