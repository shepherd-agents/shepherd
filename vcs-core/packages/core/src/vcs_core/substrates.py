"""Substrate protocol hierarchy and built-in substrates.

The protocol hierarchy is internal in v0.1. Built-in substrates are public.

All writes to the commit DAG flow through the RecordingPipeline.
Substrates produce EffectRecord descriptors; the pipeline records them.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Self

from vcs_core._capture_reducer import (
    CAPTURE_DIAGNOSTIC_KIND,
    CAPTURE_REDUCTION_KIND,
    covered_capture_paths,
    ordered_capture_events,
)
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._fork_hints import validate_branch_hints
from vcs_core._fs_capture import (
    FS_CAPTURE_SHELL_COMMAND_FINISH_OP,
    FsCaptureEvent,
    normalize_fs_capture_op,
    normalize_fs_capture_path,
)
from vcs_core._hooks import (
    HookCaptureDiagnostic,
    HookCaptureEvent,
    HookCaptureProcessFinish,
    HookCaptureProcessStart,
    HookCaptureShellCommandFinish,
    HookEffects,
    HookEvent,
    HookIgnored,
    SystemHook,
)
from vcs_core._patch_paths import PatchPathCandidate, PatchPathCandidateLike, resolve_patch_path
from vcs_core._substrate_runtime import (
    BuiltInRuntimeBinding,
    BuiltInSubstrateContext,
    CarrierBackend,
    PatchMutationIntent,
    PerformedEventSpec,
    PythonPatch,
    bootstrap_builtin_runtime,
)
from vcs_core.authority import SubstrateAuthority, make_authority_aspect
from vcs_core.spi import (
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    IngressRequest,
    ParamSpec,
    UnsupportedRequestError,
)
from vcs_core.types import (
    EffectRecord,
    FileState,
    ScopeInfo,
    WorkspaceChange,
    normalize_git_filemode,
    posix_to_git_mode,
)

_logger = logging.getLogger(__name__)


STRICT_TREE_BACKED_MATERIALIZATION_ENV = "VCSCORE_STRICT_TREE_BACKED_MATERIALIZATION"


def _strict_tree_backed_materialization_enabled() -> bool:
    """Return True when ``VCSCORE_STRICT_TREE_BACKED_MATERIALIZATION`` is truthy.

    Phase E pre-removal gate. When enabled, a tree-backed ground world that
    needs to fall back to scalar for a diff path raises rather than warns; CI
    runs under this mode to catch drift before scalar deletion. Truthy values
    are ``"1"`` and ``"true"`` (case-insensitive); anything else (including
    unset) yields ``False``.
    """
    value = os.environ.get(STRICT_TREE_BACKED_MATERIALIZATION_ENV)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true"}


# ---------------------------------------------------------------------------
# Scalar-fallback instrumentation (test-observable; production-inert)
#
# The materializer at ``FilesystemSubstrate.materialize_workspace`` reads from
# the substrate's tree-backed ``workspace/`` when possible and falls back to
# the scalar ``Store.read_workspace_file(GROUND_REF, ...)`` otherwise. The
# Phase E "scalar fallback never fires" gate (see DELETION-LEDGER §"Scalar C1
# Recording") needs that fallback to be observable from tests so the
# v2-sole-authority acceptance test can assert it did not fire.
#
# Strict mode (``VCSCORE_STRICT_TREE_BACKED_MATERIALIZATION``) already
# fails-closed for the *drift* case (tree-backed ground world missing a path);
# the counter additionally catches the *silent* case where strict mode is off
# or the ground world is not tree-backed at all. Together they bracket the
# gate from both sides.
#
# Production code MUST NOT branch on this counter; it exists only so tests
# can read it. Not thread-safe by intent — materialization is sequential.
# ---------------------------------------------------------------------------

_scalar_fallback_invocations: int = 0


def scalar_fallback_invocations() -> int:
    """Return the running count of scalar-fallback materialization reads.

    Test instrumentation only. Increments once per diff-path read that fell
    back from the substrate tree to ``Store.read_workspace_file(GROUND_REF)``
    inside ``FilesystemSubstrate.materialize_workspace``.
    """
    return _scalar_fallback_invocations


def reset_scalar_fallback_invocations() -> None:
    """Reset the scalar-fallback counter to zero. Test instrumentation only."""
    global _scalar_fallback_invocations
    _scalar_fallback_invocations = 0


def _bump_scalar_fallback_invocations() -> None:
    """Increment the scalar-fallback counter. Internal helper."""
    global _scalar_fallback_invocations
    _scalar_fallback_invocations += 1


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vcs_core._capture_reducer import CaptureJournalEvent
    from vcs_core._patch_manager import PatchManager
    from vcs_core.materialization import InternalMaterializer


# ---------------------------------------------------------------------------
# Overlay backend detection (module-level, usable without a substrate instance)
# ---------------------------------------------------------------------------


def _has_cap_sys_admin() -> bool:
    """Check whether the current process has CAP_SYS_ADMIN in its effective set."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                cap_hex = int(line.split(":", 1)[1].strip(), 16)
                return bool(cap_hex & (1 << 21))  # CAP_SYS_ADMIN is bit 21
    except (OSError, ValueError, IndexError):
        pass
    return False


def _platform_name() -> str:
    return sys.platform


def detect_overlay_backend() -> str | None:
    """Probe the platform for available overlay backends.

    Returns ``"kernel"``, ``"fuse"``, or ``None``. Side-effect-free.

    Usable without instantiating a substrate — the ``configure`` command
    calls this directly to determine platform capabilities.

    Native overlays only — this stays the low-level platform probe, so ``configure``
    keeps a truthful view of native overlay capability. The macOS APFS clonefile
    default and the portable copy-carrier floor are layered on top in
    ``FilesystemSubstrate._auto_detect_backend_name``, which is what lets
    ``backend=None`` resolve to a working carrier on every platform.
    """
    if _platform_name() != "linux":
        return None
    # Kernel overlayfs: requires root + CAP_SYS_ADMIN + overlayfs support + mount/umount
    if os.geteuid() == 0 and _has_cap_sys_admin():
        proc_fs = Path("/proc/filesystems")
        if (
            proc_fs.exists()
            and "overlay" in proc_fs.read_text()
            and shutil.which("mount") is not None
            and shutil.which("umount") is not None
        ):
            return "kernel"
    # FUSE overlayfs: requires /dev/fuse + fuse-overlayfs + fusermount3
    if (
        Path("/dev/fuse").exists()
        and shutil.which("fuse-overlayfs") is not None
        and shutil.which("fusermount3") is not None
    ):
        return "fuse"
    return None


def _physical_matches(
    physical: tuple[bytes, int] | None,
    content: bytes | None,
    mode: int | None,
) -> bool:
    if content is None:
        return physical is None
    if physical is None:
        return False
    physical_content, physical_mode = physical
    return physical_content == content and physical_mode == (mode or 0o100644)


def _failed_command_origin_from_metadata(operation_id: str, metadata: dict[str, object]) -> dict[str, object] | None:
    status = metadata.get("status")
    if status in (None, "success"):
        return None
    origin: dict[str, object] = {
        "operation_id": operation_id,
        "exit_code": None,
        "signal": None,
    }
    exit_code = metadata.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        origin["exit_code"] = exit_code
    signal = metadata.get("signal")
    if isinstance(signal, int) and not isinstance(signal, bool):
        origin["signal"] = signal
    return origin


