"""End-to-end overlay capture of a REAL subprocess's filesystem edits.

This is the spine regression backbone for the Shepherd/Claude integration:
an external agent (here, a deterministic ``sh`` stand-in for the Claude Agent
SDK) runs as a child process inside a vcs-core overlay mount and edits files.
We assert the edits are captured at ``merge`` (persisted to ground) and fully
reverted by ``discard``.

Unlike ``test_wander_e2e.py`` — which simulates the agent by writing into the
overlay mount from the test's own (in-process) Python — these tests launch an
actual subprocess. Patched-builtins / Python-tier capture cannot see a child
process's writes; only the overlay can. That is precisely the mechanism the
Claude Agent SDK (which edits via a child ``claude`` process) will rely on.

Requires Linux with root privileges and kernel overlayfs support; runs under
Podman via ``make -C vcs-core/packages/core test_container`` on macOS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.vcscore import VcsCore

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        sys.platform != "linux" or os.geteuid() != 0,
        reason="Real-subprocess overlay capture requires Linux with root.",
    ),
]


def _ensure_overlay_available() -> None:
    filesystems = Path("/proc/filesystems")
    if not filesystems.exists():
        pytest.skip("/proc/filesystems not available")
    if "overlay" not in filesystems.read_text():
        pytest.skip("overlayfs is not available in this environment")
    if shutil.which("mount") is None or shutil.which("umount") is None:
        pytest.skip("mount/umount are required for kernel overlay validation")


@pytest.fixture
def overlay_state_root(tmp_path: Path) -> Path:
    _ensure_overlay_available()
    configured = os.environ.get("VCS_CORE_KERNEL_OVERLAY_STATE_ROOT")
    if configured:
        root = Path(configured) / f"subproc-overlay-{uuid.uuid4().hex[:8]}"
    else:
        root = tmp_path / "overlay-state"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def runtime_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def overlay_mg(runtime_workspace: Path, overlay_state_root: Path) -> VcsCore:
    """VcsCore with an auto-detected overlay backend (kernel or FUSE)."""
    repo_path = runtime_workspace / ".vcscore"
    store = Store(str(repo_path))
    context = build_builtin_substrate_context(
        store,
        workspace=runtime_workspace,
        config={"state_root": str(overlay_state_root)},
    )
    marker = MarkerSubstrate(context)
    filesystem = FilesystemSubstrate(context)
    mg = VcsCore(str(runtime_workspace), substrates=[marker, filesystem], store=store)
    try:
        mg.activate()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"Overlay runtime not available in this environment: {exc}")
    if filesystem._backend is None:
        pytest.skip("No overlay backend detected — store-only mode")
    yield mg
    mg.deactivate()


def _backend(mg: VcsCore):
    sub = next(s for s in mg.lifecycle_substrates if getattr(s, "name", None) == "filesystem")
    return sub._backend


def _run_agent(cwd: Path, script: str) -> None:
    """Run a deterministic external 'agent' as a real child process.

    ``sh`` is a genuinely separate process (not the test's Python), so its
    writes are only observable via the overlay — the same path the Claude
    Agent SDK's child process will take.
    """
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, f"agent subprocess failed: {result.stderr or result.stdout}"


def test_real_subprocess_write_is_captured_and_merged(
    overlay_mg: VcsCore, runtime_workspace: Path
) -> None:
    """A real child process's write into the overlay is captured at merge."""
    task = overlay_mg.fork(overlay_mg.ground, "subproc-write", hints={"isolated": True})
    mount = _backend(overlay_mg).working_path(task.name)

    _run_agent(mount, "mkdir -p src && printf '%s' 'written by subprocess agent' > src/agent_out.txt")

    # Edit lives only in the overlay until merge — not in the real workspace.
    assert not (runtime_workspace / "src" / "agent_out.txt").exists()

    overlay_mg.merge(task, overlay_mg.ground)

    effects = overlay_mg.store.filter_effects(effect_type="FileCreate", max_count=20)
    paths = {e.metadata.get("path") for e in effects}
    assert "src/agent_out.txt" in paths
    assert (
        overlay_mg.store.read_workspace_file(Store.GROUND_REF, "src/agent_out.txt")
        == b"written by subprocess agent"
    )


def test_real_subprocess_write_is_reverted_by_discard(
    overlay_mg: VcsCore, runtime_workspace: Path
) -> None:
    """Discarding a scope reverts a real subprocess's edits with no trace.

    v2-truth gate (assertion (a) only): the v2 substrate tree's view of
    ``scratch.txt`` is None before the fork and None after the discard. There
    is no merge into ground in this flow, so no materialization runs and the
    counter-based (b) assertion does not apply — Carrier isolation is the
    claim, asserted against v2 truth instead of the scalar surface.
    """
    backend = _backend(overlay_mg)

    # v2 ground does not see scratch.txt before the speculative fork.
    assert overlay_mg._read_v2_workspace_file_for_materialization("scratch.txt") is None

    task = overlay_mg.fork(overlay_mg.ground, "subproc-discard", hints={"isolated": True})
    mount = backend.working_path(task.name)

    _run_agent(mount, "printf '%s' 'ephemeral' > scratch.txt")
    assert backend.has_layer(task.name)

    overlay_mg.discard(task)

    assert not backend.has_layer(task.name)
    assert not (runtime_workspace / "scratch.txt").exists()
    assert overlay_mg.store.read_workspace_file(Store.GROUND_REF, "scratch.txt") is None
    # v2 ground is also unchanged after the discard: Carrier isolation held.
    assert overlay_mg._read_v2_workspace_file_for_materialization("scratch.txt") is None


def test_real_subprocess_modify_then_merge_produces_patch(
    overlay_mg: VcsCore, runtime_workspace: Path, monkeypatch
) -> None:
    """A real subprocess editing an existing file yields a FilePatch at merge.

    v2-truth gate (assertions (a) and (b)): under strict mode, after the second
    merge the v2 substrate tree carries the modified bytes (a), and an explicit
    materialization completes without the scalar-fallback counter firing (b).
    The FilePatch lifecycle path is structurally distinct from FileCreate; the
    gate carries to both.
    """
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        reset_scalar_fallback_invocations,
        scalar_fallback_invocations,
    )

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")
    reset_scalar_fallback_invocations()

    backend = _backend(overlay_mg)

    v1 = overlay_mg.fork(overlay_mg.ground, "subproc-base", hints={"isolated": True})
    _run_agent(backend.working_path(v1.name), "printf '%s' 'original' > tracked.txt")
    overlay_mg.merge(v1, overlay_mg.ground)
    assert overlay_mg.store.read_workspace_file(Store.GROUND_REF, "tracked.txt") == b"original"

    v2 = overlay_mg.fork(overlay_mg.ground, "subproc-edit", hints={"isolated": True})
    _run_agent(backend.working_path(v2.name), "printf '%s' 'modified' > tracked.txt")
    overlay_mg.merge(v2, overlay_mg.ground)

    effects = overlay_mg.store.filter_effects(effect_type="FilePatch", max_count=20)
    paths = {e.metadata.get("path") for e in effects}
    assert "tracked.txt" in paths
    assert overlay_mg.store.read_workspace_file(Store.GROUND_REF, "tracked.txt") == b"modified"

    # (a) v2 substrate tree carries the patched bytes after the second merge.
    v2_read = overlay_mg._read_v2_workspace_file_for_materialization("tracked.txt")
    assert v2_read is not None, "v2 substrate tree must contain the patched bytes"
    content, _mode = v2_read
    assert content == b"modified", f"v2 served wrong bytes for the patch: {content!r}"

    # (b) Strict-mode materialization completes without scalar fallback.
    counter_before = scalar_fallback_invocations()
    overlay_mg.push()
    counter_after = scalar_fallback_invocations()
    assert counter_after == counter_before, (
        f"materialization fell back to scalar {counter_after - counter_before} time(s) "
        f"on the FilePatch flow; v2 tree is not the sole authority"
    )
    assert (runtime_workspace / "tracked.txt").read_bytes() == b"modified"


