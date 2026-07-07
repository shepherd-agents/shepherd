"""Public foundation types for the syntax nucleus."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from shepherd_core.errors import ShepherdError
from shepherd_kernel_v3_reference.proof_envelope import ProofEnvelope, runtime_only_envelope

from shepherd_runtime.identities import RUN_REF_SCHEMA, RunRef

# Runtime import is intentional so typing.get_type_hints(Run)["trace"] resolves.
from shepherd_runtime.trace.container import Trace  # noqa: TC001

T = TypeVar("T")


class _NucleusError(ShepherdError, RuntimeError):
    """Base class for syntax nucleus runtime errors."""


class DeliveryException(_NucleusError):  # noqa: N818
    """Shared parent for ordinary-call delivery exceptions."""

    def __init__(self, message: str = "Delivery exception", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


class DeliveryFailed(DeliveryException):
    """Ordinary-call exception for a failed terminal run."""

    def __init__(self, message: str = "Delivery failed", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


class AmbientWorldAccessRefused(DeliveryException):
    """Refusal for an ambient call of a bodyless task that declares world access.

    A bodyless task whose signature carries substrate-handle annotations
    (``May[GitRepo, ...]`` parameters or handle-typed returns) cannot be honored
    by an in-process ambient model call: the grant would be silently erased and
    a "successful" delivery would be a fabricated report of world work the model
    cannot have done. Run the task through retained execution (``workspace.run(...)``)
    instead. The refusal keys on the annotation, never the passed value.
    """

    def __init__(self, message: str = "Ambient world access refused", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


class AmbiguousTaskBody(DeliveryException):
    """The task body cannot be classified and would run to a silent ``None``.

    Source is unavailable (exec/REPL/notebook definition) and the compiled body
    is empty-shaped — docstring-only and ``return None`` bodies compile
    byte-identically, so the runtime cannot tell a delegating bodyless task
    from a deliberate no-op. Raised loud at call time instead of silently
    returning ``None``. Remedy: move the task into an importable ``.py`` file.
    """

    def __init__(self, message: str = "Ambiguous task body", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


class DeliveryExhausted(DeliveryException):
    """Ordinary-call exception for an exhausted terminal run."""

    def __init__(self, message: str = "Delivery exhausted", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


class DeliveryStopped(DeliveryException):
    """Ordinary-call exception for a stopped terminal run."""

    def __init__(self, message: str = "Delivery stopped", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


class WorkspaceNotConfigured(_NucleusError):  # noqa: N818
    """Raised when task execution needs a nucleus workspace but none is active."""


class WorkspaceAlreadyConfigured(_NucleusError):  # noqa: N818
    """Raised when an ambient workspace is opened with conflicting configuration."""


class NoActiveTaskRun(_NucleusError):  # noqa: N818
    """Raised when a nucleus primitive requires an active task run."""


class RunInProgress(_NucleusError):  # noqa: N818
    """Raised when a completed-run-only read is attempted before completion."""

    def __init__(self, message: str = "Run is still in progress", *, run: Any | None = None) -> None:
        super().__init__(message)
        self.run = run


@dataclass(frozen=True)
class DeliveryLimits:
    """First-cut delivery controls."""

    max_turns: int | None = None

    def __post_init__(self) -> None:
        if self.max_turns is None:
            return
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int):
            raise TypeError("DeliveryLimits.max_turns must be None or a positive integer")
        if self.max_turns <= 0:
            raise ValueError("DeliveryLimits.max_turns must be positive")


@dataclass(frozen=True)
class Finished(Generic[T]):
    """Terminal outcome carrying a completed value."""

    value: T


@dataclass(frozen=True)
class Exhausted:
    """Terminal outcome for a run that exhausted its delivery budget."""

    reason: str


@dataclass(frozen=True)
class Stopped:
    """Terminal outcome for a run stopped by external control."""

    reason: str


@dataclass(frozen=True)
class Failed:
    """Terminal outcome for a run that failed after execution began."""

    error_type: str
    message: str
    retryable: bool | None = None


@dataclass(frozen=True)
class Run(Generic[T]):
    """Inspectable task run result used by detailed calls."""

    outcome: Finished[T] | Exhausted | Stopped | Failed
    effects: tuple[object, ...]
    artifacts: tuple[object, ...]
    usage: object | None
    duration: float
    trace: Trace | None
    ref: RunRef | None
    proof: ProofEnvelope = field(default_factory=runtime_only_envelope)

    def unwrap(self) -> T:
        if isinstance(self.outcome, Finished):
            return self.outcome.value
        if isinstance(self.outcome, Failed):
            raise DeliveryFailed(self.outcome.message, run=self)
        if isinstance(self.outcome, Exhausted):
            raise DeliveryExhausted(self.outcome.reason, run=self)
        if isinstance(self.outcome, Stopped):
            raise DeliveryStopped(self.outcome.reason, run=self)
        raise DeliveryFailed("Unknown delivery outcome", run=self)


__all__ = [
    "RUN_REF_SCHEMA",
    "DeliveryException",
    "DeliveryExhausted",
    "DeliveryFailed",
    "DeliveryLimits",
    "DeliveryStopped",
    "Exhausted",
    "Failed",
    "Finished",
    "NoActiveTaskRun",
    "Run",
    "RunInProgress",
    "RunRef",
    "Stopped",
    "WorkspaceAlreadyConfigured",
    "WorkspaceNotConfigured",
]