# ---------------------------------------------------------------------------
# Built-in substrates
# ---------------------------------------------------------------------------


class _TrackedFileHandle:
    def __init__(
        self,
        handle: Any,
        *,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        rel_path: str,
        resolved_path: Path,
        always_record: bool,
    ) -> None:
        self._handle = handle
        self._manager = manager
        self._substrate = substrate
        self._rel_path = rel_path
        self._resolved_path = resolved_path
        self._always_record = always_record
        self._dirty = False
        self._recorded = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __iter__(self) -> Any:
        return iter(self._handle)

    def __enter__(self) -> Self:
        self._handle.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> Any:
        result = self._handle.__exit__(exc_type, exc_val, exc_tb)
        self._record_if_needed()
        return result

    def write(self, data: Any) -> Any:
        self._dirty = True
        return self._handle.write(data)

    def writelines(self, lines: Any) -> Any:
        self._dirty = True
        return self._handle.writelines(lines)

    def truncate(self, size: int | None = None) -> int:
        self._dirty = True
        if size is None:
            return int(self._handle.truncate())
        return int(self._handle.truncate(size))

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
        self._record_if_needed()

    def _record_if_needed(self) -> None:
        if self._recorded or not (self._always_record or self._dirty):
            return
        self._recorded = True
        if not self._resolved_path.exists() or not self._resolved_path.is_file():
            return
        with self._manager.guard():
            content = self._resolved_path.read_bytes()
            file_stat = self._resolved_path.stat()
        params: dict[str, Any] = {"path": self._rel_path, "content": content}
        mode = posix_to_git_mode(file_stat.st_mode)
        if mode != 0o100644:
            params["mode"] = mode
        self._manager.record_performed_event(
            self._substrate,
            "write",
            params,
        )


class MarkerSubstrate:
    """Level 1 (Observe): zero-domain-effect annotations."""

    name = "marker"
    binding = "marker"
    role = "marker"
    driver_id = "marker"
    driver_version = "v1"

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands=self.commands,
        )

    @property
    def commands(self) -> dict[str, CommandSpec]:
        return {
            "mark": CommandSpec(
                description="Create an annotation marker.",
                params={
                    "label": ParamSpec(type="str", description="Marker label."),
                    "metadata": ParamSpec(
                        type="object",
                        required=False,
                        description="Optional arbitrary metadata to attach to the marker.",
                    ),
                },
                examples=("vcs-core exec marker mark -p label=checkpoint",),
            )
        }

    def __init__(self, ctx: BuiltInSubstrateContext) -> None:
        self.bind_runtime(bootstrap_builtin_runtime(ctx)[0])

    def bind_runtime(self, binding: BuiltInRuntimeBinding) -> None:
        self._runtime = binding
        self._pipeline = binding.pipeline

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass

    def materializers(self) -> Sequence[InternalMaterializer]:
        return ()

    def push(self, scope_id: str | None = None) -> None:
        del scope_id

    def authority(self) -> SubstrateAuthority:
        return SubstrateAuthority(
            substrate=self.name,
            containment=make_authority_aspect(
                regime="none",
                access_gated=False,
                tier="recording",
                reason="Markers do not gate or isolate external state changes.",
            ),
            provenance=make_authority_aspect(
                regime="none",
                access_gated=False,
                tier="recording",
                reason="Marker effects exist only when explicitly emitted by the caller.",
            ),
            reason="Marker effects are recorded only when explicitly emitted by the caller.",
        )

    def python_patches(self) -> Sequence[PythonPatch]:
        return ()

    def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
        del context
        if not isinstance(request, CommandRequest):
            raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))
        return self.execute(request.command, self._pipeline.require_world(), **dict(request.params))

    def capture_adapters(self, context: DriverContext) -> tuple[Any, ...]:
        del context
        return ()

    def validate_result(self, request: IngressRequest, result: DriverIngressResult) -> None:
        del request, result

    def execute(
        self,
        command: str,
        scope: ScopeInfo,
        **params: Any,
    ) -> DriverIngressResult:
        del scope
        if command != "mark":
            raise ValueError(f"Unknown marker command: {command!r}")
        return DriverIngressResult(effects=(self._marker_effect(params["label"], params.get("metadata")),))

    def _marker_effect(self, label: object, metadata: object | None) -> EffectRecord:
        effect_metadata: dict[str, Any] = {"label": label}
        metadata = metadata or {}
        if metadata:
            effect_metadata["metadata"] = metadata
        return EffectRecord(
            effect_type="Marker",
            metadata=effect_metadata,
        )

    def mark(self, label: str, metadata: dict[str, Any] | None = None, *, scope: ScopeInfo | None = None) -> str:
        """Record a marker on the given scope, or the ambient scope."""
        scope = self._pipeline.require_world(scope)
        effect = self._marker_effect(label, metadata)
        return self._pipeline.record_runtime_effect(
            effect,
            substrate="marker",
            scope=scope,
            boundary_policy="append_or_root",
            operation_kind="marker.mark",
            operation_label=f"marker-{label}",
            operation_metadata={"label": label},
        )


