"""Typed public identity nouns for workspace-control surfaces."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from shepherd_runtime.identities import RunRef

WORKSPACE_REF_SCHEMA = "shepherd.workspace_control.workspace_ref.v1"
TASK_REF_SCHEMA = "shepherd.workspace_control.task_ref.v1"

# A task defined in a run-as-script module (__main__) is registered under a synthetic
# module derived from its qualname (its artifact is the definition, not the script).
# The same default convention is used by callable lookup. Explicit `task_id=`
# registration is intentionally not an alias for this derived id.
GENERATED_MODULE_PREFIX = "shepherd_generated_"


def task_id_for_callable(fn: Callable[..., object]) -> str:
    """Derive the default task id for a callable (plain or ``@sp.task``-decorated).

    Mirrors registration's default id derivation without capturing source. A task
    registered with an explicit ``task_id=`` must be referenced by that id. Refuses
    unstable callables (locals, lambdas).
    """
    plain = inspect.unwrap(fn)
    module = getattr(plain, "__module__", "") or ""
    qualname = getattr(plain, "__qualname__", getattr(plain, "__name__", "")) or ""
    if not module or not qualname or "<locals>" in qualname:
        raise TypeError(f"callable {fn!r} does not have a stable task identity")
    if module == "__main__":
        return f"{GENERATED_MODULE_PREFIX}{qualname.replace('.', '_')}.{qualname}"
    return f"{module}.{qualname}"


@dataclass(frozen=True)
class WorkspaceRef:
    """Pure value identity for one workspace-control facade.

    This is not a workspace handle, lock, or custody object. It is a typed
    public spelling for the workspace identity string used at API boundaries.
    """

    id: str

    def __post_init__(self) -> None:
        _validate_non_empty_identity(self.id, "WorkspaceRef id")

    def __str__(self) -> str:
        return self.id

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-shaped representation of this workspace identity."""
        return {"schema": WORKSPACE_REF_SCHEMA, "id": self.id}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> WorkspaceRef:
        """Rehydrate a workspace identity from its JSON-shaped representation."""
        return cls(id=_id_from_payload(payload, schema=WORKSPACE_REF_SCHEMA, noun="WorkspaceRef"))

    @classmethod
    def from_path(cls, path: str | Path) -> WorkspaceRef:
        """Build a workspace identity from a filesystem path."""
        return cls(str(Path(path).resolve()))


@dataclass(frozen=True)
class TaskRef:
    """Pure value identity for a task definition reference.

    The value is the existing workspace-control task reference spelling:
    ``task_id`` or ``task_id@version``.
    """

    id: str

    def __post_init__(self) -> None:
        _validate_task_ref(self.id)

    def __str__(self) -> str:
        return self.id

    def to_payload(self) -> dict[str, object]:
        """Return a JSON-shaped representation of this task identity."""
        return {"schema": TASK_REF_SCHEMA, "id": self.id}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TaskRef:
        """Rehydrate a task identity from its JSON-shaped representation."""
        return cls(id=_id_from_payload(payload, schema=TASK_REF_SCHEMA, noun="TaskRef"))


TaskRefInput: TypeAlias = "str | TaskRef | Callable[..., object]"
RunRefInput: TypeAlias = str | RunRef
RunSelectorInput: TypeAlias = str | RunRef
WorkspaceRefInput: TypeAlias = str | WorkspaceRef


def coerce_task_ref(value: TaskRefInput, *, field_name: str = "task_ref") -> str:
    """Return the string identity for a task ref boundary value.

    Accepts a task-id string, a ``TaskRef``, or the task callable itself (plain or
    ``@sp.task``-decorated) — the callable resolves by the default callable-identity
    convention. A task registered with an explicit ``task_id=`` must be referenced by
    that id until source-sync/aliasing lands.
    """
    if isinstance(value, TaskRef):
        return value.id
    if isinstance(value, str):
        _validate_task_ref(value, field_name=field_name)
        return value
    if callable(value):
        return task_id_for_callable(value)
    raise TypeError(f"{field_name} must be a TaskRef, task callable, or non-empty string")


