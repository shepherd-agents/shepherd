"""Private substrate driver draft contract for world-vector ingress.

The SPI v0.1 contract is pinned in
``vcs-core/design/roadmap/substrate-framework/260523-2200-spi/260523-2200-spi.md``.
This module is its in-code embodiment.

The synchronous request/result family is dispatched via
``SubstrateDriver.prepare(context, request)`` where ``request`` is a member of
the ``IngressRequest`` discriminated union. The parse-only streaming family
is a sibling ``CaptureAdapter`` Protocol whose ``parse`` emits observations
to an ``ObservationSink``. The two families differ in shape (sync vs streaming,
single-consumer vs fan-out) and ship as siblings rather than a single
overloaded surface.

The stable import home for this vocabulary is :mod:`vcs_core.spi`
(``decisions.md`` ``spi-top-level-promotion``); this private module is its
definition site. (Typed ``prepare(request)`` dispatch is the only path —
the legacy ``prepare_command`` method was removed at T3-final.)
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from inspect import Parameter, Signature, signature
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Protocol,
    TypeVar,
    Union,
    assert_never,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._transition_kernel_records import (
    VALIDATED_PAYLOAD_DESCRIPTOR_SCHEMA,
    EvidenceRef,
    PayloadDescriptorClaim,
    RelationshipRequirement,
    ValidatedPayloadDescriptor,
)

if TYPE_CHECKING:
    from vcs_core._world_types import SubstrateStoreIdentity
    from vcs_core.types import EffectRecord


# Stable contract revision under SPI_VERSION = 0. Tracked here so consumers
# can read the live value rather than scrape the design doc.
SUBSTRATE_DRIVER_CONTRACT_REVISION: str = "v0.1"

_CommandFunc = TypeVar("_CommandFunc", bound=Callable[..., object])
_COMMAND_METADATA_ATTR = "__vcs_core_command__"


# ===========================================================================
# Diagnostic shape (typed alternative to Mapping[str, object])
# ===========================================================================


@dataclass(frozen=True)
class Diagnostic:
    """Coordinator-readable diagnostic produced by a driver or adapter.

    Diagnostics ride alongside observations and transitions in the
    ``DriverIngressResult``; they report problems without producing
    substrate state.
    """

    code: str
    message: str
    subject: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


# ===========================================================================
# Typed ingress family (SPI v0.1 Q1)
# ===========================================================================


@dataclass(frozen=True)
class CommandRequest:
    """Declared-intent substrate mutation.

    Used for bootstrap, import, create-candidate, checkpoint, append, and
    similar caller-declared semantic operations.
    """

    command: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ingress_kind(self) -> Literal["command"]:
        return "command"


@dataclass(frozen=True)
class ScanRequest:
    """Post-hoc state observation from external sources (drift detector).

    Used for workspace-scan and workspace-adoption.
    """

    scan_kind: str
    external_state: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ingress_kind(self) -> Literal["scan"]:
        return "scan"


@dataclass(frozen=True)
class CaptureRequest:
    """Evidence-persistence stage: adapter has produced observations.

    The driver does not produce transitions from a CaptureRequest; the
    coordinator persists the observations as evidence and a later
    ReduceRequest carries citations to those evidence refs.
    """

    adapter_id: str
    observations: Sequence[ObservationDraft] = ()

    @property
    def ingress_kind(self) -> Literal["capture"]:
        return "capture"


@dataclass(frozen=True)
class ReduceRequest:
    """Reduction stage: produce transitions citing previously persisted observations.

    The driver does not receive a typed copy of the observations directly.
    Observations live in the coordinator's evidence store; the driver
    accesses them by resolving citations to EvidenceRecords when its
    reduction logic needs the full payload. This avoids holding two
    authoritative views of the same content in the request envelope.

    ``reduction_payload`` and ``reduction_proof`` (T2c) carry the
    caller-computed reduction state when the driver cannot derive it from
    citations alone. v0.1 ``DriverContext`` does not carry a coordinator-
    supplied evidence resolver, so callers (e.g., the runtime layer that
    already holds the bytes the patch manager captured) compute the
    reduction and supply it here. v0.2 may revisit whether these fields
    can be derived from citations alone given an evidence resolver in
    ``DriverContext``. Both fields are optional at the SPI shape level;
    per-driver semantic validation (validator layer 3) decides whether
    a given driver's reduce handler requires them.
    """

    evidence_citations: ReductionBatch
    reduction_payload: Mapping[str, Any] | None = None
    reduction_proof: Mapping[str, Any] | None = None

    @property
    def ingress_kind(self) -> Literal["reduce"]:
        return "reduce"


@dataclass(frozen=True)
class MergeRequest:
    """Coordinated multi-head merge.

    Workspace overlay-merge is the v0.1 instance. Future merge mechanisms
    may want different policy shapes; v0.1 keeps ``policy`` as
    ``Mapping[str, Any]`` and v0.2 may introduce a per-mechanism
    discriminator.
    """

    other_head: str
    policy: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ingress_kind(self) -> Literal["merge"]:
        return "merge"


# Reserved for v0.2 (kernel-v3 designs the field shape):
# @dataclass(frozen=True)
# class ReplayRequest:
#     prior_transition_digest: str
#     ...


IngressRequest = CommandRequest | ScanRequest | CaptureRequest | ReduceRequest | MergeRequest


# ===========================================================================
# Capabilities, storage shape, and active surface (SPI v0.1 Q1, Q5a)
# ===========================================================================


@dataclass(frozen=True)
class CapabilitySet:
    """Declared per-driver capability surface.

    ``accepts`` is the set of ``IngressRequest`` variants this driver
    handles via ``prepare(context, request)``. Coordinator fails closed
    on unsupported requests before invoking the driver.

    The non-derivable flags (``selectable``, ``materializable``,
    ``journal_only``, ``evidence_only``, ``recursive_reference_capable``)
    declare static driver shape facts that don't map to the dispatch
    surface.
    """

    accepts: frozenset[type[IngressRequest]] = frozenset()
    selectable: bool = True
    materializable: bool = False
    journal_only: bool = False
    evidence_only: bool = False
    recursive_reference_capable: bool = False


RevisionStorageShape = Literal[
    "json-snapshot",
    "tree-projection",
    "keyed-json-tree",
    "event-stream",
    "derived-index",
    "object-store",
]
AuthorityRole = Literal["authority", "projection", "accelerator"]
GrowthBound = Literal["bounded", "unbounded"]
ReadSafety = Literal["superset", "exact"]
CrashLagOrdering = Literal["index-leads", "authority-leads", "atomic"]


@dataclass(frozen=True)
class RevisionStorageProfile:
    """Declared logical storage shape for revisions produced by a driver.

    ``CapabilitySet`` answers what request families dispatch at runtime.
    This profile answers a different question: whether selected revisions are
    bounded JSON snapshots, addressable tree content, append/event streams, or
    derived views. It is introspection/conformance metadata, not a dispatch
    switch.
    """

    shape: RevisionStorageShape = "json-snapshot"
    authority_role: AuthorityRole = "authority"
    growth_bound: GrowthBound = "bounded"
    read_safety: ReadSafety | None = None
    crash_lag: CrashLagOrdering | None = None
    notes: str = ""
    allow_totalized_snapshot: bool = False

    def __post_init__(self) -> None:
        if self.shape not in get_args(RevisionStorageShape):
            raise ValueError(f"unsupported revision storage shape: {self.shape!r}")
        if self.authority_role not in get_args(AuthorityRole):
            raise ValueError(f"unsupported revision authority role: {self.authority_role!r}")
        if self.growth_bound not in get_args(GrowthBound):
            raise ValueError(f"unsupported revision growth bound: {self.growth_bound!r}")
        if self.read_safety is not None and self.read_safety not in get_args(ReadSafety):
            raise ValueError(f"unsupported revision read safety: {self.read_safety!r}")
        if self.crash_lag is not None and self.crash_lag not in get_args(CrashLagOrdering):
            raise ValueError(f"unsupported revision crash-lag ordering: {self.crash_lag!r}")


@dataclass(frozen=True)
class ActiveSurface:
    """Effect surface declared by ``may=`` (or session/scope policy).

    Each axis carries an optional allow set and a deny set. ``allow_*``
    being None means "no whitelist restriction on this axis"; ``deny_*``
    is always additive. This shape supports both whitelist policies
    (``may=AllowOnly([...])``) and blacklist policies (``may=ReadOnly``).

    A request is admitted iff:

    - ``allow_request_types is None or type(request) in allow_request_types``
    - ``type(request) not in deny_request_types``
    - ``allow_evidence_kinds is None`` or every observation's
      ``evidence_kind`` in result is in ``allow_evidence_kinds``
    - no observation's ``evidence_kind`` in ``deny_evidence_kinds``
    - ``allow_semantic_ops is None`` or every transition's ``semantic_op``
      in result is in ``allow_semantic_ops``
    - no transition's ``semantic_op`` in ``deny_semantic_ops``

    The request-type check fires pre-dispatch; evidence-kind /
    semantic-op checks fire post-dispatch (after the driver returns and
    before the coordinator lowers the result).
    """

    allow_request_types: frozenset[type[IngressRequest]] | None = None
    deny_request_types: frozenset[type[IngressRequest]] = frozenset()
    allow_evidence_kinds: frozenset[str] | None = None
    deny_evidence_kinds: frozenset[str] = frozenset()
    allow_semantic_ops: frozenset[str] | None = None
    deny_semantic_ops: frozenset[str] = frozenset()


# ===========================================================================
# Coordinator-supplied context (Q3, Q5a)
# ===========================================================================


@dataclass(frozen=True)
class ChildWorldSnapshot:
    """Read-only child-world fact returned to drivers by coordinator context."""

    world_store_id: str
    world_oid: str
    snapshot_digest: str

    def to_payload(self) -> dict[str, object]:
        return {
            "world_store_id": self.world_store_id,
            "world_oid": self.world_oid,
            "snapshot_digest": self.snapshot_digest,
        }


class ChildWorldResolver(Protocol):
    """Narrow read-only lookup service for world-ref-style drivers."""

    def resolve_child_world(
        self,
        world_oid: str,
        *,
        expected_world_store_id: str | None = None,
        expected_snapshot_digest: str | None = None,
    ) -> ChildWorldSnapshot: ...


@dataclass(frozen=True)
class DriverContext:
    """Coordinator-issued context for one driver dispatch.

    ``active_surface`` is the Q5a reservation hook; unused in v0.1 but
    pinned at the Protocol level so tour-bundle integration can land
    without reshape.
    """

    operation_id: str
    binding: str
    role: str
    store_identity: SubstrateStoreIdentity
    base_heads: tuple[str, ...] = ()
    child_worlds: ChildWorldResolver | None = None
    active_surface: ActiveSurface | None = None


# ===========================================================================
# Draft DTOs (unchanged shape from prior contract)
# ===========================================================================


@dataclass(frozen=True)
class KeyedJsonPut:
    """One JSON object written at an addressable path inside a keyed tree."""

    key: str
    path: str
    payload: dict[str, object]


@dataclass(frozen=True)
class KeyedJsonTreeDraft:
    """Addressable JSON-tree content for one logical substrate revision.

    ``payload`` on :class:`TransitionDraft` remains the small revision manifest
    written to ``revision.json``. This draft carries the changed keyed records
    under ``content_root``.
    """

    manifest: dict[str, object]
    base_head: str | None
    puts: tuple[KeyedJsonPut, ...]
    deletes: tuple[str, ...] = ()
    content_root: str = "data"


RevisionContentDraft = KeyedJsonTreeDraft


@dataclass(frozen=True)
class ObservationDraft:
    """Driver observation before coordinator-owned evidence persistence."""

    observation_id: str
    evidence_kind: str
    stable_observation: dict[str, object]
    observed_head: str | None = None
    observed_at_unix_ns: int | None = None
    mechanism: str | None = None
    correlation_id: str | None = None
    evidence_payload_descriptor_claim: PayloadDescriptorClaim | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class TransitionDraft:
    """Driver transition before coordinator-owned canonical lowering."""

    transition_id: str
    semantic_op: str
    payload: dict[str, object]
    observation_ids: tuple[str, ...]
    evidence_citation_ids: tuple[str, ...] = ()
    base_heads: tuple[str, ...] = ()
    payload_descriptor_claim: PayloadDescriptorClaim | None = None
    materialization_class: str = "external"
    relationship_requirements: tuple[RelationshipRequirement, ...] = ()
    metadata: dict[str, object] | None = None
    # Optional Git tree oid the driver wants embedded as a tree-backed payload.
    # When set, the coordinator propagates this to PreparedRevisionPlan.git_tree_oid
    # and the substrate commits a workspace/ tree entry alongside revision.json.
    # The tree must be reachable from the substrate's ODB (alternates suffice).
    git_tree_oid: str | None = None
    # Optional structured content for revisions whose logical state is not a
    # bounded JSON snapshot. ``payload`` remains the revision manifest.
    content: RevisionContentDraft | None = None


@dataclass(frozen=True)
class RetentionHint:
    """Advisory retention proposed by a driver before coordinator policy."""

    kind: str
    target: str
    digest: str | None = None
    mandatory: bool = False
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class DriverSelectionRequirementDraft:
    """Driver-facing selection proposal, distinct from coordinator plans."""

    binding: str
    role: str
    selection_kind: str
    transition_id: str | None = None
    retention_hints: tuple[RetentionHint, ...] = ()
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class DriverIngressResult:
    """Batch-shaped driver ingress result awaiting coordinator validation."""

    observations: tuple[ObservationDraft, ...] = ()
    transitions: tuple[TransitionDraft, ...] = ()
    effects: tuple[EffectRecord, ...] = ()
    value: object | None = None
    retention_hints: tuple[RetentionHint, ...] = ()
    selection_requirements: tuple[DriverSelectionRequirementDraft, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class EvidenceCitation:
    """Coordinator-issued handle for one existing evidence ref in a reducer batch."""

    citation_id: str
    producer_operation_id: str
    evidence_ref: EvidenceRef
    evidence_digest: str
    record_digest: str
    payload_digest: str
    binding: str
    store_id: str
    substrate_kind: str
    evidence_kind: str


@dataclass(frozen=True)
class ReductionBatch:
    """Operation-scoped allowlist of evidence citations available to one lowering call."""

    citations: tuple[EvidenceCitation, ...]


# ===========================================================================
# Introspection schema (Q4)
# ===========================================================================


@dataclass(frozen=True)
class ParamSpec:
    """Schema for a single command/scan/merge param."""

    type: str
    required: bool = True
    description: str = ""
    has_default: bool = False
    default: Any | None = None
    choices: tuple[Any, ...] = ()
    repeated: bool = False
    projectable: bool = True


@dataclass(frozen=True)
class CommandSpec:
    """Schema for one CommandRequest.command value the driver accepts."""

    description: str = ""
    params: Mapping[str, ParamSpec] = field(default_factory=dict)
    examples: tuple[str, ...] = ()
    projectable: bool = True
    required_one_of: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ScanSpec:
    """Schema for one ScanRequest.scan_kind value the driver accepts."""

    description: str = ""
    params: Mapping[str, ParamSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeSpec:
    """Schema for the MergeRequest.policy values the driver accepts."""

    description: str = ""
    params: Mapping[str, ParamSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptureAdapterSchema:
    """Driver-declared metadata for one capture adapter."""

    adapter_id: str
    adapter_version: str
    mechanism: str
    evidence_kinds: tuple[str, ...]


@dataclass(frozen=True)
class DriverSchema:
    """Typed introspection axis for a SubstrateDriver.

    Returned by ``SubstrateDriver.describe()``. JSON-serializable via the
    existing dataclass conventions. Consumers cache one per driver
    instance; ``describe`` has no context argument in v0.1 so the schema
    is invariant under DriverContext changes.
    """

    driver_id: str
    driver_version: str
    capabilities: CapabilitySet
    commands: Mapping[str, CommandSpec] = field(default_factory=dict)
    scans: Mapping[str, ScanSpec] = field(default_factory=dict)
    merges: Mapping[str, MergeSpec] = field(default_factory=dict)
    capture_adapters: tuple[CaptureAdapterSchema, ...] = ()
    storage_profile: RevisionStorageProfile = field(default_factory=RevisionStorageProfile)


@dataclass(frozen=True)
class _CommandDecoratorMetadata:
    name: str
    description: str | None
    projectable: bool
    examples: tuple[str, ...]
    required_one_of: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _DecoratedCommandBinding:
    attr_name: str
    metadata: _CommandDecoratorMetadata
    command_spec: CommandSpec
    context_delivery: Literal["absent", "positional", "keyword"]


def command(
    name: str,
    *,
    description: str | None = None,
    projectable: bool = True,
    examples: tuple[str, ...] = (),
    required_one_of: tuple[tuple[str, ...], ...] = (),
) -> Callable[[_CommandFunc], _CommandFunc]:
    """Mark a ``BaseSubstrateDriver`` method as a schema-derived command.

    The decorator records metadata only. Drivers that want helper dispatch
    explicitly delegate from ``prepare()`` to
    ``dispatch_decorated_command(...)``.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("@command name must be a non-empty string")
    if not isinstance(examples, tuple) or any(not isinstance(item, str) or not item for item in examples):
        raise ValueError("@command examples must be a tuple of non-empty strings")
    if (
        not isinstance(required_one_of, tuple)
        or any(not isinstance(group, tuple) or not group for group in required_one_of)
        or any(not isinstance(item, str) or not item for group in required_one_of for item in group)
    ):
        raise ValueError("@command required_one_of must be a tuple of non-empty parameter-name tuples")
    metadata = _CommandDecoratorMetadata(
        name=name,
        description=description,
        projectable=projectable,
        examples=examples,
        required_one_of=required_one_of,
    )

    def _decorate(func: _CommandFunc) -> _CommandFunc:
        setattr(func, _COMMAND_METADATA_ATTR, metadata)
        return func

    return _decorate


