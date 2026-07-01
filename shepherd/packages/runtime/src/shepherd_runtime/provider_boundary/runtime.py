"""D2 provider-boundary runtime types.

Two Protocols plus in-memory test implementations:

- ``TraceWriter``     append-only sink for kernel and surface records
- ``ProviderRuntime`` runtime services exposed to provider adapters
                      during one ``perform_model_call`` invocation
- ``StubTraceWriter`` / ``StubProviderRuntime`` — counter-based test doubles
                      sufficient for consumer tests. Production refs use
                      canonical-JSON-SHA256; consumer code MUST NOT assert ref
                      equality across test-vs-production substitution.

Pinned by `docs/design/proposed/260505-plans/CONTRACTS.md` D2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from shepherd_runtime.kernel import ExecutionContext
from shepherd_runtime.trace import KernelRecord, Ref, RunRef, SurfaceRecord

__all__ = [
    "ProviderRuntime",
    "StubProviderRuntime",
    "StubTraceWriter",
    "TraceWriter",
]


@runtime_checkable
class TraceWriter(Protocol):
    """Append-only sink for kernel and surface records.

    The writer mints content-addressed refs for each appended record;
    refs are unique within a trace. Returned refs are stable across
    runs in production (canonical-JSON-SHA256 over record content);
    test-runtime counter-based refs are not.
    """

    def append_kernel(self, record: KernelRecord) -> Ref: ...

    def append_surface(self, record: SurfaceRecord) -> Ref: ...


@runtime_checkable
class ProviderRuntime(Protocol):
    """Runtime services for one ``perform_model_call`` invocation.

    Constructed by ``ExecutionLifecycle``, consumed by the adapter
    and (transitively) by the recorder. ``execution_context`` is
    captured at runtime construction and remains stable for the
    call's lifetime; kernel records emitted during the call cite this
    exact context.
    """

    @property
    def trace_writer(self) -> TraceWriter: ...

    @property
    def execution_context(self) -> ExecutionContext: ...

    @property
    def run_ref(self) -> RunRef: ...

    @property
    def task_name(self) -> str | None: ...


# ---------------------------------------------------------------------------
# In-memory test implementations (counter-based refs; not content-addressed)
# ---------------------------------------------------------------------------


@dataclass
class StubTraceWriter:
    """In-memory ``TraceWriter`` for consumer tests.

    Mints counter-based refs (``ref:1``, ``ref:2``, ...) and stores appended
    records in lists. Production replaces this with ``content_ref(...)`` minting
    and durable storage.
    """

    kernel_records: list[tuple[str, Any]] = field(default_factory=list)
    surface_records: list[tuple[str, Any]] = field(default_factory=list)
    _counter: int = 0

    def _next_ref(self) -> str:
        self._counter += 1
        return f"ref:{self._counter}"

    def append_kernel(self, record: KernelRecord) -> Ref:
        ref = self._next_ref()
        self.kernel_records.append((ref, record))
        return ref

    def append_surface(self, record: SurfaceRecord) -> Ref:
        ref = self._next_ref()
        self.surface_records.append((ref, record))
        return ref


@dataclass
class StubProviderRuntime:
    """Concrete ``ProviderRuntime`` for consumer tests.

    All four properties mandatory per CONTRACTS D2; adapters cannot
    rely on duck-typing for any of them.
    """

    trace_writer: StubTraceWriter = field(default_factory=StubTraceWriter)
    execution_context: ExecutionContext = field(default_factory=ExecutionContext)
    run_ref: RunRef = field(default_factory=lambda: RunRef(id="run-stub"))
    task_name: str | None = None
