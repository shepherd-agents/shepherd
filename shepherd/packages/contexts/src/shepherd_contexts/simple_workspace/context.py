"""SimpleWorkspace: Non-git file workspace with effect-based state management.

This module provides SimpleWorkspace, a file workspace that uses
file manifests and changesets instead of git for state tracking.

Key features:
- No git dependency - works with any directory
- Manifest-based state tracking (stat-only scanning)
- Adaptive content encoding (diff-match-patch for text, zlib for binary)
- Full V2 protocol support (extract_effects + apply_effect)
- Copy-based sandboxing via CopySandbox

Use cases:
- Temporary directories (/tmp/scratch)
- Generated output folders
- Cloud storage mounts
- Container ephemeral storage
"""

from __future__ import annotations

import hashlib
import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from shepherd_core.effects import (
    MAX_CONTENT_SIZE,
    Effect,
    FileCreate,
    FilePatch,
    FileRead,
    truncate_with_hash,
)
from shepherd_core.types import (
    ExecutionResult,
    PreparationError,
    ProviderBinding,
    ProviderCapabilities,
    ReversibilityLevel,
    ToolCall,
    ValidationResult,
)
from shepherd_runtime.context import Bindable, Sandbox

from shepherd_contexts.simple_workspace.delta import FileChangeset, FileDelta
from shepherd_contexts.simple_workspace.effects import SimpleWorkspaceChangesetCaptured
from shepherd_contexts.simple_workspace.encoding import get_encoder
from shepherd_contexts.simple_workspace.manifest import FileEntry, FileManifest
from shepherd_contexts.simple_workspace.materializer import (
    SimpleWorkspaceMaterializationIntent,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from shepherd_runtime.cache import HashingScope
    from shepherd_runtime.materialization import MaterializationResult


class SimpleWorkspace(BaseModel, Bindable):
    """File workspace without git backing.

    Uses file manifests and changesets for state management.
    Suitable for temporary directories, generated output, etc.

    State Model:
        current_state = apply_changesets(base_manifest, pending_changesets)

    This follows the v2 architecture's core invariant:
        state(t) = fold(apply_effect, effects[0:t], initial_state)

    Context Classification: DERIVABLE
        - Full state derived from effects (base_manifest + pending_changesets)
        - Supports time-travel debugging
        - Supports trivial branching/speculation

    Attributes:
        path: Filesystem path to workspace
        base_manifest: Snapshot of initial file state
        pending_changesets: Accumulated changes from task executions
        capabilities: What operations are allowed (read, write, bash)
    """

    __binding_name__: ClassVar[str] = "workspace"

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    path: str
    base_manifest: FileManifest | None = None
    pending_changesets: tuple[FileChangeset, ...] = ()
    capabilities: frozenset[str] = Field(default_factory=lambda: frozenset({"read", "write"}))
    frozen_context_id: str | None = Field(default=None, alias="_frozen_context_id")

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> str:
        """Coerce Path objects to str for ergonomic API."""
        if isinstance(v, Path):
            return str(v)
        return v

    # === Identity ===

    @property
    def context_id(self) -> str:
        """Stable identity for effect attribution.

        The context_id is frozen at creation time and preserved across
        all mutations to maintain lineage correlation.
        """
        if self.frozen_context_id:
            return self.frozen_context_id
        manifest_hash = ""
        if self.base_manifest and self.base_manifest.entries:
            manifest_hash = hashlib.sha256(str(self.base_manifest.entries).encode()).hexdigest()[:8]
        return f"simple-workspace:{self.path}:{manifest_hash}"

    @computed_field
    @property
    def content_hash(self) -> str:
        """Content-addressable identity for caching.

        Two workspaces with the same content_hash have identical logical
        content regardless of filesystem location.

        Returns:
            12-character hex string (48 bits of entropy)
        """
        anchor = self._compute_manifest_anchor()

        changeset_hashes: list[str] = []
        for changeset in self.pending_changesets:
            if changeset.sha256:
                changeset_hashes.append(changeset.sha256)

        components = [anchor, *changeset_hashes]
        combined = "|".join(components)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]

    def _compute_manifest_anchor(self) -> str:
        """Compute deterministic anchor hash for manifest."""
        if self.base_manifest is None or not self.base_manifest.entries:
            return "empty"

        entries = []
        for entry in sorted(self.base_manifest.entries, key=lambda e: e.path):
            if entry.content_hash:
                content = entry.content_hash
            else:
                warnings.warn(
                    f"FileEntry '{entry.path}' missing content_hash, "
                    "using size fallback for content_hash computation. "
                    "Consider using FileManifest.from_directory(..., compute_hashes=True)",
                    UserWarning,
                    stacklevel=2,
                )
                content = f"size:{entry.size_bytes}"
            entries.append(f"{entry.path}|{content}|{entry.mode}")

        combined = "\n".join(entries)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    def content_equals(self, other: SimpleWorkspace) -> bool:
        """Check if two workspaces have identical logical content."""
        return self.content_hash == other.content_hash

    def state_hash(self, scope: HashingScope | None = None) -> str:
        """Compute hash for cache key computation.

        Combines content_hash + capabilities. The scope parameter is
        accepted for API compatibility but not used.

        Returns:
            16-character hex hash for cache key computation
        """
        caps_str = ",".join(sorted(self.capabilities))
        combined = f"{self.content_hash}|caps:{caps_str}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    @property
    def reversibility(self) -> ReversibilityLevel:
        """File operations are mechanically reversible via changesets."""
        return ReversibilityLevel.AUTO

    # === Capability Properties ===

    @property
    def can_read(self) -> bool:
        """Whether this workspace has read capability."""
        return "read" in self.capabilities

    @property
    def can_write(self) -> bool:
        """Whether this workspace has write capability."""
        return "write" in self.capabilities

    @property
    def can_bash(self) -> bool:
        """Whether this workspace has bash capability."""
        return "bash" in self.capabilities

    # === Factory Methods ===

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        capabilities: frozenset[str] | None = None,
        scan_manifest: bool = True,
    ) -> SimpleWorkspace:
        """Create workspace from existing directory.

        Args:
            path: Directory path
            capabilities: Allowed operations (default: read, write)
            scan_manifest: Whether to scan and create base manifest

        Returns:
            SimpleWorkspace bound to the directory
        """
        path_str = str(path)
        caps = capabilities or frozenset({"read", "write"})

        base_manifest = None
        if scan_manifest and Path(path_str).exists():
            base_manifest = FileManifest.from_directory(Path(path_str))

        # Generate frozen ID
        manifest_hash = ""
        if base_manifest and base_manifest.entries:
            manifest_hash = hashlib.sha256(str(base_manifest.entries).encode()).hexdigest()[:8]
        frozen_id = f"simple-workspace:{path_str}:{manifest_hash}"

        return cls(
            path=path_str,
            base_manifest=base_manifest,
            capabilities=caps,
            frozen_context_id=frozen_id,
        )

    @classmethod
    def empty(cls, path: str | Path) -> SimpleWorkspace:
        """Create workspace for a new/empty directory.

        Args:
            path: Directory path (will be created if it doesn't exist)

        Returns:
            SimpleWorkspace with empty base manifest
        """
        path_str = str(path)
        Path(path_str).mkdir(parents=True, exist_ok=True)

        frozen_id = f"simple-workspace:{path_str}:empty"
        return cls(
            path=path_str,
            base_manifest=FileManifest(entries=()),
            capabilities=frozenset({"read", "write"}),
            frozen_context_id=frozen_id,
        )

    @classmethod
    def readonly(cls, path: str | Path) -> SimpleWorkspace:
        """Create read-only workspace.

        Args:
            path: Directory path

        Returns:
            SimpleWorkspace with read-only capabilities
        """
        return cls.from_path(path, capabilities=frozenset({"read"}))

    # === State Derivation ===

    def current_manifest(self) -> FileManifest:
        """Derive current file manifest from base + changesets.

        Applies all pending changesets to base_manifest to compute
        the expected current state. Uses changeset.created_at for
        deterministic mtime values.

        Returns:
            FileManifest representing expected current state
        """
        if self.base_manifest is None:
            return FileManifest(entries=())

        if not self.pending_changesets:
            return self.base_manifest

        # Build mutable dict from base entries
        entries: dict[str, FileEntry] = {e.path: e for e in self.base_manifest.entries}

        # Apply each changeset in order
        for changeset in self.pending_changesets:
            # Use changeset timestamp for deterministic mtime
            changeset_mtime_ns = int(changeset.created_at.timestamp() * 1e9)

            for delta in changeset.deltas:
                if delta.operation == "delete":
                    entries.pop(delta.path, None)
                elif delta.operation in ("create", "modify"):
                    entries[delta.path] = FileEntry(
                        path=delta.path,
                        size_bytes=delta.new_size_bytes or 0,
                        mtime_ns=changeset_mtime_ns,
                        mode=delta.new_mode or 0o644,
                        content_hash=delta.new_content_hash,
                    )

        return FileManifest(entries=tuple(sorted(entries.values(), key=lambda e: e.path)))

    @property
    def has_pending_changes(self) -> bool:
        """Whether there are changesets to materialize."""
        return len(self.pending_changesets) > 0

    def materialization_intent(self) -> SimpleWorkspaceMaterializationIntent:
        """Return intent describing what to materialize (PURE).

        Returns:
            Intent with changesets to apply to filesystem
        """
        return SimpleWorkspaceMaterializationIntent(
            context_id=self.context_id,
            target_path=Path(self.path),
            changesets=self.pending_changesets,
        )

    def with_materialized(self, result: MaterializationResult) -> Self:
        """Return context with cleared changesets and updated manifest (PURE).

        After materialization, recalculates manifest from current filesystem
        state to establish new base for future changesets.

        Args:
            result: The materialization result

        Returns:
            New SimpleWorkspace with cleared changesets and updated manifest
        """
        # After materialization, recalculate manifest from current filesystem
        new_manifest = self.current_manifest()
        return self.model_copy(
            update={
                "pending_changesets": (),
                "base_manifest": new_manifest,
            }
        )

    # === V1 Protocol: configure ===

    def configure(self, capabilities: ProviderCapabilities | None = None) -> ProviderBinding:
        """Return provider binding configuration.

        Pure method - no side effects. Same inputs always produce same outputs.
        """
        blocked = self._blocked_tools(self.capabilities)
        description = self._build_description(self.capabilities)

        # Handle frozenset for backward compatibility
        if isinstance(capabilities, frozenset):
            capabilities = None

        # Check if provider supports tools
        if capabilities and not capabilities.supports_tools:
            return ProviderBinding(
                context_id=self.context_id,
                context_type="SimpleWorkspace",
                context_description=description + "\n\n(Tools unavailable)",
                cwd=self.path,
            )

        # Translate capabilities to trust level
        trust = "standard" if "write" in self.capabilities else "restricted"

        return ProviderBinding(
            context_id=self.context_id,
            context_type="SimpleWorkspace",
            context_description=description,
            capabilities=self.capabilities,
            blocked_tools=blocked,
            validate_tool=self._make_validator(self.capabilities, blocked),
            cwd=self.path,
            trust_level=trust,
        )

    def _blocked_tools(self, caps: frozenset[str]) -> frozenset[str]:
        """Compute blocked tools from capabilities."""
        blocked: set[str] = set()
        if "write" not in caps:
            blocked.update({"Write", "Edit", "NotebookEdit"})
        if "bash" not in caps:
            blocked.add("Bash")
        return frozenset(blocked)

    def _build_description(self, caps: frozenset[str]) -> str:
        """Build human-readable context description."""
        access = sorted(caps)
        desc = f"Workspace at `{self.path}` ({', '.join(access)} access)"
        if self.pending_changesets:
            desc += f"\n{len(self.pending_changesets)} pending changesets."
        return desc

    def _make_validator(self, caps: frozenset[str], blocked: frozenset[str]) -> Callable[[ToolCall], ValidationResult]:
        """Create a validator closure that captures capabilities."""
        workspace = self

        def validate(tool: ToolCall) -> ValidationResult:
            # Check blocked tools
            if tool.name in blocked:
                missing_cap = "write" if tool.name in {"Write", "Edit", "NotebookEdit"} else "bash"
                return ValidationResult.reject(tool, f"Tool '{tool.name}' requires '{missing_cap}' capability")

            # Validate file paths are within workspace
            if tool.name in {"Read", "Write", "Edit"}:
                file_path = tool.params.get("file_path", "")
                if file_path and not workspace._is_within_workspace(file_path):
                    return ValidationResult.reject(tool, f"Path '{file_path}' is outside workspace")

            return ValidationResult.allow(tool)

        return validate

    def _is_within_workspace(self, path: str) -> bool:
        """Check if path is within workspace."""
        abs_path = os.path.abspath(path)
        abs_workspace = os.path.abspath(self.path)
        return abs_path.startswith(abs_workspace)

    # === V1 Protocol: prepare ===

    def prepare(self) -> Self:
        """Validate workspace path exists.

        For SimpleWorkspace, preparation is minimal since we use
        copy-based sandboxing (the sandbox handles setup).
        """
        if not Path(self.path).exists():
            raise PreparationError(self.context_id, f"Workspace path does not exist: {self.path}")
        return self

    # === V2 Protocol: extract_effects ===

    def extract_effects(
        self,
        sandbox: Sandbox | None,
        result: ExecutionResult,
    ) -> Sequence[Effect]:
        """Extract effects by comparing sandbox state to expected state.

        V2 Protocol: sandbox may be None if no filesystem isolation was used.
        In that case, return empty list (no filesystem effects to capture).

        Args:
            sandbox: CopySandbox with execution results
            result: ExecutionResult from provider

        Returns:
            Sequence of effects including SimpleWorkspaceChangesetCaptured
        """
        if sandbox is None:
            return []

        effects: list[Effect] = []
        encoder = get_encoder()

        # Generate semantic effects from tool calls first
        for call, res in zip(result.tool_calls, result.tool_results, strict=False):
            if not res.success:
                continue

            if call.name == "Read":
                # Extract content from tool result (empty for binary files)
                raw_content = res.output if isinstance(res.output, str) else ""
                content_hash = ""
                content_truncated = False

                if raw_content:
                    # Apply truncation for large files (>1MB)
                    if len(raw_content) > MAX_CONTENT_SIZE:
                        raw_content, content_hash, content_truncated = truncate_with_hash(raw_content)
                    else:
                        content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()

                effects.append(
                    FileRead(
                        path=call.params.get("file_path", ""),
                        context_id=self.context_id,
                        content=raw_content,
                        content_hash=content_hash,
                        content_truncated=content_truncated,
                    )
                )
            elif call.name == "Write":
                effects.append(
                    FileCreate(
                        path=call.params.get("file_path", ""),
                        context_id=self.context_id,
                    )
                )
            elif call.name == "Edit":
                effects.append(
                    FilePatch(
                        path=call.params.get("file_path", ""),
                        context_id=self.context_id,
                    )
                )

        # Compare sandbox state to expected state
        expected = self.current_manifest()
        actual = FileManifest.from_directory(sandbox.path)

        added, modified, removed = expected.detect_changes(actual)

        # Build deltas for all changes
        deltas: list[FileDelta] = []

        # Handle new files
        for rel_path in added:
            full_path = sandbox.path / rel_path
            try:
                content = full_path.read_bytes()
                stat = full_path.stat()
                deltas.append(FileDelta.create(rel_path, content, mode=stat.st_mode & 0o777, encoder=encoder))
            except OSError:
                continue

        # Handle deleted files
        for rel_path in removed:
            old_entry = expected.get(rel_path)
            deltas.append(FileDelta.delete(rel_path, old_entry.content_hash if old_entry else None))

        # Handle modified files
        for rel_path in modified:
            old_content = self._get_file_content(rel_path)
            new_path = sandbox.path / rel_path
            try:
                new_content = new_path.read_bytes()
                if old_content is not None:
                    deltas.append(FileDelta.modify(rel_path, old_content, new_content, encoder=encoder))
                else:
                    # Can't compute delta, treat as create
                    deltas.append(FileDelta.create(rel_path, new_content, encoder=encoder))
            except OSError:
                continue

        # Create changeset if there are changes
        if deltas:
            changeset = FileChangeset(
                deltas=tuple(deltas),
                source_step=result.metadata.get("task_name"),
            )
            effects.append(
                SimpleWorkspaceChangesetCaptured(
                    context_id=self.context_id,
                    changeset=changeset,
                )
            )

        return effects

    def _get_file_content(self, rel_path: str) -> bytes | None:
        """Get content of file at path, considering pending changesets.

        First checks if content is available in pending changesets,
        then falls back to reading from base filesystem.
        """
        # Check pending changesets for this file (newest first)
        for changeset in reversed(self.pending_changesets):
            for delta in changeset.deltas:
                if delta.path == rel_path:
                    if delta.operation == "delete":
                        return None
                    if delta.content is not None:
                        return delta.decode_content()

        # Fall back to base filesystem
        full_path = Path(self.path) / rel_path
        if full_path.exists():
            try:
                return full_path.read_bytes()
            except OSError:
                return None
        return None

    # === V2 Protocol: apply_effect ===

    def apply_effect(self, effect: Effect) -> Self:
        """Apply effect to derive new state.

        Pure function - returns new context instance.

        Note: We do NOT filter by context_id here. The scope routes effects to us
        by binding_name (stable), so we trust that we only receive effects intended
        for this context. This is essential for cache replay, where the context_id
        may differ from the original execution. See SessionState for the canonical pattern.

        Args:
            effect: Effect to apply

        Returns:
            New SimpleWorkspace with updated state
        """
        if isinstance(effect, SimpleWorkspaceChangesetCaptured) and effect.changeset:
            return self.model_copy(
                update={
                    "pending_changesets": (*self.pending_changesets, effect.changeset),
                }
            )
        return self

    # === Cleanup ===

    def cleanup(self, error: Exception | None = None) -> None:
        """No cleanup needed - sandbox handles its own cleanup."""

    @classmethod
    def requires_sandbox(cls) -> bool:
        """SimpleWorkspace requires CopySandbox for filesystem isolation."""
        return True


__all__ = ["SimpleWorkspace"]