# ===========================================================================
# CaptureAdapter Protocol + observation sink family (Q2, Q5b)
# ===========================================================================


@dataclass(frozen=True)
class ParseResult:
    """Result of one CaptureAdapter.parse() invocation.

    Observations and diagnostics are delivered via the sink during parse;
    this return value carries the summary so callers can verify completion
    without scanning the sink contents.
    """

    parsed_count: int
    diagnostic_count: int
    skipped: bool = False
    # Continuation handle for resumable parsing of large event streams.
    # None in v0.1; reserved for v0.2 if a streaming adapter needs it.
    continuation: object | None = None
    # Sink failures observed during parse, recorded per Q5b discipline.
    sink_failures: tuple[Diagnostic, ...] = ()

    @classmethod
    def skip(cls) -> ParseResult:
        """Adapter declined to parse (e.g., events were not for this mechanism)."""
        return cls(parsed_count=0, diagnostic_count=0, skipped=True)

    @classmethod
    def complete(cls, *, parsed: int, diagnostics: int = 0) -> ParseResult:
        """Adapter parsed all provided events successfully."""
        return cls(parsed_count=parsed, diagnostic_count=diagnostics)


@runtime_checkable
class ObservationSink(Protocol):
    """Coordinator-or-supervisor sink for capture-adapter observations."""

    def emit(self, observation: ObservationDraft) -> None: ...
    def diagnostic(self, diagnostic: Diagnostic) -> None: ...