class FilesystemSubstrate:
    """Filesystem substrate with store-only and overlay-backed modes."""

    name = "filesystem"
    binding = "filesystem"
    role = "filesystem"
    driver_id = "filesystem"
    driver_version = "v1"

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(accepts=frozenset({CommandRequest}), selectable=False)

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands=self.commands,
        )

    @property
    def commands(self) -> dict[str, CommandSpec]:
        return {
            "write": CommandSpec(
                description="Write content to a workspace file.",
                params={
                    "path": ParamSpec(type="str", description="Relative workspace path."),
                    "content": ParamSpec(type="bytes", description="File content bytes."),
                    "mode": ParamSpec(
                        type="int",
                        required=False,
                        description="Optional Git filemode, such as 100644 or 100755.",
                    ),
                },
                examples=("vcs-core exec filesystem write -p path=src/main.py -p content=@src/main.py",),
            ),
            "read": CommandSpec(
                description="Record a file read for provenance.",
                params={"path": ParamSpec(type="str", description="Relative workspace path.")},
                examples=("vcs-core exec filesystem read -p path=src/main.py",),
            ),
            "delete": CommandSpec(
                description="Delete a workspace file.",
                params={"path": ParamSpec(type="str", description="Relative workspace path.")},
                examples=("vcs-core exec filesystem delete -p path=src/main.py",),
            ),
        }

    def __init__(
        self,
        ctx: BuiltInSubstrateContext,
        *,
        backend: CarrierBackend | None = None,
    ) -> None:
        runtime, workspace = bootstrap_builtin_runtime(ctx)
        self.bind_runtime(runtime)
        self._ctx = ctx
        self._store = ctx.store
        self._workspace = workspace
        self._backend = backend
        self._owns_backend = backend is None
        self._overlay_scopes: set[str] = set()

    def _resolve_overlay_state_root(self, ctx: BuiltInSubstrateContext) -> Path:
        configured = (
            ctx.config.get("state_root")
            or os.environ.get("VCS_CORE_OVERLAY_STATE_ROOT")
            or os.environ.get("VCS_CORE_KERNEL_OVERLAY_STATE_ROOT")
        )
        if configured:
            path = Path(str(configured))
            if not path.is_absolute():
                path = ctx.workspace / path
            return path
        workspace = ctx.workspace.resolve()
        workspace_name = workspace.name or "workspace"
        digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
        # Keep upper/work dirs outside the workspace lowerdir so overlay mounts remain valid by default.
        return Path(tempfile.gettempdir()) / "vcs-core-overlay" / f"{workspace_name}-{digest}"

    def _auto_detect_backend_name(self) -> str:
        """Resolve the carrier backend for this platform (never ``None``).

        Prefers a native overlay when present (``detect_overlay_backend()`` →
        kernel/FUSE on Linux), then the macOS APFS clonefile carrier, then the
        portable copy carrier as a universal floor — so an isolated run always
        has a working carrier, including on bare Linux / WSL with no overlay
        support configured.
        """
        native = detect_overlay_backend()
        if native is not None:
            return native
        if _platform_name() == "darwin":
            return "clonefile"
        return "copy"

    def _build_overlay_backend(self, ctx: BuiltInSubstrateContext) -> CarrierBackend | None:
        backend_name = ctx.config.get("backend")
        if backend_name is None:
            backend_name = self._auto_detect_backend_name()
        state_root = self._resolve_overlay_state_root(ctx)
        from vcs_core._workspace_snapshot import render_workspace_snapshot
        from vcs_core.store import GROUND_REF

        snapshot = render_workspace_snapshot(ctx.store, GROUND_REF)
        base_lowerdir = snapshot.root
        if backend_name == "kernel":
            from vcs_core._kernel_overlay import KernelOverlayBackend

            return KernelOverlayBackend(
                workspace=ctx.workspace,
                state_root=state_root,
                base_lowerdir=base_lowerdir,
                base_tree_oid=snapshot.tree_oid,
            )
        if backend_name == "fuse":
            from vcs_core._fuse_overlay import FuseOverlayBackend

            return FuseOverlayBackend(
                workspace=ctx.workspace,
                state_root=state_root,
                base_lowerdir=base_lowerdir,
                base_tree_oid=snapshot.tree_oid,
                fuse_overlayfs_bin=str(ctx.config.get("fuse_overlayfs_bin", "fuse-overlayfs")),
                fusermount_bin=str(ctx.config.get("fusermount_bin", "fusermount3")),
            )
        if backend_name == "clonefile":
            from vcs_core._clonefile_carrier import ClonefileCarrierBackend

            return ClonefileCarrierBackend(
                workspace=ctx.workspace,
                state_root=state_root,
                base_lowerdir=base_lowerdir,
                base_tree_oid=snapshot.tree_oid,
            )
        if backend_name == "copy":
            from vcs_core._copy_carrier import CopyCarrierBackend

            return CopyCarrierBackend(
                workspace=ctx.workspace,
                state_root=state_root,
                base_lowerdir=base_lowerdir,
                base_tree_oid=snapshot.tree_oid,
            )
        raise ValueError(f"Unknown filesystem backend: {backend_name!r}")

    def bind_runtime(self, binding: BuiltInRuntimeBinding) -> None:
        self._runtime = binding
        self._pipeline = binding.pipeline

    def activate(self) -> None:
        if self._backend is None:
            self._backend = self._build_overlay_backend(self._ctx)
        if self._backend is not None:
            self._backend.create_layer("ground", parent_scope_id=None)

    def deactivate(self) -> None:
        if self._backend is not None:
            self._backend.deactivate()
            if self._owns_backend:
                self._backend = None
        self._overlay_scopes.clear()

    def has_overlay_layer(self, scope_name: str) -> bool:
        return self._backend is not None and self._backend.has_layer(scope_name)

    def overlay_mount_path(self, scope_name: str) -> Path | None:
        backend = self._backend
        if backend is None or not backend.has_layer(scope_name):
            return None
        return backend.working_path(scope_name)

    def overlay_changes(self, scope_name: str) -> list[WorkspaceChange]:
        backend = self._backend
        if backend is None or not backend.has_layer(scope_name):
            return []
        changes: list[WorkspaceChange] = []
        for path, content, mode in backend.diff_layer(scope_name):
            if content is None:
                changes.append((path, None))
            else:
                changes.append(FileState(content, mode).to_workspace_change(path))
        return changes

    def materializers(self) -> Sequence[InternalMaterializer]:
        from vcs_core.materialization import _FilesystemMaterializer

        return (_FilesystemMaterializer(self),)

    def push(self, scope_id: str | None = None) -> None:
        """Legacy substrate-local push hook kept for the experimental SPI."""
        self.materialize_workspace(scope_id=scope_id)

    def materialize_workspace(self, scope_id: str | None = None) -> None:
        """Apply pending filesystem state to the real workspace once.

        For each pending file change, the substrate tree is preferred as the
        byte source when the ground world's workspace head is tree-backed.
        Scalar ``Store.read_workspace_file(GROUND_REF, ...)`` is the fallback
        for digest-only revisions and for any read the substrate tree cannot
        serve (e.g., no v2 ground world yet). Both surfaces are
        byte-equivalent under Tranche 2's alternates configuration; the
        substrate-first read makes the materializer's dependency on scalar
        coord storage compensable rather than load-bearing.
        """
        if scope_id not in (None, "ground"):
            msg = "Only the ground filesystem state can be materialized."
            raise RuntimeError(msg)
        self._preflight_physical_workspace_matches_materialized()
        byte_source = self._runtime.ground_workspace_byte_source
        ground_is_tree_backed = self._runtime.ground_workspace_is_tree_backed()
        for change in self._store.diff().files:
            path = change.path
            destination = self._workspace_path(path)
            if change.status == "deleted":
                if destination.exists():
                    destination.unlink()
                    self._remove_empty_parent_dirs(destination.parent)
                continue

            content: bytes | None
            mode: int | None
            v2_read = byte_source(path)
            if v2_read is not None:
                content, mode = v2_read
            else:
                if ground_is_tree_backed:
                    # Tranche 1's validator enforces manifest/tree
                    # correspondence at write time, so a tree-backed ground
                    # world should contain every diff path in its embedded
                    # ``workspace/`` tree. Reaching the scalar fallback under
                    # those conditions signals drift worth investigating.
                    # Strict mode (Phase E pre-removal gate) raises instead;
                    # default mode logs and falls back, since scalar still has
                    # the bytes under the alternates configuration.
                    if _strict_tree_backed_materialization_enabled():
                        raise InvalidRepositoryStateError(
                            f"tree-backed ground world has no substrate-tree "
                            f"entry for diff path {path!r}; "
                            f"{STRICT_TREE_BACKED_MATERIALIZATION_ENV} is enabled"
                        )
                    _logger.warning(
                        "filesystem materialization: tree-backed ground world "
                        "has no substrate-tree entry for diff path %r; falling "
                        "back to scalar GROUND_REF",
                        path,
                    )
                _bump_scalar_fallback_invocations()
                content = self._store.read_workspace_file(self._store.GROUND_REF, path)
                mode = self._store.workspace_file_mode(self._store.GROUND_REF, path)
            if content is None:
                msg = f"Pending filesystem change {path!r} has no ground content."
                raise RuntimeError(msg)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(stat.S_IMODE(mode or 0o100644))

    def authority(self) -> SubstrateAuthority:
        if self._backend is not None:
            containment = make_authority_aspect(
                regime="complete",
                access_gated=True,
                tier="container",
                reason="Overlay-backed sessions gate filesystem writes and preserve authoritative final state before materialization.",
            )
            provenance = make_authority_aspect(
                regime="partial",
                access_gated=True,
                tier="container",
                reason="Overlay-backed sessions preserve final state, but canonical low-level filesystem history remains partial until direct capture covers all mutation paths.",
            )
            reason = "Filesystem substrate provides authoritative containment with partial low-level provenance."
        else:
            containment = make_authority_aspect(
                regime="none",
                access_gated=False,
                tier="python",
                reason="Python interception does not gate or isolate filesystem access.",
            )
            provenance = make_authority_aspect(
                regime="partial",
                access_gated=False,
                tier="python",
                reason="Filesystem capture relies on Python interception and can be bypassed by non-Python writes.",
            )
            reason = "Filesystem substrate provides partial provenance without authoritative containment."

        return SubstrateAuthority(
            substrate=self.name,
            containment=containment,
            provenance=provenance,
            reason=reason,
        )

    def python_patches(self) -> Sequence[PythonPatch]:
        # Patches are installed at activate time regardless of backend.
        # VcsCore installs explicit execution context only around scoped
        # work, so incidental coordinator I/O outside that context does not
        # get attributed: pipeline.context.world is None post-merge/discard, and
        # record_effects / record_performed_event short-circuit on None.
        return (
            PythonPatch(
                target="builtins.open",
                wrap_handler=self._handle_open,
                path_candidates=self._open_candidates,
                mutation_intent=self._open_mutation_intent,
            ),
            PythonPatch(
                target="io.open",
                wrap_handler=self._handle_open,
                path_candidates=self._open_candidates,
                mutation_intent=self._open_mutation_intent,
            ),
            PythonPatch(
                target="os.remove",
                after_translator=self._translate_remove,
                path_candidates=self._single_path_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="os.unlink",
                after_translator=self._translate_remove,
                path_candidates=self._single_path_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="os.chmod",
                after_translator=self._translate_chmod,
                path_candidates=self._single_path_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="pathlib.Path.chmod",
                after_translator=self._translate_chmod,
                path_candidates=self._single_path_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="os.rename",
                wrap_handler=self._handle_rename,
                path_candidates=self._source_dest_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="os.replace",
                wrap_handler=self._handle_rename,
                path_candidates=self._source_dest_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="shutil.copyfile",
                wrap_handler=self._handle_copy,
                path_candidates=self._copy_destination_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="shutil.copy2",
                wrap_handler=self._handle_copy,
                path_candidates=self._copy_destination_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="shutil.move",
                wrap_handler=self._handle_move,
                path_candidates=self._source_dest_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
            PythonPatch(
                target="shutil.rmtree",
                wrap_handler=self._handle_rmtree,
                path_candidates=self._single_path_candidates,
                requires_scope=True,
                mutation_intent="external_write",
            ),
        )

    @staticmethod
    def _open_mutation_intent(*args: Any, **kwargs: Any) -> PatchMutationIntent:
        mode = str(args[1] if len(args) > 1 else kwargs.get("mode", "r"))
        if any(flag in mode for flag in "wax+"):
            return "external_write"
        return "none"

    def system_hooks(self) -> Sequence[SystemHook]:
        return (
            SystemHook(
                hook_id="filesystem-direct",
                kind="ld_preload",
                config={"shim": "fs_capture_shim.so"},
                translator=self._translate_preload_event,
                capabilities=frozenset({"fs_capture"}),
            ),
        )

    def _translate_preload_event(
        self, event: HookEvent
    ) -> (
        HookCaptureEvent
        | HookCaptureDiagnostic
        | HookCaptureProcessStart
        | HookCaptureProcessFinish
        | HookCaptureShellCommandFinish
        | HookEffects
        | HookIgnored
        | None
    ):
        lifecycle = event.payload.get("capture_lifecycle")
        if lifecycle == "process_start" and event.phase == "start":
            return HookCaptureProcessStart()
        if lifecycle == "process_finish" and event.phase == "finish":
            last_proc_seq = event.payload.get("last_proc_seq")
            if isinstance(last_proc_seq, int) and not isinstance(last_proc_seq, bool):
                return HookCaptureProcessFinish(last_proc_seq=last_proc_seq)
            return HookIgnored("ignored_unsupported", reason="invalid filesystem capture lifecycle payload")
        if event.phase != "point":
            return HookIgnored("ignored_unsupported", reason="unsupported filesystem hook phase")
        op_value = event.payload.get("op")
        seq = event.payload.get("seq")
        if op_value == FS_CAPTURE_SHELL_COMMAND_FINISH_OP:
            if not isinstance(seq, int) or isinstance(seq, bool):
                return HookIgnored("ignored_unsupported", reason="invalid filesystem capture shell finish payload")
            return HookCaptureShellCommandFinish(seq=seq)
        op = normalize_fs_capture_op(op_value)
        path = normalize_fs_capture_path(event.payload.get("path"))
        capture_mechanism = event.payload.get("capture_mechanism", "preload")
        if op is None or path is None or not isinstance(seq, int) or isinstance(seq, bool):
            return HookIgnored("ignored_unsupported", reason="invalid filesystem capture payload")
        if not isinstance(capture_mechanism, str) or not capture_mechanism:
            return HookIgnored("ignored_unsupported", reason="invalid filesystem capture mechanism")
        scope = self._runtime.lookup_scope(event.scope)
        if scope is None:
            return HookIgnored("ignored_stale_scope", reason=f"no live filesystem scope: {event.scope}")
        fs_event = FsCaptureEvent(
            op=op,
            scope=event.scope,
            scope_instance_id=event.scope_instance_id,
            path=path,
            pid=event.pid,
            proc_seq=event.proc_seq,
            ppid=event.ppid,
            exe=event.exe,
            cwd=event.cwd,
        )
        if event.command_operation_id is None:
            return HookCaptureDiagnostic(
                event=fs_event,
                seq=seq,
                capture_mechanism=capture_mechanism,
                reason="shim_context_missing",
            )
        return HookCaptureEvent(event=fs_event, seq=seq, capture_mechanism=capture_mechanism)

    def effects_for_capture_reduction(
        self,
        scope: ScopeInfo,
        events: Sequence[CaptureJournalEvent],
        *,
        failed_command_origin: dict[str, object] | None = None,
    ) -> tuple[EffectRecord, ...]:
        """Reduce raw command-correlated capture events into final filesystem effects."""
        covered_paths: list[str] = []
        seen: set[str] = set()
        attribution: dict[str, CaptureJournalEvent] = {}
        for event in sorted(events, key=lambda item: (item.global_seq, item.pid, item.proc_seq)):
            if event.path not in seen:
                covered_paths.append(event.path)
                seen.add(event.path)
            attribution[event.path] = event

        effects: list[EffectRecord] = []
        for path in covered_paths:
            effect = self._reduced_capture_effect_for_path(
                scope,
                path,
                attribution=attribution[path],
                failed_command_origin=failed_command_origin,
            )
            if effect is not None:
                effects.append(effect)
        return tuple(effects)

    def branch(self, scope_id: str, *, parent_scope: ScopeInfo, hints: dict[str, Any] | None = None) -> None:
        validate_branch_hints(hints)
        wants_isolation = bool(hints and hints.get("isolated"))
        if not wants_isolation:
            return
        if self._backend is None:
            raise RuntimeError(
                "Scope requested isolated=True but no overlay backend is available. "
                'Use backend="copy" (the portable carrier) or backend=None (auto).'
            )
        restoring = bool(hints and hints.get("__restore__"))
        if restoring and self._backend.has_layer(scope_id):
            self._overlay_scopes.add(scope_id)
            return
        parent_layer = self._runtime.overlay_base_scope_name(parent_scope)
        self._backend.create_layer(scope_id, parent_scope_id=parent_layer)
        self._overlay_scopes.add(scope_id)

    def prepare_merge(self, scope: ScopeInfo, parent: ScopeInfo) -> Sequence[EffectRecord]:
        del parent
        if self._backend is None or scope.name not in self._overlay_scopes:
            return []

        changes = self._backend.diff_layer(scope.name)
        fallback_metadata = self._capture_reconcile_fallback_metadata_by_path(scope)
        effects: list[EffectRecord] = []
        for path, content, mode in changes:
            effective_mode = mode or None  # 0 (deletion sentinel) → None
            effect = self._reconciled_effect_for_change(
                scope,
                path,
                content,
                mode=effective_mode,
                reconcile_metadata=fallback_metadata.get(path),
            )
            if effect is not None:
                effects.append(effect)
        return effects

    def commit_merge(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
        if self._backend is not None and scope_id in self._overlay_scopes:
            parent_layer = self._runtime.overlay_base_scope_name(parent_scope)
            self._backend.commit_layer(scope_id, into_scope_id=parent_layer)
        self._overlay_scopes.discard(scope_id)

    def close_retained(self, scope_id: str, *, parent_scope: ScopeInfo) -> None:
        del parent_scope
        if self._backend is not None and scope_id in self._overlay_scopes:
            self._backend.discard_layer(scope_id)
        self._overlay_scopes.discard(scope_id)

    def discard(self, scope_id: str) -> None:
        if self._backend is not None and scope_id in self._overlay_scopes:
            self._backend.discard_layer(scope_id)
        self._overlay_scopes.discard(scope_id)

    def _file_exists_in_workspace(self, scope: ScopeInfo, path: str) -> bool:
        return self._pipeline.store.file_exists_in_workspace(scope.ref, path)

    def _workspace_path(self, path: str) -> Path:
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts:
            msg = f"Invalid workspace-relative path: {path!r}"
            raise ValueError(msg)
        return self._workspace.joinpath(*pure.parts)

    def _read_physical_file(self, path: str) -> tuple[bytes, int] | None:
        destination = self._workspace_path(path)
        if not destination.exists():
            return None
        if destination.is_symlink() or not destination.is_file():
            msg = f"Cannot materialize over unsupported physical workspace entry: {path!r}"
            raise RuntimeError(msg)
        return (destination.read_bytes(), posix_to_git_mode(destination.stat().st_mode))

    def _preflight_physical_workspace_matches_materialized(self) -> None:
        from vcs_core._workspace_external import ExternalWorkspace

        external_workspace = ExternalWorkspace(self._workspace)
        diff_paths = {change.path for change in self._store.diff().files}
        if external_workspace.git_workspace is not None:
            blockers = external_workspace.git_index_blockers()
            if blockers:
                sample = ", ".join(f"{blocker.path} ({blocker.reason})" for blocker in blockers[:5])
                remainder = len(blockers) - min(len(blockers), 5)
                suffix = f", and {remainder} more" if remainder > 0 else ""
                msg = f"Refusing to materialize with {len(blockers)} dirty Git index path(s): {sample}{suffix}."
                raise RuntimeError(msg)

        for path in sorted(diff_paths):
            expected_content = self._store.read_workspace_file(self._store.MAT_REF, path)
            expected_mode = self._store.workspace_file_mode(self._store.MAT_REF, path)
            desired_content = self._store.read_workspace_file(self._store.GROUND_REF, path)
            desired_mode = self._store.workspace_file_mode(self._store.GROUND_REF, path)

            exact = external_workspace.read_exact_physical(path)
            if exact.is_unsupported:
                msg = f"Refusing to materialize {path!r}: physical path is an unsupported {exact.kind}."
                raise RuntimeError(msg)
            physical = exact.file_tuple
            if _physical_matches(physical, desired_content, desired_mode):
                continue
            if expected_content is None:
                if physical is not None:
                    msg = f"Refusing to materialize {path!r}: physical file exists but materialized baseline is absent."
                    raise RuntimeError(msg)
                continue
            if not _physical_matches(physical, expected_content, expected_mode):
                msg = f"Refusing to materialize {path!r}: physical file differs from materialized baseline."
                raise RuntimeError(msg)

    def _remove_empty_parent_dirs(self, path: Path) -> None:
        current = path
        while current != self._workspace and self._workspace in current.parents:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _read_workspace_file(self, scope: ScopeInfo, path: str) -> bytes | None:
        return self._pipeline.store.read_workspace_file(scope.ref, path)

    def _read_workspace_file_mode(self, scope: ScopeInfo, path: str) -> int | None:
        return self._pipeline.store.workspace_file_mode(scope.ref, path)

    def _capture_reducer_covered_paths(self, scope: ScopeInfo) -> frozenset[str]:
        paths: set[str] = set()
        try:
            summaries = self._pipeline.store.visible_operations(ref=scope.ref, max_count=1000)
        except Exception:  # noqa: BLE001
            return frozenset()
        for summary in summaries:
            if summary.kind != CAPTURE_REDUCTION_KIND:
                continue
            try:
                history = self._pipeline.store.read_visible_operation_history(
                    scope.ref, operation_id=summary.operation_id
                )
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "Skipping unreadable capture reduction operation %s while preparing merge for %s",
                    summary.operation_id,
                    scope.ref,
                    exc_info=True,
                )
                continue
            for commit in history.commits:
                capture = commit.metadata.get("capture")
                if not isinstance(capture, dict) or capture.get("capture_status") != "complete":
                    continue
                covered = capture.get("covered_paths")
                if isinstance(covered, list):
                    paths.update(path for path in covered if isinstance(path, str))
        return frozenset(paths)

    def _capture_reconcile_fallback_metadata_by_path(self, scope: ScopeInfo) -> dict[str, dict[str, object]]:
        world_id = scope.world_id
        if not world_id:
            return {}
        fallback_by_path: dict[str, dict[str, object]] = {}
        try:
            summaries = self._pipeline.store.archived_operations(max_count=1000, world_id=world_id)
        except Exception:  # noqa: BLE001
            return {}
        self._add_capture_fallbacks_from_summaries(scope, summaries, fallback_by_path)
        try:
            visible_summaries = self._pipeline.store.visible_operations(ref=scope.ref, max_count=1000)
        except Exception:  # noqa: BLE001
            return fallback_by_path
        self._add_capture_fallbacks_from_summaries(scope, visible_summaries, fallback_by_path)
        return fallback_by_path

    def _add_capture_fallbacks_from_summaries(
        self,
        scope: ScopeInfo,
        summaries: Sequence[Any],
        fallback_by_path: dict[str, dict[str, object]],
    ) -> None:
        for summary in summaries:
            if summary.kind not in {"vcs_core.session_exec", CAPTURE_DIAGNOSTIC_KIND}:
                continue
            try:
                if summary.kind == CAPTURE_DIAGNOSTIC_KIND:
                    history = self._pipeline.store.read_visible_operation_history(
                        scope.ref, operation_id=summary.operation_id
                    )
                else:
                    history = self._pipeline.store.read_operation_history(summary.carrier_ref)
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "Skipping unreadable capture fallback operation %s while preparing merge for %s",
                    summary.operation_id,
                    scope.ref,
                    exc_info=True,
                )
                continue
            paths = covered_capture_paths(ordered_capture_events(history.commits))
            if not paths:
                continue
            for commit in history.commits:
                command = commit.metadata.get("command")
                capture = commit.metadata.get("capture")
                incomplete = command if isinstance(command, dict) else capture
                if isinstance(incomplete, dict) and incomplete.get("capture_status") == "incomplete":
                    reason = incomplete.get("capture_incomplete_reason")
                    if isinstance(reason, str) and reason:
                        reconcile_reason = f"capture_incomplete:{reason}"
                    else:
                        reconcile_reason = "capture_incomplete"
                    command_operation_id = incomplete.get("command_operation_id")
                    if not isinstance(command_operation_id, str) or not command_operation_id:
                        command_operation_id = summary.operation_id
                    metadata = {
                        "reconcile_reason": reconcile_reason,
                        "reconcile_command_operation_id": command_operation_id,
                    }
                    if summary.kind == "vcs_core.session_exec" and isinstance(command, dict):
                        failed_origin = _failed_command_origin_from_metadata(command_operation_id, command)
                        if failed_origin is not None:
                            metadata["failed_command_origin"] = failed_origin
                    for path in paths:
                        fallback_by_path.setdefault(path, metadata)
                    break

    def effect_for_captured_event(
        self,
        scope: ScopeInfo,
        event: FsCaptureEvent,
        *,
        seq: int,
        capture_mechanism: str = "preload",
    ) -> EffectRecord | None:
        op = normalize_fs_capture_op(event.op)
        path = normalize_fs_capture_path(event.path)
        if op is None or path is None:
            return None
        if self._claim_policy_for_workspace_path(path) == "authoritative_suppress_fs":
            return None
        try:
            current = self._read_workspace_file(scope, path)
        except ValueError:
            return None
        metadata = {
            "path": path,
            "capture_mode": "direct",
            "capture_mechanism": capture_mechanism,
            "pid": event.pid,
            "proc_seq": event.proc_seq,
            "seq": seq,
        }

        if op == "unlink":
            if current is None:
                return None
            return EffectRecord(
                effect_type="FileDelete",
                metadata=metadata,
                workspace_changes=((path, None),),
            )

        if self._backend is None:
            return None

        layer_scope = self._runtime.overlay_base_scope_name(scope)
        try:
            state = self._backend.read_file_state(layer_scope, path)
        except (OSError, ValueError):
            return None

        try:
            current_mode = self._read_workspace_file_mode(scope, path)
        except ValueError:
            return None
        if current == state.content and current_mode == state.mode:
            return None

        effect_type = "FilePatch" if current is not None else "FileCreate"
        return EffectRecord(
            effect_type=effect_type,
            metadata=metadata,
            workspace_changes=(state.to_workspace_change(path),),
        )

    def _reduced_capture_effect_for_path(
        self,
        scope: ScopeInfo,
        path: str,
        *,
        attribution: CaptureJournalEvent,
        failed_command_origin: dict[str, object] | None,
    ) -> EffectRecord | None:
        path = normalize_fs_capture_path(path) or ""
        if not path:
            return None
        if self._claim_policy_for_workspace_path(path) == "authoritative_suppress_fs":
            return None
        try:
            current = self._read_workspace_file(scope, path)
            current_mode = self._read_workspace_file_mode(scope, path)
        except ValueError:
            return None

        state = None
        if self._backend is not None:
            layer_scope = self._runtime.overlay_base_scope_name(scope)
            try:
                state = self._backend.read_file_state(layer_scope, path)
            except (AssertionError, KeyError, OSError, ValueError):
                state = None

        metadata: dict[str, object] = {
            "path": path,
            "capture_mode": "direct",
            "capture_record": "reduction",
            "capture_status": "complete",
            "capture_mechanism": attribution.capture_mechanism,
            "command_operation_id": attribution.command_operation_id,
            "pid": attribution.pid,
            "proc_seq": attribution.proc_seq,
            "global_seq": attribution.global_seq,
        }
        if failed_command_origin is not None:
            metadata["failed_command_origin"] = failed_command_origin

        if state is None:
            if current is None:
                return None
            return EffectRecord(
                effect_type="FileDelete",
                metadata=metadata,
                workspace_changes=((path, None),),
            )

        if current == state.content and current_mode == state.mode:
            return None
        effect_type = "FilePatch" if current is not None else "FileCreate"
        return EffectRecord(
            effect_type=effect_type,
            metadata=metadata,
            workspace_changes=(state.to_workspace_change(path),),
        )

    def _reconciled_effect_for_change(
        self,
        scope: ScopeInfo,
        path: str,
        content: bytes | None,
        *,
        mode: int | None = None,
        reconcile_metadata: dict[str, object] | None = None,
    ) -> EffectRecord | None:
        if self._claim_policy_for_workspace_path(path) == "authoritative_suppress_fs":
            return None
        if mode is not None:
            mode = normalize_git_filemode(mode)
        current = self._read_workspace_file(scope, path)
        if content is None:
            if current is None:
                return None
            metadata: dict[str, object] = {
                "path": path,
                "capture_mode": "reconciled",
                "capture_mechanism": "overlay-diff",
                "reconcile_reason": "missing_direct_delete",
            }
            if reconcile_metadata is not None:
                metadata.update(reconcile_metadata)
            return EffectRecord(
                effect_type="FileDelete",
                metadata=metadata,
                workspace_changes=((path, None),),
            )

        if current == content:
            # Content unchanged — check if mode changed
            current_mode = self._read_workspace_file_mode(scope, path) or 0o100644
            if mode is None or mode == current_mode:
                return None
            # Mode-only change: fall through to produce a FilePatch

        effect_type = "FilePatch" if current is not None else "FileCreate"
        ws_change: tuple[str, bytes | None] | tuple[str, bytes | None, int]
        if mode is not None and mode != 0o100644:
            ws_change = (path, content, mode)
        else:
            ws_change = (path, content)
        metadata = {
            "path": path,
            "capture_mode": "reconciled",
            "capture_mechanism": "overlay-diff",
            "reconcile_reason": "missing_direct_create_or_patch",
        }
        if reconcile_metadata is not None:
            metadata.update(reconcile_metadata)
        return EffectRecord(
            effect_type=effect_type,
            metadata=metadata,
            workspace_changes=(ws_change,),
        )

    def _resolve_path(self, candidate: PatchPathCandidateLike) -> Path | None:
        return resolve_patch_path(candidate)

    def _claim_policy_for_resolved_path(self, resolved: Path) -> str | None:
        claim = self._runtime.lookup_claim(resolved)
        if claim is None:
            return None
        return claim.policy

    def _claim_policy_for_workspace_path(self, path: str) -> str | None:
        return self._claim_policy_for_resolved_path((self._workspace / path).resolve())

    def _rel(self, candidate: PatchPathCandidateLike) -> str | None:
        resolved = self._resolve_path(candidate)
        if resolved is None:
            return None
        try:
            relative = resolved.relative_to(self._workspace)
        except ValueError:
            return None
        if relative.parts and relative.parts[0] == ".vcscore":
            return None
        if self._claim_policy_for_resolved_path(resolved) == "authoritative_suppress_fs":
            return None
        return relative.as_posix()

    def _single_path_candidates(
        self, path: str | Path | object, *args: Any, **kwargs: Any
    ) -> tuple[PatchPathCandidate, ...]:
        del args
        return (PatchPathCandidate(path, dir_fd=kwargs.get("dir_fd")),) if isinstance(path, (str, os.PathLike)) else ()

    def _source_dest_candidates(
        self, src: object, dst: object, *args: Any, **kwargs: Any
    ) -> tuple[PatchPathCandidate, ...]:
        del args
        candidates = [
            PatchPathCandidate(src, dir_fd=kwargs.get("src_dir_fd")) if isinstance(src, (str, os.PathLike)) else None,
            PatchPathCandidate(dst, dir_fd=kwargs.get("dst_dir_fd")) if isinstance(dst, (str, os.PathLike)) else None,
        ]
        return tuple(candidate for candidate in candidates if candidate is not None)

    def _source_candidate(self, src: object, **kwargs: Any) -> PatchPathCandidateLike:
        if not isinstance(src, (str, os.PathLike)):
            return src
        return PatchPathCandidate(src, dir_fd=kwargs.get("src_dir_fd"))

    def _destination_candidate(self, dst: object, **kwargs: Any) -> PatchPathCandidateLike:
        if not isinstance(dst, (str, os.PathLike)):
            return dst
        return PatchPathCandidate(dst, dir_fd=kwargs.get("dst_dir_fd"))

    def _single_candidate(self, path: str | Path | object, **kwargs: Any) -> PatchPathCandidateLike:
        if not isinstance(path, (str, os.PathLike)):
            return path
        return PatchPathCandidate(path, dir_fd=kwargs.get("dir_fd"))

    def _copy_destination_candidates(
        self, src: object, dst: object, *args: Any, **kwargs: Any
    ) -> tuple[str | os.PathLike[str], ...]:
        del src, args, kwargs
        return (dst,) if isinstance(dst, (str, os.PathLike)) else ()

    def _open_candidates(self, *args: Any, **kwargs: Any) -> tuple[str | os.PathLike[str], ...]:
        if args and isinstance(args[0], (str, os.PathLike)):
            return (args[0],)
        if "file" in kwargs and isinstance(kwargs["file"], (str, os.PathLike)):
            return (kwargs["file"],)
        return ()

    def _translate_remove(
        self,
        path: str | Path | object,
        *args: Any,
        _result: object = None,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        del args, _result
        rel = self._rel(self._single_candidate(path, **kwargs))
        if rel is None:
            return None
        return ("delete", {"path": rel})

    def _translate_chmod(
        self,
        path: str | Path | object,
        mode: object,
        *args: Any,
        _result: object = None,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        del mode, args, _result
        candidate = self._single_candidate(path, **kwargs)
        rel = self._rel(candidate)
        resolved = self._resolve_path(candidate)
        if rel is None or resolved is None:
            return None
        file_state = self._snapshot_file(resolved)
        if file_state is None:
            return None
        return ("write", {"path": rel, "content": file_state.content, "mode": file_state.mode})

    def _snapshot_file(self, path: Path) -> FileState | None:
        if not path.is_file():
            return None
        rel = self._rel(path)
        if rel is None:
            return None
        file_stat = path.stat()
        return FileState(content=path.read_bytes(), mode=posix_to_git_mode(file_stat.st_mode))

    def _snapshot_tree(self, path: PatchPathCandidateLike) -> dict[str, FileState]:
        resolved = self._resolve_path(path)
        if resolved is None or not resolved.exists():
            return {}
        if resolved.is_file():
            file_state = self._snapshot_file(resolved)
            if file_state is None:
                return {}
            rel = self._rel(resolved)
            assert rel is not None
            return {rel: file_state}
        if not resolved.is_dir():
            return {}

        snapshot: dict[str, FileState] = {}
        for candidate in sorted(resolved.rglob("*")):
            file_state = self._snapshot_file(candidate)
            if file_state is None:
                continue
            rel = self._rel(candidate)
            assert rel is not None
            snapshot[rel] = file_state
        return snapshot

    def _record_snapshot_diff(
        self,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        *,
        before: dict[str, FileState],
        after: dict[str, FileState],
    ) -> None:
        for rel in sorted(before.keys() - after.keys()):
            manager.record_performed_event(substrate, "delete", {"path": rel})
        for rel in sorted(after):
            file_state = after[rel]
            if before.get(rel) == file_state:
                continue
            path, content, *mode = file_state.to_workspace_change(rel)
            params: dict[str, Any] = {"path": path, "content": content}
            if mode:
                params["mode"] = mode[0]
            manager.record_performed_event(
                substrate,
                "write",
                params,
            )

    def _seed_snapshot_paths(
        self,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        snapshot: dict[str, FileState],
    ) -> None:
        scope = manager.scope
        if scope is None:
            return
        for rel in sorted(snapshot):
            if self._file_exists_in_workspace(scope, rel):
                continue
            file_state = snapshot[rel]
            path, content, *mode = file_state.to_workspace_change(rel)
            params: dict[str, Any] = {"path": path, "content": content}
            if mode:
                params["mode"] = mode[0]
            manager.record_performed_event(
                substrate,
                "write",
                params,
            )

    def _handle_open(
        self,
        original_fn: Any,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        file_arg = args[0] if args else kwargs.get("file")
        rel = self._rel(file_arg)
        if rel is None:
            return original_fn(*args, **kwargs)

        mode = str(args[1] if len(args) > 1 else kwargs.get("mode", "r"))
        if any(flag in mode for flag in "wax+"):
            manager.require_scope_for_mutation("builtins.open", file_arg)
        handle = original_fn(*args, **kwargs)
        if not any(flag in mode for flag in "wax+"):
            manager.record_performed_event(substrate, "read", {"path": rel})
            return handle

        resolved = self._resolve_path(file_arg)
        if resolved is None:
            return handle
        return _TrackedFileHandle(
            handle,
            manager=manager,
            substrate=substrate,
            rel_path=rel,
            resolved_path=resolved,
            always_record=("w" in mode or "x" in mode),
        )

    def _handle_copy(
        self,
        original_fn: Any,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        src: object,
        dst: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        before_dst: dict[str, FileState] = {}
        result_path = dst
        if self._rel(dst) is not None:
            before_dst = self._snapshot_tree(dst)
        result = original_fn(src, dst, *args, **kwargs)
        if result is not None:
            result_path = result
        after_dst = self._snapshot_tree(result_path)
        with manager.activity(
            operation_label="filesystem-copy",
            operation_kind="filesystem.copy",
            operation_metadata={"src": self._rel(src), "dst": self._rel(result_path)},
        ):
            self._record_snapshot_diff(manager, substrate, before=before_dst, after=after_dst)
        return result

    def _handle_rename(
        self,
        original_fn: Any,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        src: object,
        dst: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        source_candidate = self._source_candidate(src, **kwargs)
        destination_candidate = self._destination_candidate(dst, **kwargs)
        before_src = self._snapshot_tree(source_candidate)
        result = original_fn(src, dst, *args, **kwargs)
        after_dst = self._snapshot_tree(destination_candidate)
        with manager.activity(
            operation_label="filesystem-rename",
            operation_kind="filesystem.rename",
            operation_metadata={"src": self._rel(source_candidate), "dst": self._rel(destination_candidate)},
        ):
            self._seed_snapshot_paths(manager, substrate, before_src)
            self._record_snapshot_diff(manager, substrate, before=before_src, after=after_dst)
        return result

    def _handle_move(
        self,
        original_fn: Any,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        src: object,
        dst: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        before_src = self._snapshot_tree(src)
        result = original_fn(src, dst, *args, **kwargs)
        final_dst = result if result is not None else dst
        after_dst = self._snapshot_tree(final_dst)
        with manager.activity(
            operation_label="filesystem-move",
            operation_kind="filesystem.move",
            operation_metadata={"src": self._rel(src), "dst": self._rel(final_dst)},
        ):
            self._seed_snapshot_paths(manager, substrate, before_src)
            self._record_snapshot_diff(manager, substrate, before=before_src, after=after_dst)
        return result

    def _handle_rmtree(
        self,
        original_fn: Any,
        manager: PatchManager,
        substrate: FilesystemSubstrate,
        path: object,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        candidate = self._single_candidate(path, **kwargs)
        resolved = self._resolve_path(candidate)
        if resolved is None or self._rel(candidate) is None:
            return original_fn(path, *args, **kwargs)
        before = self._snapshot_tree(candidate)
        result = original_fn(path, *args, **kwargs)
        with manager.activity(
            operation_label="filesystem-rmtree",
            operation_kind="filesystem.rmtree",
            operation_metadata={"path": self._rel(candidate)},
        ):
            self._seed_snapshot_paths(manager, substrate, before)
            self._record_snapshot_diff(manager, substrate, before=before, after={})
        return result

    def _backend_write(self, scope: ScopeInfo, path: str, content: bytes, *, mode: int = 0o100644) -> None:
        if self._backend is None:
            return
        self._backend.write_file(self._runtime.overlay_base_scope_name(scope), path, content, mode=mode)

    def _backend_delete(self, scope: ScopeInfo, path: str) -> None:
        if self._backend is None:
            return
        self._backend.delete_file(self._runtime.overlay_base_scope_name(scope), path)

    def prepare(self, context: DriverContext, request: IngressRequest) -> DriverIngressResult:
        del context
        if not isinstance(request, CommandRequest):
            raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))
        return self.execute(request.command, self._pipeline.require_world(), **dict(request.params))

    def capture_adapters(self, context: DriverContext) -> tuple[Any, ...]:
        del context
        return ()

    def validate_result(self, request: IngressRequest, result: DriverIngressResult) -> None:
        del request, result

    def execute(
        self,
        command: str,
        scope: ScopeInfo,
        **params: Any,
    ) -> DriverIngressResult:
        return self._filesystem_result(command, scope, already_performed=False, params=params)

    def performed_event_specs(self) -> dict[str, PerformedEventSpec]:
        return {
            "write": PerformedEventSpec(
                description="A workspace file was written.",
                params=self.commands["write"].params,
                effect_types=("FileCreate", "FilePatch"),
            ),
            "read": PerformedEventSpec(
                description="A workspace file was read.",
                params=self.commands["read"].params,
                effect_types=("FileRead",),
            ),
            "delete": PerformedEventSpec(
                description="A workspace file was deleted.",
                params=self.commands["delete"].params,
                effect_types=("FileDelete",),
            ),
        }

    def performed_effects(
        self,
        event: str,
        scope: ScopeInfo,
        *,
        params: Mapping[str, Any],
    ) -> Sequence[EffectRecord]:
        return self._filesystem_result(event, scope, already_performed=True, params=params).effects

    def _filesystem_result(
        self,
        command: str,
        scope: ScopeInfo,
        *,
        already_performed: bool,
        params: Mapping[str, Any],
    ) -> DriverIngressResult:
        if command == "read":
            path = params["path"]
            return DriverIngressResult(
                effects=(
                    EffectRecord(
                        effect_type="FileRead",
                        metadata={"path": path},
                    ),
                )
            )

        if command == "delete":
            path = params["path"]
            if self._runtime.is_scope_or_ancestor_isolated(scope):
                if not already_performed:
                    self._backend_delete(scope, path)
                return DriverIngressResult()
            return DriverIngressResult(
                effects=(
                    EffectRecord(
                        effect_type="FileDelete",
                        metadata={"path": path},
                        workspace_changes=((path, None),),
                    ),
                )
            )

        if command != "write":
            raise ValueError(f"Unknown filesystem command: {command!r}")

        path = params["path"]
        content = params["content"]
        if content is None:
            raise ValueError("Filesystem write requires non-null content. Use command='delete' for deletions.")
        raw_mode = params.get("mode")
        mode = normalize_git_filemode(raw_mode) if raw_mode is not None else 0o100644

        if self._runtime.is_scope_or_ancestor_isolated(scope):
            if not already_performed:
                self._backend_write(scope, path, content, mode=mode)
            return DriverIngressResult()

        if self._file_exists_in_workspace(scope, path):
            effect_type = "FilePatch"
        else:
            effect_type = "FileCreate"

        ws_change: tuple[str, bytes | None] | tuple[str, bytes | None, int]
        if mode != 0o100644:
            ws_change = (path, content, mode)
        else:
            ws_change = (path, content)
        return DriverIngressResult(
            effects=(
                EffectRecord(
                    effect_type=effect_type,
                    metadata={"path": path},
                    workspace_changes=(ws_change,),
                ),
            )
        )

    def record_changes(self, changes: Sequence[WorkspaceChange], *, scope: ScopeInfo | None = None) -> list[str]:
        """Record file changes on the given scope, or the ambient scope."""
        scope = self._pipeline.require_world(scope)

        effects: list[EffectRecord] = []
        for change in changes:
            path = change[0]
            content = change[1]
            command = "delete" if content is None else "write"
            params: dict[str, Any] = {"path": path, "content": content}
            if len(change) > 2:
                params["mode"] = change[2]
            effects.extend(self.execute(command, scope, **params).effects)
        return self._pipeline.record_runtime_effects(
            effects,
            substrate="filesystem",
            scope=scope,
            boundary_policy="append_or_root",
            operation_kind="filesystem.record_changes",
            operation_label="filesystem-record-changes",
            operation_metadata={"change_count": len(changes)},
        )

    def record_read(self, path: str, *, scope: ScopeInfo | None = None) -> str:
        """Record a file read on the given scope, or the ambient scope."""
        scope = self._pipeline.require_world(scope)
        outcome = self.execute("read", scope, path=path)
        return self._pipeline.record_runtime_effect(
            outcome.effects[0],
            substrate="filesystem",
            scope=scope,
            boundary_policy="append_or_root",
            operation_kind="filesystem.record_read",
            operation_label=f"filesystem-read-{path}",
            operation_metadata={"path": path},
        )


DeclarativeFilesystemSubstrate = FilesystemSubstrate