# ---------------------------------------------------------------------------
# v2-sole-authority acceptance gate
#
# PLAN-authority-flip.md step 3: prove the v2 substrate tree is the sole byte
# authority for a real-subprocess overlay write.
#
# The three tests above assert via the scalar-visible surface
# (``Store.GROUND_REF`` reads, ``filter_effects``). They confirm the overlay's
# three roles (Carrier / keyframe / evidence) work end-to-end on real
# overlayfs, but they cannot distinguish "v2 head carries the truth" from
# "scalar still serving alongside." This test closes that gap with two
# independent assertions:
#
#   (a) the merged bytes resolve via ``VcsCore._read_v2_workspace_file_for_materialization`` —
#       i.e. directly from the ground world's tree-backed workspace substrate
#       head — proving v2 carries the truth without any reliance on
#       materialization.
#
#   (b) an explicit ``mg.push()`` (which triggers ``materialize_workspace``)
#       completes under strict mode (``VCSCORE_STRICT_TREE_BACKED_MATERIALIZATION``)
#       *without* incrementing the scalar-fallback counter — proving the
#       materializer reached the v2 tree for every diff path and never fell back
#       to scalar. Strict mode would raise on the drift case; the counter
#       additionally catches the silent case where ground is not tree-backed
#       at all. Together they bracket the gate from both sides.
# ---------------------------------------------------------------------------