@dataclass
class TupleSink:
    """Default ObservationSink that collects to tuples for synchronous consumers.

    Direct propagation: per Q5b, a raising TupleSink (used standalone,
    not wrapped in FanOutSink) propagates the exception to the caller —
    there are no other sinks to protect.
    """

    observations: list[ObservationDraft] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def emit(self, observation: ObservationDraft) -> None:
        self.observations.append(observation)

    def diagnostic(self, diagnostic: Diagnostic) -> None:
        self.diagnostics.append(diagnostic)


@dataclass(frozen=True)
class SinkFailure:
    """One sink's exception during fan-out delivery (Q5b)."""

    sink_index: int
    sink_repr: str
    operation: Literal["emit", "diagnostic"]
    subject_id: str
    exception_repr: str

    def as_diagnostic(self) -> Diagnostic:
        return Diagnostic(
            code="sink_failure",
            message=f"sink {self.sink_index} ({self.sink_repr}) raised on {self.operation}",
            subject=self.subject_id,
            detail={
                "sink_index": self.sink_index,
                "sink_repr": self.sink_repr,
                "operation": self.operation,
                "exception": self.exception_repr,
            },
        )


class FanOutSink:
    """Compose multiple ObservationSinks per the Q5b delivery discipline.

    - Synchronous push-based: every sink sees observation N before any
      sink sees observation N+1 (fan-out atomicity).
    - Per-adapter ordering preserved per sink.
    - Per-sink failure isolation: an exception in one sink's emit /
      diagnostic is caught, recorded as a SinkFailure, and does not
      block delivery to other sinks. The failing sink misses the
      observation that triggered the exception; subsequent observations
      continue to attempt delivery to it.
    """

    def __init__(self, sinks: Sequence[ObservationSink]) -> None:
        self._sinks: tuple[ObservationSink, ...] = tuple(sinks)
        self._failures: list[SinkFailure] = []

    @property
    def sinks(self) -> tuple[ObservationSink, ...]:
        return self._sinks

    @property
    def failures(self) -> tuple[SinkFailure, ...]:
        return tuple(self._failures)

    def emit(self, observation: ObservationDraft) -> None:
        for index, sink in enumerate(self._sinks):
            try:
                sink.emit(observation)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                self._failures.append(
                    SinkFailure(
                        sink_index=index,
                        sink_repr=repr(sink),
                        operation="emit",
                        subject_id=observation.observation_id,
                        exception_repr=repr(exc),
                    )
                )

    def diagnostic(self, diagnostic: Diagnostic) -> None:
        subject = diagnostic.subject or diagnostic.code
        for index, sink in enumerate(self._sinks):
            try:
                sink.diagnostic(diagnostic)
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                self._failures.append(
                    SinkFailure(
                        sink_index=index,
                        sink_repr=repr(sink),
                        operation="diagnostic",
                        subject_id=subject,
                        exception_repr=repr(exc),
                    )
                )


@runtime_checkable
class CaptureAdapter(Protocol):
    """Parse raw event streams into normalized observations.

    Adapters can never:
      - return TransitionDrafts,
      - persist evidence (coordinator-owned),
      - call coordinator entry points,
      - mutate substrate state,
      - write durable refs.

    Adapters can:
      - emit ObservationDrafts to the sink,
      - emit Diagnostics alongside observations,
      - decline to parse (return ParseResult.skip()).
    """

    @property
    def adapter_id(self) -> str: ...
    @property
    def adapter_version(self) -> str: ...
    @property
    def mechanism(self) -> str: ...
    @property
    def evidence_kinds(self) -> tuple[str, ...]: ...

    def parse(
        self,
        context: DriverContext,
        raw_events: Sequence[Mapping[str, object]],
        sink: ObservationSink,
    ) -> ParseResult: ...


