"""WorkspaceRef: Git-backed workspace with capability-based access control.

This is the most complex reference implementation, demonstrating:
- Stateful preparation (git checkout, patch application)
- Stateful capture (git diff)
- Capability-based tool restrictions
- Frozen context_id for lineage correlation
- Domain effect generation from tool calls

v2 API:
- extract_effects(sandbox, result): Extract effects from sandbox/result (PURE)
- apply_effect(effect): Derive new state from effect (PURE)

The v2 API enables:
- Time-travel debugging (reconstruct state by replaying effects)
- Speculative execution (fork, run, approve/reject)
- Effect-sourced state derivation
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from shepherd_core.effects import (
    MAX_CONTENT_SIZE,
    DiffPatch,
    Effect,
    FileCreate,
    FilePatch,
    FileRead,
    truncate_with_hash,
)
from shepherd_core.foundation.protocols.device import ContextStateBase
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

from shepherd_contexts.workspace.effects import WorkspacePatchCaptured

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from shepherd_runtime.cache import HashingScope
    from shepherd_runtime.device.transfer import TransferBundle
    from shepherd_runtime.materialization import MaterializationResult
    from shepherd_runtime.scope_types import TransferScope

    from .materializer import WorkspaceMaterializationIntent


# =============================================================================
# WorkspaceState (for container serialization)
# =============================================================================


@dataclass(frozen=True)
class WorkspaceState(ContextStateBase):
    """Serializable state for WorkspaceRef transfer across device boundaries.

    This dataclass captures everything needed to reconstruct a WorkspaceRef
    inside a container sandbox. The rebind() method handles path translation
    from host to container filesystem.

    Attributes:
        path: Workspace path (will be rebound for container).
        base_commit: Git commit SHA anchor point.
        pending_patches: Accumulated patches as serialized dicts.
        capabilities: Available capabilities (read, write, bash).
        frozen_context_id: Original context_id for lineage tracking.
    """

    path: str = ""
    base_commit: str = ""
    pending_patches: tuple[dict[str, Any], ...] = ()
    capabilities: frozenset[str] = frozenset({"read", "write"})
    frozen_context_id: str | None = None

    @property
    def context_type(self) -> str:
        """Type discriminator for deserialization."""
        return "workspace"

    def rebind(self, env: Mapping[str, str]) -> WorkspaceState:
        """Return state with path rebound for container environment.

        Args:
            env: Environment variables with WORKSPACE_PATH mapping.

        Returns:
            New state with updated path.
        """
        new_path = env.get("WORKSPACE_PATH", self.path)
        return replace(self, path=new_path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceState:
        """Deserialize from dictionary.

        Args:
            data: Dictionary with state fields.

        Returns:
            WorkspaceState instance.
        """
        return cls(
            path=data.get("path", ""),
            base_commit=data.get("base_commit", ""),
            pending_patches=tuple(data.get("pending_patches", ())),
            capabilities=frozenset(data.get("capabilities", {"read", "write"})),
            frozen_context_id=data.get("frozen_context_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation for JSON serialization.
        """
        return {
            "context_type": self.context_type,
            "path": self.path,
            "base_commit": self.base_commit,
            "pending_patches": list(self.pending_patches),
            "capabilities": list(self.capabilities),
            "frozen_context_id": self.frozen_context_id,
        }


# =============================================================================
# WorkspaceRef
# =============================================================================