def test_v2_substrate_tree_is_sole_authority_for_real_subprocess_write(
    overlay_mg: VcsCore, runtime_workspace: Path, monkeypatch
) -> None:
    """v2 substrate tree carries the merged bytes; materialization never falls
    back to scalar. The empirical proof that the two-truths seam is closed for
    this flow."""
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        reset_scalar_fallback_invocations,
        scalar_fallback_invocations,
    )

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")
    reset_scalar_fallback_invocations()

    # Same flow as test_real_subprocess_write_is_captured_and_merged, but
    # under strict mode and instrumented.
    task = overlay_mg.fork(overlay_mg.ground, "subproc-v2-sole-authority", hints={"isolated": True})
    mount = _backend(overlay_mg).working_path(task.name)
    _run_agent(
        mount, "mkdir -p src && printf '%s' 'v2 truth' > src/agent_out.txt"
    )
    overlay_mg.merge(task, overlay_mg.ground)

    # (a) v2 substrate tree serves the bytes, independent of materialization.
    v2_read = overlay_mg._read_v2_workspace_file_for_materialization("src/agent_out.txt")
    assert v2_read is not None, (
        "v2 substrate tree must contain the merged bytes — there should be no "
        "need for materialization to fall back to scalar"
    )
    content, _mode = v2_read
    assert content == b"v2 truth", f"v2 served wrong bytes: {content!r}"

    # (b) Materialization completes without firing the scalar fallback.
    # Under strict mode, a tree-backed ground with a substrate-tree miss would
    # raise InvalidRepositoryStateError; the counter additionally catches any
    # silent fallback path (e.g., ground reporting not-tree-backed when it
    # should be).
    counter_before = scalar_fallback_invocations()
    overlay_mg.push()
    counter_after = scalar_fallback_invocations()
    assert counter_after == counter_before, (
        f"materialization fell back to scalar {counter_after - counter_before} time(s); "
        f"v2 tree is not the sole authority"
    )
    # And confirm materialize_workspace actually applied the bytes.
    assert (runtime_workspace / "src" / "agent_out.txt").read_bytes() == b"v2 truth"


# ---------------------------------------------------------------------------
# v2-sole-authority gate — §4a nested-isolation stress
#
# DESIGN-recording-isolation.md §4a documents a known classification gap:
# when both parent and child have ``isolated=True`` and the parent's overlay
# contains an uncommitted file X, a nested child writing to X has its merge
# classified as ``FileCreate`` instead of ``FilePatch`` — because the
# classifier compares against parent.ref (no X) rather than parent's overlay
# (has X). The docs claim *"the file content is correct -- only the effect
# type label is wrong."*
#
# This test makes that claim empirical and additionally checks the v2-truth
# gate. There are three meaningful outcomes:
#
#   * green → §4a is a C1-cosmetic bug; v2 truth is intact even under the
#     trigger condition; Phase E removes the buggy C1 path entirely with no
#     risk to v2 byte authority.
#   * (a) red → §4a affects v2 content too; the bug is more severe than the
#     §4a §"Consequence" wording admits; Phase E must handle this case before
#     scalar removal.
#   * (b) red → nested isolation produces v2-incomplete revisions and
#     materialization actually relies on the scalar fallback to land the
#     bytes; the two-truths seam is operationally real for this flow.
# ---------------------------------------------------------------------------