def coerce_run_ref(value: RunRefInput, *, field_name: str = "run_ref") -> str:
    """Return the exact string identity for a run ref boundary value."""
    if isinstance(value, RunRef):
        return value.id
    if isinstance(value, str):
        _validate_exact_run_ref(value, field_name=field_name)
        return value
    raise TypeError(f"{field_name} must be a RunRef or non-empty string")


def coerce_exact_run_ref(value: RunRefInput, *, field_name: str = "run_ref") -> str:
    """Return an exact run identity for mutation or repair boundaries."""
    return coerce_run_ref(value, field_name=field_name)


def coerce_optional_run_ref(value: RunRefInput | None, *, field_name: str = "run_ref") -> str | None:
    """Return a string run identity or ``None`` for optional run ref parameters."""
    if value is None:
        return None
    return coerce_run_ref(value, field_name=field_name)


def coerce_run_selector(value: RunSelectorInput, *, field_name: str = "run_ref") -> str:
    """Return a run selector string for read/query boundaries."""
    if isinstance(value, RunRef):
        return value.id
    if isinstance(value, str):
        _validate_non_empty_identity(value, field_name)
        return value
    raise TypeError(f"{field_name} must be a RunRef or non-empty string")


def coerce_optional_run_selector(
    value: RunSelectorInput | None,
    *,
    field_name: str = "run_ref",
) -> str | None:
    """Return a run selector string or ``None`` for optional read/query parameters."""
    if value is None:
        return None
    return coerce_run_selector(value, field_name=field_name)


def coerce_workspace_ref(value: WorkspaceRefInput, *, field_name: str = "workspace_ref") -> str:
    """Return the string identity for a workspace ref boundary value."""
    if isinstance(value, WorkspaceRef):
        return value.id
    if isinstance(value, str):
        _validate_non_empty_identity(value, field_name)
        return value
    raise TypeError(f"{field_name} must be a WorkspaceRef or non-empty string")


def _id_from_payload(payload: Mapping[str, object], *, schema: str, noun: str) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{noun} payload must be an object")
    if payload.get("schema") != schema:
        raise ValueError(f"{noun} payload schema is unsupported")
    raw_id = payload.get("id")
    if not isinstance(raw_id, str):
        raise TypeError(f"{noun} payload id must be a string")
    _validate_non_empty_identity(raw_id, f"{noun} id")
    return raw_id


def _validate_task_ref(value: object, *, field_name: str = "TaskRef id") -> None:
    _validate_non_empty_identity(value, field_name)
    assert isinstance(value, str)
    if any(char.isspace() for char in value):
        raise ValueError(f"{field_name} must not contain whitespace")
    if "@" in value:
        task_id, version = value.rsplit("@", 1)
        if not task_id or not version:
            raise ValueError(f"{field_name} must be shaped as task_id or task_id@version")


def _validate_exact_run_ref(value: object, *, field_name: str) -> None:
    _validate_non_empty_identity(value, field_name)
    assert isinstance(value, str)
    if value == "@latest":
        raise ValueError(f"{field_name} must be an exact run id, not selector '@latest'")


def _validate_non_empty_identity(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


__all__ = [
    "TASK_REF_SCHEMA",
    "WORKSPACE_REF_SCHEMA",
    "RunRef",
    "RunRefInput",
    "RunSelectorInput",
    "TaskRef",
    "TaskRefInput",
    "WorkspaceRef",
    "WorkspaceRefInput",
    "coerce_exact_run_ref",
    "coerce_optional_run_ref",
    "coerce_optional_run_selector",
    "coerce_run_ref",
    "coerce_run_selector",
    "coerce_task_ref",
    "coerce_workspace_ref",
]