class WorkspaceRef(BaseModel, Bindable):
    """Git-backed workspace with capability-based access control.

    Uses Pydantic BaseModel for validation and immutability.
    Inherits Bindable for fluent scope binding via .bind().

    IMPORTANT: Always use WorkspaceRef.from_path() to create instances.
    Direct construction requires a valid 40-character SHA for base_commit.

    Uses the "patch accumulation with index checkpoint" approach:
    - base_commit is the anchor point in git history (must be full 40-char SHA)
    - pending_patches are accumulated changes from task executions
    - No temporary commits pollute git history

    The workspace can be reconstructed by:
    1. Resetting to base_commit
    2. Applying pending_patches in order

    Capabilities:
        - "read": Can read files (always present)
        - "write": Can create/modify/delete files
        - "bash": Can execute bash commands

    Lifecycle:
        configure(): Returns binding with capabilities and tool restrictions
        prepare(): Checkout base, apply patches, stage checkpoint
        extract_effects(): Capture git diff, generate effects
        apply_effect(): Apply patch effects to accumulated state
        cleanup(): No-op (git state is persistent)
    """

    __binding_name__: ClassVar[str] = "workspace"

    # frozen=True: Immutable after creation
    # extra="forbid": Reject unknown fields (catches typos)
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Fields ---
    path: str  # Accepts Path objects (coerced to str via field_validator)
    base_commit: str  # No default - must be explicit 40-char SHA
    pending_patches: tuple[DiffPatch, ...] = ()
    capabilities: frozenset[str] = Field(default_factory=lambda: frozenset({"read", "write"}))
    frozen_context_id: str | None = None  # No underscore prefix (serialization-safe)

    # --- Validators ---

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, v: Any) -> str:
        """Coerce Path objects to str for ergonomic API.

        This allows users to write:
            WorkspaceRef(path=Path("/repo"), base_commit=...)
        instead of:
            WorkspaceRef(path=str(Path("/repo")), base_commit=...)
        """
        if isinstance(v, Path):
            return str(v)
        return v

    @field_validator("base_commit")
    @classmethod
    def _validate_base_commit(cls, v: str) -> str:
        """Ensure base_commit is a full 40-character SHA.

        Symbolic refs like 'HEAD' or 'main' are rejected because they
        change over time, which would break content_hash stability.

        Use WorkspaceRef.from_path() which resolves HEAD automatically.
        """
        if not re.match(r"^[0-9a-f]{40}$", v):
            raise ValueError(
                f"base_commit must be a full 40-char SHA, got: {v!r}. "
                f"Use WorkspaceRef.from_path() to resolve HEAD automatically."
            )
        return v

    # --- Computed Fields ---

    @computed_field
    @property
    def content_hash(self) -> str:
        """Content-addressable identity for caching.

        Two workspaces with the same content_hash have identical logical
        content regardless of filesystem location.

        Implementation Notes:
        - Uses @computed_field + @property (NOT @model_validator + PrivateAttr)
        - This is critical because model_copy() does NOT trigger @model_validator
        - The property recomputes on each access, which is correct behavior
        - Cost is O(n) where n = len(pending_patches), just string joins + one SHA-256
        - Each DiffPatch.sha256 is precomputed, so we just concatenate them

        Returns:
            12-character hex string (48 bits of entropy)
        """
        # Collect patch hashes in order
        patch_hashes: list[str] = []
        for patch in self.pending_patches:
            if patch.sha256:
                patch_hashes.append(patch.sha256)
            elif patch.patch.strip():
                # Fallback: compute on the fly (shouldn't happen with DiffPatch validator)
                patch_hashes.append(hashlib.sha256(patch.patch.encode("utf-8")).hexdigest())
            # Skip empty patches

        # Combine: base_commit | patch1_hash | patch2_hash | ...
        # base_commit is guaranteed to be a 40-char SHA (validated above)
        components = [self.base_commit, *patch_hashes]

        combined = "|".join(components)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]

    # --- Content Comparison ---

    def content_equals(self, other: WorkspaceRef) -> bool:
        """Check if two workspaces have identical logical content.

        This compares content identity (base_commit + patches), ignoring
        filesystem location. Use this for cache deduplication scenarios.

        For resource identity (same filesystem location), use standard
        equality (==) which compares all fields including path.
        """
        return self.content_hash == other.content_hash

    def state_hash(self, scope: HashingScope | None = None) -> str:
        """Compute hash for cache key computation.

        Combines:
        - content_hash: Logical content identity (base_commit + patches)
        - capabilities: Affect LLM behavior (read-only vs writable)

        The scope parameter is accepted for API compatibility with the
        cache system but not used, since WorkspaceRef's content is fully
        captured by pending_patches (no working tree state to consider).

        This method enables cross-sandbox cache hits: two workspaces with
        identical content and capabilities will produce the same state_hash
        regardless of filesystem location.

        Args:
            scope: HashingScope (ignored - patches capture all state)

        Returns:
            16-character hex hash for cache key computation
        """
        caps_str = ",".join(sorted(self.capabilities))
        combined = f"{self.content_hash}|caps:{caps_str}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    # --- Other Properties ---

    @property
    def context_id(self) -> str:
        """Stable identity for effect attribution.

        The context_id is frozen at creation time and preserved across
        all mutations to maintain lineage correlation.
        """
        if self.frozen_context_id:
            return self.frozen_context_id
        # base_commit is guaranteed to be 40-char SHA
        return f"workspace:{self.path}:{self.base_commit[:8]}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        """Git operations are mechanically reversible."""
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

    def __str__(self) -> str:
        """Empty string = invisible in prompts."""
        return ""

    def __repr__(self) -> str:
        # Simplified: base_commit is always a valid 40-char SHA after validation
        short = self.base_commit[:8]
        return f"WorkspaceRef({Path(self.path).name}@{short}+{len(self.pending_patches)})"

    # === Factory Methods ===

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        branch: str | None = None,
    ) -> WorkspaceRef:
        """Create a WorkspaceRef from a git repository path.

        This is the primary way to create WorkspaceRef instances.
        Resolves HEAD to a full SHA for content_hash stability.

        Args:
            path: Path to a git repository (must contain .git directory)
            branch: Reserved for future use (checkout specific branch)

        Raises:
            ValueError: If path is not a git repository or HEAD cannot be resolved.
                The error message provides actionable guidance.

        Example:
            >>> ws = WorkspaceRef.from_path("/path/to/repo")
            >>> ws.base_commit  # Full 40-char SHA, e.g., "a1b2c3..."
        """
        path = str(path)

        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                capture_output=True,
                text=True,
                check=True,
            )
            base_commit = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            # Provide actionable error message with context
            stderr = e.stderr.strip() if e.stderr else ""
            raise ValueError(
                f"Cannot resolve HEAD in '{path}'. "
                f"Ensure this is a git repository with at least one commit.\n"
                f"  - Check: Does '{path}/.git' exist?\n"
                f"  - Check: Has the repository been initialized with 'git init'?\n"
                f"  - Check: Does the repository have at least one commit?\n"
                f"Git error: {stderr or str(e)}"
            ) from e
        except FileNotFoundError as e:
            # Could be: (1) path doesn't exist, or (2) git binary not found
            if not Path(path).exists():
                raise ValueError(f"Path '{path}' does not exist. Provide a valid path to a git repository.") from None
            # git binary not installed or not in PATH
            raise ValueError(
                "Git command not found. Ensure git is installed and in PATH.\n"
                "  - On macOS: Install Xcode Command Line Tools or use Homebrew\n"
                "  - On Linux: Install via package manager (apt, yum, etc.)\n"
                "  - On Windows: Install Git for Windows"
            ) from e
        except PermissionError as e:
            raise ValueError(f"Permission denied accessing '{path}'. Check file permissions and try again.") from e

        frozen_id = f"workspace:{path}:{base_commit[:8]}"
        return cls(
            path=path,
            base_commit=base_commit,
            frozen_context_id=frozen_id,
        )

    @classmethod
    def readonly(cls, path: str | Path) -> WorkspaceRef:
        """Create a read-only workspace."""
        ref = cls.from_path(path)
        return ref.model_copy(update={"capabilities": frozenset({"read"})})

    @classmethod
    def writable(cls, path: str | Path) -> WorkspaceRef:
        """Create a writable workspace (read + write, no bash)."""
        ref = cls.from_path(path)
        return ref.model_copy(update={"capabilities": frozenset({"read", "write"})})

    @classmethod
    def from_serialized(cls, data: dict[str, Any]) -> Self:
        """Deserialize from model_dump() output.

        This convenience method handles the interaction between extra="forbid"
        and @computed_field. The content_hash field is automatically excluded
        since it's recomputed from base_commit + pending_patches.

        Use this instead of model_validate() when round-tripping through
        model_dump():

            # Instead of this (fails due to extra="forbid"):
            ws2 = WorkspaceRef.model_validate(ws1.model_dump())

            # Do this:
            ws2 = WorkspaceRef.from_serialized(ws1.model_dump())

        Args:
            data: Dictionary from model_dump() or equivalent

        Returns:
            New WorkspaceRef instance with content_hash recomputed
        """
        # Exclude computed fields that would be rejected by extra="forbid"
        filtered = {k: v for k, v in data.items() if k != "content_hash"}
        return cls.model_validate(filtered)

    def with_write(self) -> WorkspaceRef:
        """Return workspace with write capability added."""
        return self.model_copy(update={"capabilities": self.capabilities | {"write"}})

    def with_bash(self) -> WorkspaceRef:
        """Return workspace with bash capability added."""
        return self.model_copy(update={"capabilities": self.capabilities | {"bash"}})

    def without_bash(self) -> WorkspaceRef:
        """Return workspace with bash capability removed."""
        return self.model_copy(update={"capabilities": self.capabilities - {"bash"}})

    def with_capabilities(self, *caps: str) -> WorkspaceRef:
        """Return workspace with additional capabilities added."""
        return self.model_copy(update={"capabilities": self.capabilities | set(caps)})

    def without_capabilities(self, *caps: str) -> WorkspaceRef:
        """Return workspace with capabilities removed."""
        return self.model_copy(update={"capabilities": self.capabilities - set(caps)})

    # === State Serialization (for container transfer) ===

    def to_state(self) -> WorkspaceState:
        """Serialize workspace to transportable state.

        Creates a WorkspaceState that can be serialized to JSON and
        transferred across device boundaries. The state captures
        everything needed to reconstruct the workspace in a sandbox.

        Returns:
            WorkspaceState with serialized patches and capabilities.
        """
        # Serialize DiffPatch objects to dicts
        serialized_patches = tuple(patch.model_dump() for patch in self.pending_patches)

        return WorkspaceState(
            path=self.path,
            base_commit=self.base_commit,
            pending_patches=serialized_patches,
            capabilities=self.capabilities,
            frozen_context_id=self.frozen_context_id,
        )

    @classmethod
    def from_state(
        cls,
        state: WorkspaceState,
        sandbox_path: Path | str | None = None,
    ) -> WorkspaceRef:
        """Reconstruct workspace from state.

        Creates a WorkspaceRef from a deserialized WorkspaceState.
        If sandbox_path is provided, it overrides the path in state
        (useful when the container mounts workspace at different location).

        Args:
            state: WorkspaceState from deserialization.
            sandbox_path: Optional override for workspace path.

        Returns:
            WorkspaceRef instance.
        """
        # Use sandbox_path if provided, otherwise state.path
        path = str(sandbox_path) if sandbox_path else state.path

        # Reconstruct DiffPatch objects from dicts
        patches = tuple(DiffPatch.model_validate(p) for p in state.pending_patches)

        return cls(
            path=path,
            base_commit=state.base_commit,
            pending_patches=patches,
            capabilities=state.capabilities,
            frozen_context_id=state.frozen_context_id,
        )

    def transfer_bundle(self, scope: TransferScope) -> TransferBundle | None:
        """Create transfer bundle with visible patches for device transfer.

        Called when entering a Device context. The bundle contains patches
        accumulated locally that need to be visible inside the container.

        Args:
            scope: The scope containing effect stream and context bindings.

        Returns:
            TransferBundle with patch files and manifest for effect attribution,
            or None if no patches to transfer.
        """
        from shepherd_runtime.device.transfer import TransferBundle, collect_visible_patches

        # Collect patches using "visible" strategy
        visible_patches = collect_visible_patches(scope, binding_name="workspace")

        if not visible_patches:
            return None

        # Build files dict and manifest
        files: dict[str, bytes] = {}
        manifest: dict[str, str] = {}

        for i, patch in enumerate(visible_patches):
            # Store patch file for git apply
            patch_key = f".shepherd/patches/{i:04d}.diff"
            files[patch_key] = patch.patch.encode("utf-8")

            # Track files for effect attribution
            for filename in patch.files_changed:
                manifest[filename] = patch.sha256 or ""

        # Same-path mounting for SDK compatibility
        host_path = str(Path(self.path).resolve())

        return TransferBundle(
            state={
                "context_type": "workspace",
                "base_commit": self.base_commit,
                "capabilities": list(self.capabilities),
                "frozen_context_id": self.frozen_context_id,
                "patch_count": len(visible_patches),
            },
            files=files,
            env={
                "SHEPHERD_WORKSPACE_PATH": host_path,
                "SHEPHERD_PATCH_DIR": f"{host_path}/.shepherd/patches",
            },
            mounts={host_path: host_path},
            symlinks={},
            manifest=manifest,
        )

    # === Configuration ===

    def configure(
        self,
        capabilities: ProviderCapabilities | frozenset[str] | None = None,
    ) -> ProviderBinding:
        """Return provider configuration. Pure, no side effects.

        Args:
            capabilities: Provider capabilities (ProviderCapabilities) or None.
                         Also accepts frozenset for backward compatibility.
        """
        blocked = self._blocked_tools(self.capabilities)
        description = self._build_description(self.capabilities)

        # Handle frozenset for backward compatibility (just ignore it)
        if isinstance(capabilities, frozenset):
            capabilities = None

        # Check if provider supports tools
        if capabilities and not capabilities.supports_tools:
            return ProviderBinding(
                context_id=self.context_id,
                context_type="WorkspaceRef",
                context_description=description + "\n\n(Tools unavailable - describe changes)",
                cwd=self.path,
            )

        # Translate capabilities to abstract trust level
        # write capability -> standard trust (provider may auto-approve edits)
        # read-only -> restricted trust (provider uses default/ask mode)
        trust = "standard" if "write" in self.capabilities else "restricted"

        return ProviderBinding(
            context_id=self.context_id,
            context_type="WorkspaceRef",
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
        desc = f"Git workspace at `{self.path}` ({', '.join(access)} access)"
        if self.pending_patches:
            desc += f"\n{len(self.pending_patches)} pending patches from prior steps."
        return desc

    def _make_validator(self, caps: frozenset[str], blocked: frozenset[str]) -> Callable[[ToolCall], ValidationResult]:
        """Create a validator closure that captures capabilities."""
        workspace = self  # Capture self for path validation

        def validate(tool: ToolCall) -> ValidationResult:
            # Check blocked tools (based on capabilities)
            if tool.name in blocked:
                missing_cap = "write" if tool.name in {"Write", "Edit", "NotebookEdit"} else "bash"
                return ValidationResult.reject(tool, f"Tool '{tool.name}' requires '{missing_cap}' capability")

            # Block dangerous git commands
            if tool.name == "Bash":
                cmd = tool.params.get("command", "")
                dangerous = ["git push", "git reset --hard", "git clean -"]
                for d in dangerous:
                    if d in cmd:
                        return ValidationResult.reject(tool, f"Dangerous git command blocked: {d}")

            # Validate file paths are within workspace
            if tool.name in {"Read", "Write", "Edit"}:
                file_path = tool.params.get("file_path", "")
                if file_path and not workspace._is_within_workspace(file_path):
                    return ValidationResult.reject(tool, f"Path '{file_path}' is outside workspace")

            return ValidationResult.allow(tool)

        return validate

    def _is_within_workspace(self, path: str) -> bool:
        """Check if path is within workspace."""
        import os

        abs_path = os.path.abspath(path)
        abs_workspace = os.path.abspath(self.path)
        return abs_path.startswith(abs_workspace)

    # === Preparation ===

    def prepare(self) -> Self:
        """Validate workspace path exists.

        This method performs minimal validation only. Actual workspace
        setup (checkout, patch application) happens in GitWorktreeSandbox.
        See PROPOSAL-context-sandbox-architecture.md for the pattern.
        """
        if not Path(self.path).exists():
            raise PreparationError(self.context_id, f"Workspace path does not exist: {self.path}")
        return self

    # === Cleanup ===

    def cleanup(self, error: Exception | None = None) -> None:
        """No cleanup needed - git state is persistent."""

    @classmethod
    def requires_sandbox(cls) -> bool:
        """WorkspaceRef requires GitWorktreeSandbox for filesystem isolation."""
        return True

    # === Materialization (v2 API) ===

    @property
    def has_pending_changes(self) -> bool:
        """Whether there are patches to materialize."""
        return len(self.pending_patches) > 0

    def materialization_intent(self) -> WorkspaceMaterializationIntent:
        """Return intent describing what to materialize.

        This method is PURE - it only returns data describing
        what patches need to be applied to the real filesystem.

        Includes expected_base_commit for drift detection - if the
        repository HEAD has changed since we captured effects,
        materialization will fail to prevent inconsistent state.
        """
        from shepherd_contexts.workspace.materializer import (
            WorkspaceMaterializationIntent,
        )

        return WorkspaceMaterializationIntent(
            context_id=self.context_id,
            target_path=Path(self.path),
            patches=self.pending_patches,
            expected_base_commit=self.base_commit,
        )

    def with_materialized(self, result: MaterializationResult) -> Self:
        """Return new context reflecting post-materialization state.

        After successful materialization:
        - pending_patches is cleared
        - base_commit is updated to the new commit (if one was made)

        This method is PURE - it returns a new immutable context.

        Important: The commit_sha in result.metadata MUST be a valid 40-char SHA.
        The @field_validator on base_commit will reject invalid values.
        """
        if not result.success:
            return self

        new_commit = result.metadata.get("commit_sha", self.base_commit)
        return self.model_copy(
            update={"pending_patches": (), "base_commit": new_commit},
        )

    # === v2 API: Effect-Driven State Derivation ===

    def extract_effects(
        self,
        sandbox: Sandbox | None,
        result: ExecutionResult,
    ) -> Sequence[Effect]:
        """Extract effects from sandbox and/or result. PURE.

        This method extracts:
        1. File operation effects from tool calls (FileRead, FileCreate, FilePatch)
        2. WorkspacePatchCaptured from sandbox git diff (if sandbox provided)

        Args:
            sandbox: Optional sandbox with git diff capability
            result: Execution result with tool calls

        Returns:
            Sequence of effects (not yet attributed - lifecycle adds attribution)
        """
        effects: list[Effect] = []

        # Extract file operation effects from tool calls
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

        # Extract git diff from sandbox (if available)
        if sandbox is not None:
            diff_content = sandbox.git_diff()
            if diff_content.strip():
                files = tuple(sandbox.changed_files())
                task_name = result.metadata.get("task_name")
                patch = DiffPatch.from_diff(diff_content, files, task_name)
                effects.append(
                    WorkspacePatchCaptured(
                        context_id=self.context_id,
                        files_changed=files,
                        patch_hash=patch.sha256 or "",
                        patch_size_bytes=len(patch.patch),
                        patch=patch,  # Full patch data for state derivation
                    )
                )
        else:
            # No sandbox - simulate patch from write operations (sketch mode)
            write_tools = [c for c in result.tool_calls if c.name in {"Write", "Edit"}]
            if write_tools:
                files = tuple(c.params.get("file_path", "") for c in write_tools)
                mock_patch = DiffPatch.from_diff(
                    "mock diff content",
                    files,
                    result.metadata.get("task_name"),
                )
                effects.append(
                    WorkspacePatchCaptured(
                        context_id=self.context_id,
                        files_changed=files,
                        patch_hash=mock_patch.sha256 or "",
                        patch_size_bytes=len(mock_patch.patch),
                        patch=mock_patch,  # Full patch data
                    )
                )

        return effects

    def apply_effect(self, effect: Effect) -> Self:
        """Apply effect to derive new context state. PURE.

        Handles:
        - WorkspacePatchCaptured: Adds patch to pending_patches

        Other effects are ignored (they don't affect workspace state).

        Note: We do NOT filter by context_id here. The scope routes effects to us
        by binding_name (stable), so we trust that we only receive effects intended
        for this context. This is essential for cache replay, where the context_id
        may differ from the original execution (different sandbox path, machine, etc.).
        See SessionState for the canonical pattern.

        Args:
            effect: Effect to apply

        Returns:
            New WorkspaceRef instance (or self if no state change)
        """
        # Handle WorkspacePatchCaptured
        if isinstance(effect, WorkspacePatchCaptured):
            patch = effect.patch
            if patch and patch.patch:  # Has actual content
                return self.model_copy(
                    update={"pending_patches": (*self.pending_patches, patch)},
                )

        return self


__all__ = ["WorkspaceRef", "WorkspaceState"]


# =============================================================================
# Context Registry Registration
# =============================================================================


def _register_deserializer() -> None:
    """Register WorkspaceState deserializer with context registry.

    Called at module import time. Wrapped in function to allow
    graceful handling if device module not yet available.
    """
    try:
        from shepherd_runtime.registry import register_context_deserializer

        register_context_deserializer("workspace", WorkspaceState.from_dict)
    except ImportError:
        # Device module may not be installed - that's OK
        pass


_register_deserializer()