def test_v2_gate_under_4a_nested(monkeypatch, tmp_path_factory) -> None:
    """Stress the §4a trigger condition (nested isolated scopes with
    uncommitted parent-overlay state); assert v2 truth and no scalar fallback.

    Builds a fresh VcsCore *outside* the standard ``overlay_mg`` / ``tmp_path``
    fixture path, because the nested-isolated overlay-mount option string
    exceeds the Linux kernel's threshold (~400-450 chars, empirically observed
    2026-05-31) when the workspace lives under pytest's default
    ``/tmp/pytest-of-root/pytest-N/test_XXX/`` tree. The kernel rejects the
    third stacked overlay mount with the generic ``wrong fs type, bad option,
    bad superblock`` error — entirely a path-length issue, reproducible from a
    shell with no Python involvement and not specific to vcs-core's mount
    construction (the snapshot path ``workspace/.vcscore/runtime/snapshots/<40-char-OID>/root``
    pushes three lowerdir entries past the limit when stacked).

    Using a short ``/tmp/4a-...`` workspace keeps the option string under the
    threshold and exercises the actual §4a behaviour we care about. The same
    finding suggests `VCS_CORE_KERNEL_OVERLAY_STATE_ROOT` is not enough on its
    own (it shortens scope paths but not the snapshot path inside the
    workspace) — a proper fix would shorten the snapshot subdirectory; see
    the spike write-up for the analysis.
    """
    import shutil

    from vcs_core._substrate_runtime import build_builtin_substrate_context
    from vcs_core.store import Store
    from vcs_core.substrates import (
        STRICT_TREE_BACKED_MATERIALIZATION_ENV,
        FilesystemSubstrate,
        MarkerSubstrate,
        reset_scalar_fallback_invocations,
        scalar_fallback_invocations,
    )

    monkeypatch.setenv(STRICT_TREE_BACKED_MATERIALIZATION_ENV, "true")
    reset_scalar_fallback_invocations()

    # Short workspace path: /tmp/4a-<8-hex>/ — keeps the stacked mount option
    # string well under the kernel threshold.
    base = Path(f"/tmp/4a-{uuid.uuid4().hex[:8]}")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir()
    workspace = base / "w"
    workspace.mkdir()
    state_root = base / "s"
    state_root.mkdir()
    try:
        _ensure_overlay_available()
        store = Store(str(workspace / ".vcscore"))
        context = build_builtin_substrate_context(
            store, workspace=workspace, config={"state_root": str(state_root)}
        )
        marker = MarkerSubstrate(context)
        filesystem = FilesystemSubstrate(context)
        mg = VcsCore(str(workspace), substrates=[marker, filesystem], store=store)
        try:
            mg.activate()
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            pytest.skip(f"Overlay runtime not available: {exc}")
        if filesystem._backend is None:
            pytest.skip("No overlay backend detected")
        try:
            # Step 1. Fork an isolated parent. It gets its own overlay.
            parent = mg.fork(mg.ground, "nested-parent", hints={"isolated": True})
            parent_mount = filesystem._backend.working_path(parent.name)

            # Step 2. Write 'shared.txt' directly into parent's overlay. The
            # bytes are on parent's upperdir; parent.ref does NOT yet contain
            # shared.txt — the §4a precondition.
            subprocess.run(
                ["sh", "-c", f"printf '%s' 'parent version' > {parent_mount}/shared.txt"],
                check=True, capture_output=True, text=True,
            )

            # Step 3. Fork a NESTED isolated child. Its lowerdir stack includes
            # parent's upperdir.
            nested = mg.fork(parent, "nested-child", hints={"isolated": True})
            nested_mount = filesystem._backend.working_path(nested.name)

            # Step 4. Nested overwrites shared.txt.
            subprocess.run(
                ["sh", "-c", f"printf '%s' 'nested version' > {nested_mount}/shared.txt"],
                check=True, capture_output=True, text=True,
            )

            # Step 5. Merge nested → parent (where §4a triggers misclassification
            # at the per-file decomposition).
            mg.merge(nested, parent)
            # Step 6. Merge parent → ground.
            mg.merge(parent, mg.ground)

            # === v2-truth gate ===
            # (a) v2 substrate tree carries the nested child's bytes.
            v2_read = mg._read_v2_workspace_file_for_materialization("shared.txt")
            assert v2_read is not None, (
                "v2 substrate tree must carry shared.txt after nested merge; "
                "§4a may affect v2 content too"
            )
            content, _mode = v2_read
            assert content == b"nested version", (
                f"v2 served wrong bytes for nested-isolation flow: {content!r}; "
                f"§4a's content-correct claim is violated"
            )

            # (b) Materialization under strict mode without scalar fallback.
            counter_before = scalar_fallback_invocations()
            mg.push()
            counter_after = scalar_fallback_invocations()
            assert counter_after == counter_before, (
                f"materialization fell back to scalar {counter_after - counter_before} "
                f"time(s) on the nested-isolation flow; v2 is not sole authority under §4a"
            )
            assert (workspace / "shared.txt").read_bytes() == b"nested version"
        finally:
            mg.deactivate()
    finally:
        shutil.rmtree(base, ignore_errors=True)