# ===========================================================================
# SubstrateDriver Protocol (Q1, Q4)
# ===========================================================================


@runtime_checkable
class SubstrateDriver(Protocol):
    """Substrate driver contract for v2 world-vector ingress.

    The typed surface (``prepare``, ``describe``, ``capture_adapters``,
    ``validate_result``) is the SPI v0.1 contract. Authors import it from
    :mod:`vcs_core.spi`; most implementations inherit
    :class:`BaseSubstrateDriver` for the default-bearing hooks.
    """

    @property
    def driver_id(self) -> str: ...

    @property
    def driver_version(self) -> str: ...

    @property
    def capabilities(self) -> CapabilitySet: ...

    def describe(self) -> DriverSchema: ...

    def prepare(
        self,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult: ...

    def capture_adapters(
        self,
        context: DriverContext,
    ) -> Sequence[CaptureAdapter]: ...

    # Per-driver semantic validation (validator layer 3). Required
    # Protocol method. Drivers with no domain-specific invariants beyond
    # layers 1-2 return ``None`` unconditionally; drivers with semantic
    # rules raise ``InvalidRepositoryStateError`` on violation. The
    # coordinator invokes this method unconditionally after layer 2.
    def validate_result(
        self,
        request: IngressRequest,
        result: DriverIngressResult,
    ) -> None: ...


# ===========================================================================
# Capture adapter registry (Q2 §Discovery)
# ===========================================================================


class CaptureAdapterRegistry:
    """Per-VcsCore-instance registry for cross-cutting capture adapters.

    Lifecycle (per SPI v0.1 §Q2 Discovery):

    - Per-``VcsCore`` instance; populated at instance construction time.
    - Frozen before the first capture parse — once frozen, registrations
      raise. Late registration would race with in-flight parse calls and
      break the Q5b fan-out delivery discipline.
    - Rejects duplicate ``adapter_id`` registrations; collisions are
      configuration errors, not silent overrides.

    Boundary with driver-default adapters: adapters returned by
    ``SubstrateDriver.capture_adapters(context)`` remain driver-owned
    and are not registered here. The registry only carries adapters
    that don't have a natural driver home (cross-cutting mechanisms
    like a patch-manager-owned PythonRuntimeCaptureAdapter). Coordinator
    iterates both sources at capture-time; duplicate ``adapter_id``
    across the two sources is also a configuration error and is the
    coordinator's responsibility to detect.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, CaptureAdapter] = {}
        self._frozen: bool = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register_capture_adapter(self, adapter: CaptureAdapter) -> None:
        if self._frozen:
            raise InvalidRepositoryStateError(
                f"capture adapter registry is frozen; cannot register {adapter.adapter_id!r}"
            )
        if adapter.adapter_id in self._adapters:
            raise InvalidRepositoryStateError(
                f"capture adapter id collision: {adapter.adapter_id!r} already registered"
            )
        self._adapters[adapter.adapter_id] = adapter

    def freeze(self) -> None:
        """Idempotent; called by the coordinator before first parse."""
        self._frozen = True

    def adapters(self) -> tuple[CaptureAdapter, ...]:
        """Snapshot; safe to iterate concurrently with parse calls."""
        return tuple(self._adapters.values())


# ===========================================================================
# BaseSubstrateDriver mixin (Phase A.1)
# ===========================================================================


@dataclass(frozen=True)
class BaseSubstrateDriver:
    """Default-bearing base for ``SubstrateDriver`` implementations.

    Structurally satisfies the ``SubstrateDriver`` Protocol when a
    subclass implements the three required hooks (``prepare``,
    ``describe``, ``capabilities``). Does not inherit from the Protocol
    directly — ``@runtime_checkable Protocol`` + ABC composition is
    awkward in Python and unnecessary here; ``isinstance(x, SubstrateDriver)``
    on a subclass instance returns ``True`` via structural conformance.

    Provides defaults for the boilerplate hooks so subclasses focus on the
    parts that genuinely vary per substrate:

    - ``capture_adapters(context)`` returns ``()``.
    - ``validate_result(request, result)`` is a no-op.

    Subclasses override the identity class attributes via dataclass
    field redeclaration with new defaults, and implement the required
    hooks (``capabilities``, ``describe``, ``prepare``). See
    ``GUIDE-implementing-a-substrate.md`` for the worked pattern.
    """

    # Identity fields — subclasses override via field redeclaration.
    driver_id: str = ""
    driver_version: str = ""
    store_id: str = ""
    binding: str = ""
    role: str = ""
    materialization_class: str = "external"

    # ------------------------------------------------------------------
    # Subclass-required hooks (stubs raise NotImplementedError).
    #
    # These stubs exist so the base class structurally satisfies the
    # ``SubstrateDriver`` Protocol — mypy and the runtime_checkable
    # isinstance check both see a method present — but every subclass
    # MUST override them. Direct instantiation + use of
    # ``BaseSubstrateDriver`` raises clearly.
    # ------------------------------------------------------------------
    @property
    def capabilities(self) -> CapabilitySet:
        raise NotImplementedError(f"{type(self).__name__} must implement the ``capabilities`` property")

    def describe(self) -> DriverSchema:
        commands = self.derived_command_specs()
        if commands:
            return DriverSchema(
                driver_id=self.driver_id,
                driver_version=self.driver_version,
                capabilities=self.capabilities,
                commands=commands,
            )
        raise NotImplementedError(f"{type(self).__name__} must implement the ``describe`` method")

    def prepare(
        self,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult:
        del context, request
        raise NotImplementedError(f"{type(self).__name__} must implement the ``prepare`` method")

    # ------------------------------------------------------------------
    # Default-bearing hooks (subclasses override only when needed).
    # ------------------------------------------------------------------
    def capture_adapters(
        self,
        context: DriverContext,
    ) -> tuple[CaptureAdapter, ...]:
        """Default: driver has no driver-default capture adapters.

        Substrates with capture mechanisms tied to the driver's lifetime
        override this to return their adapter(s). Cross-cutting adapters
        live in ``CaptureAdapterRegistry`` instead; see SPI doc §Q2
        Discovery boundary criterion.
        """
        del context
        return ()

    def derived_command_specs(self) -> dict[str, CommandSpec]:
        """Return schemas for methods marked with ``@command``.

        No decorated method is invoked by this helper. A subclass can call it
        from a hand-authored ``describe()`` override to merge intentionally.
        """
        return {name: binding.command_spec for name, binding in _decorated_command_bindings(type(self)).items()}

    def dispatch_decorated_command(
        self,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult:
        """Invoke a decorated command method for drivers that opt in.

        Explicit ``prepare()`` arms stay authoritative because this helper is
        only reached when driver code calls it. Runtime command authority lives
        at the resolved binding contract boundary; this driver-local helper
        still assumes a stable ``describe()`` implementation when it compiles
        the decorated command's invocation contract.
        """
        if not isinstance(request, CommandRequest):
            raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))
        bindings = _decorated_command_bindings(type(self))
        binding = bindings.get(request.command)
        if binding is None:
            raise UnsupportedRequestError(driver_id=self.driver_id, request_type=type(request))

        from vcs_core._command_contract import compile_command_contract, normalize_command_params

        contract = compile_command_contract(
            self.describe(),
            request.command,
            binding_name=self.binding or self.driver_id,
        )
        params = normalize_command_params(contract, request.params).params
        method = getattr(self, binding.attr_name)
        if binding.context_delivery == "positional":
            result = method(context, **params)
        elif binding.context_delivery == "keyword":
            result = method(context=context, **params)
        else:
            result = method(**params)
        if not isinstance(result, DriverIngressResult):
            raise TypeError(
                f"decorated command {self.driver_id}.{request.command} returned "
                f"{type(result).__name__}, expected DriverIngressResult"
            )
        return result

    def validate_result(
        self,
        request: IngressRequest,
        result: DriverIngressResult,
    ) -> None:
        """Default: no domain-specific invariants beyond validator layers 1-2.

        Subclasses with semantic rules raise
        ``InvalidRepositoryStateError`` on violation. Layers 1 (generic)
        and 2 (per-request) run unconditionally before this is invoked.
        """
        del request, result


def _decorated_command_bindings(driver_cls: type[object]) -> dict[str, _DecoratedCommandBinding]:
    bindings: dict[str, _DecoratedCommandBinding] = {}
    attr_by_command: dict[str, str] = {}
    command_by_attr: dict[str, str] = {}
    for cls in reversed(driver_cls.mro()):
        for attr_name, raw_attr in cls.__dict__.items():
            func = _unwrap_descriptor(raw_attr)
            metadata = getattr(func, _COMMAND_METADATA_ATTR, None)
            if metadata is None:
                previous_command = command_by_attr.pop(attr_name, None)
                if previous_command is not None:
                    bindings.pop(previous_command, None)
                    attr_by_command.pop(previous_command, None)
                continue
            if not isinstance(metadata, _CommandDecoratorMetadata):
                raise TypeError(f"invalid @command metadata on {driver_cls.__name__}.{attr_name}")
            previous_command = command_by_attr.get(attr_name)
            if previous_command is not None and previous_command != metadata.name:
                bindings.pop(previous_command, None)
                attr_by_command.pop(previous_command, None)
            existing_attr = attr_by_command.get(metadata.name)
            if existing_attr is not None and existing_attr != attr_name:
                raise InvalidRepositoryStateError(
                    f"decorated command name collision for {metadata.name!r}: {existing_attr!r} and {attr_name!r}"
                )
            binding = _derive_decorated_command_binding(attr_name, func, metadata)
            bindings[metadata.name] = binding
            attr_by_command[metadata.name] = attr_name
            command_by_attr[attr_name] = metadata.name
    return bindings


def _unwrap_descriptor(raw_attr: object) -> object:
    if isinstance(raw_attr, (staticmethod, classmethod)):
        return raw_attr.__func__
    return raw_attr


def _derive_decorated_command_binding(
    attr_name: str,
    func: object,
    metadata: _CommandDecoratorMetadata,
) -> _DecoratedCommandBinding:
    if not callable(func):
        raise TypeError(f"@command target {attr_name!r} is not callable")
    sig = signature(func)
    type_hints = _safe_type_hints(func)
    command_params: dict[str, ParamSpec] = {}
    context_delivery: Literal["absent", "positional", "keyword"] = "absent"
    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        if param_name == "context":
            context_delivery = _validate_context_parameter(param)
            continue
        if param.kind is not Parameter.KEYWORD_ONLY:
            raise InvalidRepositoryStateError(
                f"decorated command method {attr_name!r} parameter {param_name!r} must be keyword-only"
            )
        command_params[param_name] = _derive_param_spec(param, type_hints.get(param_name, param.annotation))

    description = metadata.description or _doc_summary(func) or metadata.name
    command_spec = CommandSpec(
        description=description,
        params=command_params,
        examples=metadata.examples,
        projectable=metadata.projectable,
        required_one_of=metadata.required_one_of,
    )
    return _DecoratedCommandBinding(
        attr_name=attr_name,
        metadata=metadata,
        command_spec=command_spec,
        context_delivery=context_delivery,
    )


def _safe_type_hints(func: object) -> dict[str, object]:
    try:
        return get_type_hints(func, include_extras=True)
    except Exception:  # noqa: BLE001 - forward-ref failures degrade to raw annotations
        annotations = getattr(func, "__annotations__", {})
        return dict(annotations) if isinstance(annotations, Mapping) else {}


def _validate_context_parameter(param: Parameter) -> Literal["positional", "keyword"]:
    if param.kind not in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}:
        raise InvalidRepositoryStateError("decorated command context parameter must be positional or keyword-only")
    if param.kind is Parameter.KEYWORD_ONLY:
        return "keyword"
    return "positional"


def _derive_param_spec(param: Parameter, annotation: object) -> ParamSpec:
    base_annotation, metadata_items = _split_annotated(annotation)
    param_type, default_projectable = _param_type_from_annotation(base_annotation)
    description = ""
    choices: tuple[object, ...] = ()
    repeated = False
    projectable = default_projectable
    signature_has_default = param.default is not Parameter.empty
    signature_default = None if param.default is Parameter.empty else param.default
    required = not signature_has_default
    has_default = signature_has_default
    default = signature_default

    for item in metadata_items:
        if isinstance(item, ParamSpec):
            param_type = item.type
            required = item.required
            description = item.description
            if item.has_default:
                has_default = True
                default = item.default
            else:
                has_default = signature_has_default
                default = signature_default
            choices = item.choices
            repeated = item.repeated
            projectable = item.projectable
            continue
        if isinstance(item, Mapping):
            description = str(item.get("description", description))
            if "type" in item:
                param_type = str(item["type"])
            if "required" in item:
                required = bool(item["required"])
            if "has_default" in item:
                has_default = bool(item["has_default"])
            if "default" in item:
                default = item["default"]
                has_default = True
            if "choices" in item:
                raw_choices = item["choices"]
                if not isinstance(raw_choices, tuple):
                    raise InvalidRepositoryStateError(
                        f"decorated command method parameter {param.name!r} choices metadata must be a tuple"
                    )
                choices = raw_choices
            if "repeated" in item:
                repeated = bool(item["repeated"])
            if "projectable" in item:
                projectable = bool(item["projectable"])

    return ParamSpec(
        type=param_type,
        required=required,
        description=description,
        has_default=has_default,
        default=default,
        choices=choices,
        repeated=repeated,
        projectable=projectable,
    )


def _split_annotated(annotation: object) -> tuple[object, tuple[object, ...]]:
    if annotation is Signature.empty:
        return object, ()
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        return args[0], tuple(args[1:])
    return annotation, ()


def _param_type_from_annotation(annotation: object) -> tuple[str, bool]:
    if annotation is Signature.empty or annotation is Any:
        return "object", True
    if annotation is str:
        return "str", True
    if annotation is int:
        return "int", True
    if annotation is float:
        return "float", True
    if annotation is bool:
        return "bool", True
    if annotation is bytes:
        return "bytes", True
    if annotation is object:
        return "object", True
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        args = tuple(arg for arg in get_args(annotation) if arg is not type(None))
        if len(args) == 1:
            inner_type, projectable = _param_type_from_annotation(args[0])
            return f"{inner_type.removesuffix('?')}?", projectable
    if annotation is list or origin is list:
        return "list", True
    if annotation is dict or origin is dict:
        return "object", True
    return _annotation_name(annotation), False


def _annotation_name(annotation: object) -> str:
    name = getattr(annotation, "__name__", None)
    if isinstance(name, str):
        return name
    return str(annotation).replace("typing.", "")


def _doc_summary(func: object) -> str:
    doc = getattr(func, "__doc__", None)
    if not isinstance(doc, str):
        return ""
    for line in doc.strip().splitlines():
        summary = line.strip()
        if summary:
            return summary
    return ""


# ===========================================================================
# Errors
# ===========================================================================


class SubstrateContractError(InvalidRepositoryStateError):
    """Base class for SPI v0.1 substrate-driver contract violations (Phase D).

    Specific contract failures inherit from this base so consumers
    (e.g., the query plane's ``explain()`` result, admission blocker
    DTOs) can match on the base class to identify all SPI contract
    violations, or on a specific subclass to handle particular failure
    modes. Pre-T4a all of these failure modes routed through
    ``InvalidRepositoryStateError`` directly — too coarse for admission
    blocker citations that want to discriminate failure kinds.
    """


class UnsupportedRequestError(SubstrateContractError):
    """Coordinator pre-flight rejection — driver does not accept this request type."""

    def __init__(self, *, driver_id: str, request_type: type[IngressRequest]) -> None:
        super().__init__(f"driver {driver_id!r} does not accept request type {request_type.__name__}")
        self.driver_id = driver_id
        self.request_type = request_type


class DriverAuthorityRequiredError(SubstrateContractError):
    """Driver handler refused because the caller lacks orchestrator-only authority."""

    def __init__(self, message: str = "driver command requires orchestrator authority") -> None:
        super().__init__(message)


class CapabilityContractViolation(SubstrateContractError):  # noqa: N818 - public SPI name
    """A driver's ``capabilities.accepts`` advertises a type its handler does not support.

    Per SPI v0.1 §Result Shape "Capabilities are a runtime contract",
    every type in ``capabilities.accepts`` must have a working handler
    at the commit landing the declaration. Aspirational entries (type
    in ``accepts`` whose handler raises ``NotImplementedError``) are
    contract violations because consumers reading ``describe()`` cannot
    distinguish "supported, sometimes empty" from "not yet wired."
    This is the symmetric exception companion to Phase A.2's positive
    contract test.
    """

    def __init__(self, *, driver_id: str, request_type: type[IngressRequest], detail: str | None = None) -> None:
        message = (
            f"driver {driver_id!r} advertises {request_type.__name__} in "
            f"capabilities.accepts but its handler is not wired"
        )
        if detail is not None:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.driver_id = driver_id
        self.request_type = request_type
        self.detail = detail


class EvidenceKindReconciliationError(SubstrateContractError):
    """A driver emitted an observation whose evidence_kind is not in any declared adapter's set.

    Per SPI v0.1 §Q4 Evidence-kind reconciliation, every observation
    ``evidence_kind`` a driver returns must appear in some
    ``describe().capture_adapters[*].evidence_kinds`` set. This
    prevents the failure mode where the supervisor's pattern matcher
    (``Pattern.event(...)`` bound to ``evidence_kind``) silently
    misses observations whose kind doesn't match any declared adapter.
    """

    def __init__(
        self,
        *,
        driver_id: str,
        evidence_kind: str,
        declared_kinds: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            f"driver {driver_id!r} emitted observation with evidence_kind={evidence_kind!r} "
            f"not in any declared adapter's evidence_kinds (declared: {sorted(declared_kinds)!r})"
        )
        self.driver_id = driver_id
        self.evidence_kind = evidence_kind
        self.declared_kinds = declared_kinds


class SurfacePolicyError(SubstrateContractError):
    """ActiveSurface rejected a request type or a result evidence/op kind."""

    def __init__(
        self,
        *,
        driver_id: str,
        reason: str,
        offending: str | None = None,
        operation: str | None = None,
    ) -> None:
        message = f"driver {driver_id!r}: surface policy denied: {reason}"
        if offending is not None:
            message = f"{message} ({offending!r})"
        super().__init__(message)
        self.driver_id = driver_id
        self.reason = reason
        self.offending = offending
        self.operation = operation


# ===========================================================================
# Validator: three named layers (per §Result Shape and Validator Architecture)
# ===========================================================================


_AUTHORITY_FIELD_NAMES = frozenset(
    {
        "authority_ref",
        "candidate_ref",
        "candidate_refs",
        "evidence_ref",
        "evidence_refs",
        "journal_ref",
        "publication_lease",
        "publication_ref",
        "retention_receipt",
        "retention_ref",
        "selected_head_pin",
        "world_publication_ref",
    }
)
_RESERVED_REF_PREFIXES = (
    "refs/vcscore/archives/operations/",
    "refs/vcscore/candidates/",
    "refs/vcscore/evidence-only/",
    "refs/vcscore/evidence/",
    "refs/vcscore/ops/",
    "refs/vcscore/pins/world/",
    "refs/vcscore/publishing/leases/",
    "refs/vcscore/retention/",
    "refs/vcscore/scopes/",
    "refs/vcscore/worlds/",
)
_RESERVED_EXACT_REFS = frozenset({"refs/vcscore/ground"})
_DRIVER_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _validate_generic_invariants(result: DriverIngressResult) -> None:
    """Layer 1: generic invariants over any DriverIngressResult.

    No authority refs, no ValidatedPayloadDescriptor outputs, no reserved
    control-plane fields, no dangling observation local ids, retention
    hints are advisory only. Independent of request type.
    """
    observation_ids: set[str] = set()
    for observation in result.observations:
        _validate_observation_draft(observation)
        if observation.observation_id in observation_ids:
            raise InvalidRepositoryStateError(f"duplicate driver observation_id: {observation.observation_id!r}")
        observation_ids.add(observation.observation_id)

    for transition in result.transitions:
        if not transition.transition_id:
            raise InvalidRepositoryStateError("driver transition_id is required")
        if not transition.semantic_op:
            raise InvalidRepositoryStateError("driver transition semantic_op is required")
        if isinstance(transition.payload_descriptor_claim, ValidatedPayloadDescriptor):
            raise InvalidRepositoryStateError(
                "driver transition must not emit coordinator-validated payload descriptor"
            )
        _validate_str_tuple(
            transition.evidence_citation_ids,
            path=f"transition[{transition.transition_id}].evidence_citation_ids",
        )
        if len(set(transition.evidence_citation_ids)) != len(transition.evidence_citation_ids):
            raise InvalidRepositoryStateError("driver transition evidence_citation_ids must not contain duplicates")
        dangling = tuple(
            observation_id for observation_id in transition.observation_ids if observation_id not in observation_ids
        )
        if dangling:
            raise InvalidRepositoryStateError(f"driver transition references unknown observations: {dangling!r}")
        _reject_authority_control_plane(transition.metadata, path=f"transition[{transition.transition_id}].metadata")
        if transition.git_tree_oid is not None:
            _validate_git_oid(
                transition.git_tree_oid,
                path=f"transition[{transition.transition_id}].git_tree_oid",
            )
        if transition.content is not None:
            _validate_revision_content_draft(transition.content, path=f"transition[{transition.transition_id}].content")

    for index, hint in enumerate(result.retention_hints):
        _validate_retention_hint(hint, path=f"retention_hints[{index}]")

    for selection in result.selection_requirements:
        _validate_selection_requirement_draft(selection)

    for index, diagnostic in enumerate(result.diagnostics):
        if not isinstance(diagnostic, Diagnostic):
            raise InvalidRepositoryStateError("driver diagnostics must be Diagnostic instances")
        _reject_authority_control_plane(diagnostic.detail, path=f"diagnostics[{index}].detail")


def _validate_per_request_invariants(
    request: IngressRequest,
    result: DriverIngressResult,
) -> None:
    """Layer 2: per-request invariants (Q1 / §Result Shape table)."""
    if isinstance(request, CaptureRequest):
        if result.transitions:
            raise InvalidRepositoryStateError(
                "CaptureRequest result must not contain transitions; transitions are produced by ReduceRequest"
            )
        return

    if isinstance(request, ReduceRequest):
        # T2c: ReduceRequest results MAY contain observations representing
        # reduction outputs (e.g., the ``reduce:reduced-state-proof``
        # observation that documents the proof linking the request's
        # citations to the produced state manifest). The pre-T2 invariant
        # ("ReduceRequest result must not contain observations") was based
        # on a simplified mental model — reality is that reduction can
        # produce a NEW observation kind (the proof) that wants persistence
        # as durable evidence. The semantic distinction is preserved
        # elsewhere: the original observation evidence (overlay fs events,
        # python-runtime writes) IS coordinator-persisted before the
        # reduce stage and arrives as ``request.evidence_citations``;
        # observations in the result are strictly reduction-output kinds.
        allowed_citation_ids = frozenset(citation.citation_id for citation in request.evidence_citations.citations)
        for transition in result.transitions:
            for citation_id in transition.evidence_citation_ids:
                if citation_id not in allowed_citation_ids:
                    raise InvalidRepositoryStateError(
                        f"ReduceRequest transition cites evidence citation {citation_id!r} not in the request's batch"
                    )
        return

    if isinstance(request, (CommandRequest, ScanRequest, MergeRequest)):
        # Diagnostic-only results are admitted; no further per-request rule.
        return

    # Exhaustiveness sentinel - new IngressRequest variants must be
    # handled in one of the branches above. mypy reports the assert_never
    # call as unreachable when the discriminated union is fully covered;
    # that is exactly the signal we want. The ``type: ignore`` keeps
    # the line as a runtime safety net without producing a false-positive
    # build error.
    assert_never(request)


def validate_driver_ingress(
    request: IngressRequest,
    result: DriverIngressResult,
    driver: SubstrateDriver | None = None,
    schema: DriverSchema | None = None,
) -> None:
    """Three-layer validator entry point (SPI v0.1).

    Layer 1 runs unconditionally and is the bouncer at the SPI boundary.
    Layer 2 runs after layer 1 succeeds. Layer 3 runs after layer 2
    succeeds when ``driver`` is supplied; the driver's
    ``validate_result`` is required at the Protocol level and is
    invoked unconditionally — the no-op shape is normative for drivers
    without domain-specific rules.
    """
    _validate_generic_invariants(result)
    _validate_per_request_invariants(request, result)
    if driver is not None:
        _validate_storage_profile_result(driver, result, schema=schema)
        driver.validate_result(request, result)


def validate_driver_ingress_result(result: DriverIngressResult) -> None:
    """One-arg validator entry point (layer 1 only).

    Validates the result's generic invariants without a request in hand.
    Callers that have the request use :func:`validate_driver_ingress`, which
    additionally runs layer 2 (per-request) and layer 3 (the driver's
    ``validate_result``).
    """
    _validate_generic_invariants(result)


def _validate_storage_profile_result(
    driver: SubstrateDriver,
    result: DriverIngressResult,
    *,
    schema: DriverSchema | None = None,
) -> None:
    """Enforce the driver's declared storage profile against emitted transitions."""
    profile = (schema or driver.describe()).storage_profile
    for transition in result.transitions:
        path = f"transition[{transition.transition_id}]"
        if profile.shape == "json-snapshot":
            if transition.content is not None:
                raise InvalidRepositoryStateError(
                    f"driver {driver.driver_id!r} declares json-snapshot storage but {path}.content is present"
                )
            continue
        if profile.shape == "keyed-json-tree":
            if transition.git_tree_oid is not None:
                raise InvalidRepositoryStateError(
                    f"driver {driver.driver_id!r} declares keyed-json-tree storage but {path}.git_tree_oid is set"
                )
            if not isinstance(transition.content, KeyedJsonTreeDraft):
                raise InvalidRepositoryStateError(
                    f"driver {driver.driver_id!r} declares keyed-json-tree storage but {path}.content "
                    "is not KeyedJsonTreeDraft"
                )
            if transition.content.manifest != transition.payload:
                raise InvalidRepositoryStateError(
                    f"driver {driver.driver_id!r} keyed-json-tree manifest disagrees with transition payload"
                )
            continue
        raise InvalidRepositoryStateError(
            f"driver {driver.driver_id!r} declares unsupported storage shape {profile.shape!r} for {path}"
        )


def _validate_observation_draft(observation: ObservationDraft) -> None:
    if not observation.observation_id:
        raise InvalidRepositoryStateError("driver observation_id is required")
    path = f"observation[{observation.observation_id}]"
    _require_non_empty_str(observation.evidence_kind, path=f"{path}.evidence_kind")
    if not isinstance(observation.stable_observation, dict):
        raise InvalidRepositoryStateError(f"driver {path}.stable_observation must be an object")
    _validate_optional_str(observation.observed_head, path=f"{path}.observed_head")
    if observation.observed_at_unix_ns is not None and not isinstance(observation.observed_at_unix_ns, int):
        raise InvalidRepositoryStateError(f"driver {path}.observed_at_unix_ns must be an integer when present")
    _validate_optional_str(observation.mechanism, path=f"{path}.mechanism")
    _validate_optional_str(observation.correlation_id, path=f"{path}.correlation_id")
    if isinstance(observation.evidence_payload_descriptor_claim, ValidatedPayloadDescriptor):
        raise InvalidRepositoryStateError("driver observation must not emit coordinator-validated payload descriptor")
    if observation.evidence_payload_descriptor_claim is not None and not isinstance(
        observation.evidence_payload_descriptor_claim,
        PayloadDescriptorClaim,
    ):
        raise InvalidRepositoryStateError(f"driver {path}.evidence_payload_descriptor_claim is invalid")

    _reject_authority_control_plane(observation.evidence_kind, path=f"{path}.evidence_kind")
    _reject_authority_control_plane(observation.stable_observation, path=f"{path}.stable_observation")
    _reject_authority_control_plane(observation.observed_head, path=f"{path}.observed_head")
    _reject_authority_control_plane(observation.mechanism, path=f"{path}.mechanism")
    _reject_authority_control_plane(observation.correlation_id, path=f"{path}.correlation_id")
    _reject_authority_control_plane(
        observation.evidence_payload_descriptor_claim,
        path=f"{path}.evidence_payload_descriptor_claim",
    )
    _reject_authority_control_plane(observation.metadata, path=f"{path}.metadata")


def _validate_revision_content_draft(content: RevisionContentDraft, *, path: str) -> None:
    if isinstance(content, KeyedJsonTreeDraft):
        if not isinstance(content.manifest, dict):
            raise InvalidRepositoryStateError(f"driver {path}.manifest must be an object")
        _validate_optional_str(content.base_head, path=f"{path}.base_head")
        _validate_content_root(content.content_root, path=f"{path}.content_root")
        seen_paths: set[str] = set()
        for index, put in enumerate(content.puts):
            if not isinstance(put, KeyedJsonPut):
                raise InvalidRepositoryStateError(f"driver {path}.puts[{index}] must be a KeyedJsonPut")
            _require_non_empty_str(put.key, path=f"{path}.puts[{index}].key")
            _validate_relative_content_path(put.path, path=f"{path}.puts[{index}].path")
            if put.path in seen_paths:
                raise InvalidRepositoryStateError(f"driver {path}.puts contains duplicate path {put.path!r}")
            if put.path in content.deletes:
                raise InvalidRepositoryStateError(f"driver {path}.puts path is also deleted: {put.path!r}")
            if not isinstance(put.payload, dict):
                raise InvalidRepositoryStateError(f"driver {path}.puts[{index}].payload must be an object")
            seen_paths.add(put.path)
        seen_deletes: set[str] = set()
        for index, deleted in enumerate(content.deletes):
            _validate_relative_content_path(deleted, path=f"{path}.deletes[{index}]")
            if deleted in seen_deletes:
                raise InvalidRepositoryStateError(f"driver {path}.deletes contains duplicate path {deleted!r}")
            seen_deletes.add(deleted)
        return
    raise InvalidRepositoryStateError(f"driver {path} has unsupported revision content draft")


def _validate_content_root(value: object, *, path: str) -> None:
    root = _require_non_empty_str(value, path=path)
    if "/" in root or root in {".", "..", "meta", "workspace"}:
        raise InvalidRepositoryStateError(f"driver {path} must be a single non-reserved path component")


def _validate_relative_content_path(value: object, *, path: str) -> None:
    item = _require_non_empty_str(value, path=path)
    if item.startswith("/") or item.endswith("/") or "//" in item:
        raise InvalidRepositoryStateError(f"driver {path} must be a relative file path")
    if any(part in {"", ".", ".."} for part in item.split("/")):
        raise InvalidRepositoryStateError(f"driver {path} must not contain empty, '.', or '..' path segments")


def validate_driver_identity(*, driver_id: str, driver_version: str) -> None:
    """Reject driver identity values that cannot round-trip through transition records."""
    _validate_driver_identity_value(driver_id, path="driver_id")
    _validate_driver_identity_value(driver_version, path="driver_version")


def _validate_retention_hint(hint: RetentionHint, *, path: str) -> None:
    if hint.mandatory:
        raise InvalidRepositoryStateError("driver retention hints are advisory and must not be mandatory")
    _reject_authority_control_plane(hint.kind, path=f"{path}.kind")
    _reject_authority_control_plane(hint.target, path=f"{path}.target")
    _reject_authority_control_plane(hint.metadata, path=f"{path}.metadata")


def _validate_selection_requirement_draft(selection: DriverSelectionRequirementDraft) -> None:
    path = f"selection[{selection.binding}]"
    _require_non_empty_str(selection.binding, path=f"{path}.binding")
    _require_non_empty_str(selection.role, path=f"{path}.role")
    _require_non_empty_str(selection.selection_kind, path=f"{path}.selection_kind")
    _validate_optional_str(selection.transition_id, path=f"{path}.transition_id")
    _reject_authority_control_plane(selection.binding, path=f"{path}.binding")
    _reject_authority_control_plane(selection.role, path=f"{path}.role")
    _reject_authority_control_plane(selection.selection_kind, path=f"{path}.selection_kind")
    _reject_authority_control_plane(selection.transition_id, path=f"{path}.transition_id")
    _reject_authority_control_plane(selection.metadata, path=f"{path}.metadata")
    for index, hint in enumerate(selection.retention_hints):
        _validate_retention_hint(hint, path=f"{path}.retention_hints[{index}]")


def _reject_authority_control_plane(value: object, *, path: str) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if _is_reserved_authority_ref(value):
            raise InvalidRepositoryStateError(f"driver control-plane field {path} contains reserved authority ref")
        return
    if isinstance(value, ValidatedPayloadDescriptor):
        raise InvalidRepositoryStateError(f"driver control-plane field {path} contains validated payload descriptor")
    if isinstance(value, Mapping):
        keys = set(value)
        reserved = keys & _AUTHORITY_FIELD_NAMES
        if reserved:
            raise InvalidRepositoryStateError(
                f"driver control-plane field {path} contains reserved authority fields: {sorted(reserved)!r}"
            )
        if value.get("schema") == VALIDATED_PAYLOAD_DESCRIPTOR_SCHEMA:
            raise InvalidRepositoryStateError(
                f"driver control-plane field {path} contains validated payload descriptor"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidRepositoryStateError(f"driver control-plane field {path} contains non-string key")
            if _is_reserved_authority_ref(key):
                raise InvalidRepositoryStateError(
                    f"driver control-plane field {path}.{key} contains reserved authority ref"
                )
            _reject_authority_control_plane(item, path=f"{path}.{key}")
        return
    if isinstance(value, (tuple, list, frozenset)):
        for index, item in enumerate(value):
            _reject_authority_control_plane(item, path=f"{path}[{index}]")


def _require_non_empty_str(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidRepositoryStateError(f"driver {path} is required")
    return value


def _validate_optional_str(value: object, *, path: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise InvalidRepositoryStateError(f"driver {path} must be a non-empty string when present")


def _validate_str_tuple(value: object, *, path: str) -> None:
    if not isinstance(value, tuple):
        raise InvalidRepositoryStateError(f"driver {path} must be a tuple")
    for index, item in enumerate(value):
        _require_non_empty_str(item, path=f"{path}[{index}]")
        _reject_authority_control_plane(item, path=f"{path}[{index}]")


def _validate_driver_identity_value(value: object, *, path: str) -> str:
    identity = _require_non_empty_str(value, path=path)
    if _is_reserved_authority_ref(identity):
        raise InvalidRepositoryStateError(f"driver {path} must not be a reserved authority ref")
    if _DRIVER_IDENTITY_RE.fullmatch(identity) is None:
        raise InvalidRepositoryStateError(f"driver {path} contains unsupported characters")
    return identity


def _is_reserved_authority_ref(value: str) -> bool:
    return value in _RESERVED_EXACT_REFS or value.startswith(_RESERVED_REF_PREFIXES)


def _validate_git_oid(value: object, *, path: str) -> None:
    if not isinstance(value, str) or len(value) != 40:
        raise InvalidRepositoryStateError(f"driver {path} must be a 40-character hex Git oid")
    if any(c not in "0123456789abcdef" for c in value):
        raise InvalidRepositoryStateError(f"driver {path} must be a 40-character hex Git oid")


__all__ = [  # noqa: RUF022 - grouped by SPI surface area, not alphabetically sorted
    "SUBSTRATE_DRIVER_CONTRACT_REVISION",
    # Diagnostic
    "Diagnostic",
    # Typed ingress family
    "CommandRequest",
    "ScanRequest",
    "CaptureRequest",
    "ReduceRequest",
    "MergeRequest",
    "IngressRequest",
    # Capabilities / surface
    "CapabilitySet",
    "AuthorityRole",
    "CrashLagOrdering",
    "GrowthBound",
    "ReadSafety",
    "RevisionStorageProfile",
    "RevisionStorageShape",
    "ActiveSurface",
    # Context
    "ChildWorldResolver",
    "ChildWorldSnapshot",
    "DriverContext",
    # Draft DTOs
    "DriverIngressResult",
    "DriverSelectionRequirementDraft",
    "EvidenceCitation",
    "ObservationDraft",
    "ReductionBatch",
    "RetentionHint",
    "KeyedJsonPut",
    "KeyedJsonTreeDraft",
    "RevisionContentDraft",
    "TransitionDraft",
    # Introspection
    "CaptureAdapterSchema",
    "CommandSpec",
    "DriverSchema",
    "MergeSpec",
    "ParamSpec",
    "ScanSpec",
    # CaptureAdapter family
    "CaptureAdapter",
    "CaptureAdapterRegistry",
    "FanOutSink",
    "ObservationSink",
    "ParseResult",
    "SinkFailure",
    "TupleSink",
    # SubstrateDriver
    "BaseSubstrateDriver",
    "SubstrateDriver",
    "command",
    # Errors
    "CapabilityContractViolation",
    "DriverAuthorityRequiredError",
    "EvidenceKindReconciliationError",
    "SubstrateContractError",
    "SurfacePolicyError",
    "UnsupportedRequestError",
    # Validators
    "validate_driver_identity",
    "validate_driver_ingress",
    "validate_driver_ingress_result",
]
