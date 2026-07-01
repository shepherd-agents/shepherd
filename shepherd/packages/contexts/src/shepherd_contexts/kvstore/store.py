"""KVStoreContext: A simple key-value store execution context.

This module provides a non-coding execution context to validate that the
ExecutionContext pattern is truly domain-agnostic. It demonstrates:

1. Full ExecutionContext protocol implementation
2. Custom context_id format (kvstore:{hash})
3. Reversibility declaration (AUTO - in-memory is mechanically reversible)
4. Change detection between prepare and capture
5. Proper lifecycle management

This is intentionally simple to serve as a reference implementation for
users creating their own domain-specific contexts.

Example:
    from shepherd_runtime.scope import Scope
    from shepherd_runtime.task.authoring import Context, task
    from shepherd_contexts.kvstore import KVStoreContext
    from pydantic import BaseModel

    # Bind returns a ContextRef that auto-updates as effects are applied
    with Scope() as scope:
        config = scope.bind("config", KVStoreContext.create({"env": "dev"}))

        @task
        class UpdateConfig(BaseModel):
            config: Context(KVStoreContext)
            # ... task definition ...

        result = UpdateConfig()

        # ContextRef reflects the updated state automatically
        print(config.data)  # Shows updated data, not stale snapshot
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, PrivateAttr
from shepherd_core.types import (
    ExecutionResult,
    ProviderBinding,
    ProviderCapabilities,
    ReversibilityLevel,
)
from shepherd_runtime.context import Bindable

from shepherd_contexts.kvstore.effects import KeyDeleted, KeySet

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shepherd_core.effects import Effect
    from shepherd_runtime.context import Sandbox


class KVStoreContext(BaseModel, Bindable):
    """A simple key-value store execution context.

    Implements the ExecutionContext protocol with:
    - Immutable data storage (dict[str, str])
    - Change tracking between prepare and capture
    - Deterministic context_id based on content hash

    This context is useful for:
    - Configuration management tasks
    - State tracking across task executions
    - Testing and validation of the context pattern

    Attributes:
        data: The immutable key-value data (frozen after creation)
        source_step: Which task last modified this store (for audit trail)
        frozen_context_id: Frozen identity for effect correlation
        _snapshot: Private snapshot taken at prepare time for change detection

    Lifecycle:
        1. Create with initial data via KVStoreContext.create()
        2. __context_prepare__(): Takes snapshot of current data
        3. Task executes, modifying _working via set()/delete()
        4. __context_capture__(): Compares working state to snapshot
        5. Returns new context with updated data if changes detected
    """

    __binding_name__: ClassVar[str] = "config"

    data: dict[str, str]
    source_step: str | None = None
    frozen_context_id: str | None = None

    # Private attributes for lifecycle management (not serialized)
    _snapshot: dict[str, str] | None = PrivateAttr(default=None)
    _working: dict[str, str] | None = PrivateAttr(default=None)

    model_config = ConfigDict(frozen=True)

    @property
    def context_id(self) -> str:
        """Stable identity for effect attribution."""
        if self.frozen_context_id:
            return self.frozen_context_id
        return f"kvstore:{self._compute_hash()[:12]}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        """Declare reversibility as AUTO - in-memory data is mechanically reversible."""
        return ReversibilityLevel.AUTO

    def __str__(self) -> str:
        """Invisible in prompts (ExecutionContext convention)."""
        return ""

    def __repr__(self) -> str:
        """Useful repr for debugging."""
        keys_preview = list(self.data.keys())[:3]
        if len(self.data) > 3:
            keys_preview.append(f"...+{len(self.data) - 3}")
        return f"KVStoreContext({keys_preview}, step={self.source_step})"

    def _compute_hash(self) -> str:
        """Compute deterministic hash of data content."""
        content = "|".join(f"{k}={v}" for k, v in sorted(self.data.items()))
        return hashlib.sha256(content.encode()).hexdigest()

    @classmethod
    def create(cls, data: dict[str, str] | None = None) -> KVStoreContext:
        """Create a new KVStoreContext with initial data."""
        data = data or {}
        instance = cls(data=data)
        frozen_id = f"kvstore:{instance._compute_hash()[:12]}"
        object.__setattr__(instance, "frozen_context_id", frozen_id)
        return instance

    def get(self, key: str, default: str | None = None) -> str | None:
        """Get a value from the store."""
        source = self._working if self._working is not None else self.data
        return source.get(key, default)

    def set(self, key: str, value: str) -> None:
        """Set a value in the store."""
        if self._working is None:
            raise RuntimeError(
                "Cannot modify KVStoreContext before __context_prepare__(). "
                "Call prepare first or use create() with initial data."
            )
        self._working[key] = value

    def delete(self, key: str) -> bool:
        """Delete a key from the store."""
        if self._working is None:
            raise RuntimeError("Cannot modify KVStoreContext before __context_prepare__(). Call prepare first.")
        if key in self._working:
            del self._working[key]
            return True
        return False

    def keys(self) -> list[str]:
        """Get all keys in the store."""
        source = self._working if self._working is not None else self.data
        return list(source.keys())

    def has_key(self, key: str) -> bool:
        """Check if a key exists in the store."""
        source = self._working if self._working is not None else self.data
        return key in source

    # ExecutionContext Protocol Implementation (new API)

    def configure(
        self,
        capabilities: ProviderCapabilities | None = None,
    ) -> ProviderBinding:
        """Return provider configuration. Pure, no side effects."""
        return ProviderBinding(
            context_id=self.context_id,
            context_type="KVStoreContext",
            context_description=f"Key-value store with {len(self.data)} entries",
            visible=False,  # Invisible in prompts
        )

    def prepare(self) -> KVStoreContext:
        """Prepare the store for task execution."""
        snapshot = dict(self.data)
        working = dict(self.data)
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_working", working)
        return self

    def cleanup(self, error: Exception | None = None) -> None:
        """Cleanup after execution."""
        object.__setattr__(self, "_snapshot", None)
        object.__setattr__(self, "_working", None)

    # === State Serialization (for device boundary crossing) ===

    def to_state(self) -> dict[str, Any]:
        """Serialize KVStoreContext to a JSON-compatible state object."""
        return {
            "data": dict(self.data),
            "source_step": self.source_step,
            "frozen_context_id": self.frozen_context_id,
        }

    @classmethod
    def from_state(
        cls,
        state: Any,
        sandbox_path: Path | str | None = None,
    ) -> KVStoreContext:
        """Reconstruct KVStoreContext from state.

        KVStoreContext has no filesystem state to remap, so sandbox_path is ignored.
        """
        if not isinstance(state, dict):
            raise TypeError(f"KVStoreContext.from_state expected dict, got {type(state).__name__}")

        return cls(
            data=dict(state.get("data", {})),
            source_step=state.get("source_step"),
            frozen_context_id=state.get("frozen_context_id"),
        )

    def transfer_bundle(self, scope: Any) -> None:
        """KVStoreContext doesn't support device transfer.

        Returns None to indicate this context stays on the host
        and doesn't cross device boundaries (containers, VMs).
        """
        return

    # === v2 API: Effect-Driven State Derivation ===

    def extract_effects(
        self,
        sandbox: Sandbox | None,
        result: ExecutionResult,
    ) -> Sequence[Effect]:
        """Extract key changes as effects. PURE.

        Compares _working state to _snapshot to detect changes.
        Returns effects that can be applied via apply_effect() to
        derive new state.

        Args:
            sandbox: Ignored (KVStore doesn't use filesystem sandbox)
            result: Execution result (unused, changes tracked internally)

        Returns:
            Sequence of KeySet and KeyDeleted effects
        """
        if self._working is None or self._snapshot is None:
            return []

        if self._working == self._snapshot:
            return []

        effects: list[Effect] = []
        all_keys = set(self._snapshot.keys()) | set(self._working.keys())

        for key in all_keys:
            old_value = self._snapshot.get(key)
            new_value = self._working.get(key)
            if old_value != new_value:
                if new_value is None:
                    # Key was deleted
                    effects.append(
                        KeyDeleted(
                            key=key,
                            had_value=old_value or "",
                            context_id=self.context_id,
                        )
                    )
                else:
                    # Key was set (created or updated)
                    effects.append(
                        KeySet(
                            key=key,
                            old_value=old_value,
                            new_value=new_value,
                            context_id=self.context_id,
                        )
                    )

        return effects

    def apply_effect(self, effect: Effect) -> Self:
        """Apply effect to derive new context state. PURE.

        Handles:
        - KeySet: Updates data with new key/value
        - KeyDeleted: Removes key from data

        Other effects are ignored (they don't affect KVStore state).

        Note: We do NOT filter by context_id here. The scope routes effects to us
        by binding_name (stable), so we trust that we only receive effects intended
        for this context. This is essential for cache replay, where the context_id
        may differ from the original execution. See SessionState for the canonical pattern.

        Args:
            effect: Effect to apply

        Returns:
            New KVStoreContext instance (or self if no state change)
        """
        if isinstance(effect, KeySet):
            new_data = dict(self.data)
            new_data[effect.key] = effect.new_value
            # Only access task_name if effect is an Effect with this attribute
            source_step = None
            if hasattr(effect, "task_name"):
                source_step = effect.task_name
            return KVStoreContext(
                data=new_data,
                source_step=source_step,
                frozen_context_id=self.frozen_context_id,
            )

        if isinstance(effect, KeyDeleted):
            new_data = dict(self.data)
            new_data.pop(effect.key, None)
            # Only access task_name if effect is an Effect with this attribute
            source_step = None
            if hasattr(effect, "task_name"):
                source_step = effect.task_name
            return KVStoreContext(
                data=new_data,
                source_step=source_step,
                frozen_context_id=self.frozen_context_id,
            )

        return self

    # Legacy method names (for backward compatibility)
    __context_prepare__ = prepare
    __context_cleanup__ = cleanup

    def __context_capture__(self, source_step: str) -> KVStoreContext:
        """Legacy capture method using v2 API internally."""
        result = ExecutionResult(success=True, output_text="", metadata={"task_name": source_step})
        effects = list(self.extract_effects(None, result))
        new_ctx = self
        for effect in effects:
            new_ctx = new_ctx.apply_effect(effect)

        # Set source_step on the result if there were changes
        if new_ctx is not self and source_step:
            new_ctx = KVStoreContext(
                data=new_ctx.data,
                source_step=source_step,
                frozen_context_id=new_ctx.frozen_context_id,
            )
        return new_ctx

    def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        if self._working is None or self._snapshot is None:
            return False
        return self._working != self._snapshot

    def get_changes(self) -> dict[str, tuple[str | None, str | None]]:
        """Get detailed change information."""
        if self._working is None or self._snapshot is None:
            return {}

        changes: dict[str, tuple[str | None, str | None]] = {}
        all_keys = set(self._snapshot.keys()) | set(self._working.keys())
        for key in all_keys:
            old = self._snapshot.get(key)
            new = self._working.get(key)
            if old != new:
                changes[key] = (old, new)
        return changes


__all__ = ["KVStoreContext"]
