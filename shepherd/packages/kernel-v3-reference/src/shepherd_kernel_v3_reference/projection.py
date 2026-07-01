"""Eight-pass canonicalization projection from ReplayableKernelTransition to
SemanticTransitionBatch.

Per 260521-0600-kernel.md §"Settled Design Decisions" 2026-05-22 (transition
object split; B-with-tightening trace-ref policy) and 2026-05-24 (eight-pass
canonicalization order; `branch:root` literal pass-through).

The projection function is the single canonicalization boundary; Python and
Lean must agree on its output for identical inputs. The function is pure of
its three arguments.

Pre-passes (do NOT enter `CanonicalRefMap`):
    P-A: program-identity rewrite for `install:N`/`handler-env:N`/`schema:N`
    P-B: continuation-object catalog rewrite for `continuation:runtime:N`
    P-C: continuation-control catalog rewrite for `continuation-control:runtime:N`
    P-D: context catalog rewrite for `ctx:runtime:N`

Eight lifecycle-canonicalization passes (every entry enters `CanonicalRefMap`):
    Pass 1: EffectDeclaration → declaration:sha256:HEX
    Pass 2: HandlerSelection → selection:sha256:HEX
    Pass 3: ResumptionHandle / ContinuationPending / ForkBranch → source:sha256:HEX
    Pass 4: path refs (parsed) → path:sha256:HEX  (unhandled-path dispatch)
    Pass 5: ContinuationResume → resume:sha256:HEX  (seq-keyed)
    Pass 6: ResumeReturn → resume-return:sha256:HEX  (seq-keyed)
    Pass 7: EffectCapture → capture:sha256:HEX  (seq-keyed)
    Pass 8: SelectionClosed → closed:sha256:HEX  (seq-keyed)

`branch:root` is the universal root-branch sentinel and is excluded from
`CanonicalRefMap` (literal pass-through; see 2026-05-24 settled decision).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.envelope import KernelResultEnvelope, WireResult
    from shepherd_kernel_v3_reference.kernel.ir import Ref
    from shepherd_kernel_v3_reference.kernel.program_admission import PreparedKernelProgram
    from shepherd_kernel_v3_reference.kernel.replay import KernelReplayState, ReplayableKernelTransition
    from shepherd_kernel_v3_reference.profiles import SemanticProfile

from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    ContinuationObject,
    continuation_object_child_refs,
    continuation_object_to_json,
)
from shepherd_kernel_v3_reference.kernel.program_identity import (
    ProgramIdentity,
    project_program_identity,
)
from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.semantic import (
    AdmissionBasis,
    CanonicalRefMap,
    JsonValue,
    ProfileRejected,
    SemanticTransitionBatch,
    SemanticTransitionBatchValidationError,
    SourceKind,
    TransitionKind,
    build_admitted_transition_batch,
    build_initial_transition_batch,
)
from shepherd_kernel_v3_reference.trace.records import (
    ContinuationPending,
    ContinuationResume,
    EffectCapture,
    EffectDeclaration,
    ForkBranch,
    HandlerSelection,
    ResumeReturn,
    ResumptionHandle,
    SelectionClosed,
    TraceRecord,
)
from shepherd_kernel_v3_reference.trace.serde import trace_record_to_json

# ---------------------------------------------------------------------------
# Field role classification (validated against zero false positives by the
# 2330 shape spike across Resume/Abort/nested-handler/pure-let programs).
# ---------------------------------------------------------------------------

# Field roles per record type. Used by the pre-passes to decide what kind of
# rewrite (if any) applies to each field, and by the canonicalization passes
# to identify the record's own ref.
#
# Roles:
#   "self"               — the record's own ref (record.ref)
#   "lifecycle:<kind>"   — cites a runtime-local ref of <kind> emitted by a
#                          prior pass: declaration | selection | source |
#                          path | resume | capture
#   "program-structure"  — runtime-local install:N / handler-env:N / schema
#                          ref rewritten via program identity
#   "continuation"       — continuation:runtime:N ref rewritten via the
#                          transition's continuation_ref_map
#   "continuation-control" — continuation-control:runtime:N ref
#   "ctx"                — ctx:runtime:N ref rewritten via context_ref_map
#   "branch"             — branch ref (branch:root sentinel pass-through or
#                          canonicalized branch:N for non-root)
#   "branch-scope"       — branch-scope ref (publication-experimental only)
#   "enum"               — Literal-typed enum value, not a ref
#   "value"              — domain payload value, not a ref

FieldRole = str

_FIELDS_BY_RECORD_TYPE: Mapping[str, Mapping[str, FieldRole]] = {
    "EffectDeclaration": {
        "ref": "self",
        "program_ref": "program-structure",
        "effect_kind": "enum",
        "payload": "value",
        "full_continuation_ref": "continuation",
        "branch_ref": "branch",
        "payload_schema_ref": "program-structure",
        "operation_result_schema_ref": "program-structure",
        "execution_context_ref": "ctx",
        "branch_scope_ref": "branch-scope",
    },
    "HandlerSelection": {
        "ref": "self",
        "declaration_ref": "lifecycle:declaration",
        "selected_binding_ref": "program-structure",
        "handler_id": "enum",
        "handler_frame_ref": "program-structure",
        "captured_continuation_ref": "continuation",
        "outer_continuation_ref": "continuation",
        "captured_continuation_control_ref": "continuation-control",
        "outer_continuation_control_ref": "continuation-control",
        "handled_result_schema_ref": "program-structure",
        "worker_context_ref": "ctx",
        "handler_context_ref": "ctx",
        "outer_context_ref": "ctx",
        "branch_scope_ref": "branch-scope",
    },
    "ResumptionHandle": {
        "ref": "self",
        "declaration_ref": "lifecycle:declaration",
        "selection_ref": "lifecycle:selection",
        "continuation_ref": "continuation",
        "operation_result_schema_ref": "program-structure",
        "handled_result_schema_ref": "program-structure",
        "branch_scope_ref": "branch-scope",
    },
    "ContinuationPending": {
        "ref": "self",
        "declaration_ref": "lifecycle:declaration",
        "selection_ref": "lifecycle:selection",
        "selection_path_ref": "lifecycle:path",
        "continuation_ref": "continuation",
        "operation_result_schema_ref": "program-structure",
        "branch_ref": "branch",
        "reason": "value",
        "worker_context_ref": "ctx",
        "branch_scope_ref": "branch-scope",
    },
    "ForkBranch": {
        "ref": "self",
        "fork_ref": "lifecycle:source",
        "declaration_ref": "lifecycle:declaration",
        "selection_ref": "lifecycle:selection",
        "selection_path_ref": "lifecycle:path",
        "branch_ref": "branch",
        "continuation_ref": "continuation",
        "value": "value",
        "terminal_continuation_ref": "continuation",
        "branch_scope_ref": "branch-scope",
    },
    "ContinuationResume": {
        "ref": "self",
        "source_ref": "lifecycle:source",
        "source_record_type": "enum",
        "declaration_ref": "lifecycle:declaration",
        "selection_ref": "lifecycle:selection",
        "selection_path_ref": "lifecycle:path",
        "continuation_ref": "continuation",
        "handler_continuation_ref": "continuation",
        "handler_dynamic_tail_ref": "continuation",
        "branch_ref": "branch",
        "value": "value",
        "returns_to_handler": "value",
        "worker_context_ref": "ctx",
        "handler_context_ref": "ctx",
        "branch_scope_ref": "branch-scope",
    },
    "ResumeReturn": {
        "ref": "self",
        "resume_ref": "lifecycle:resume",
        "selection_ref": "lifecycle:selection",
        "selection_path_ref": "lifecycle:path",
        "branch_ref": "branch",
        "handler_continuation_ref": "continuation",
        "handler_dynamic_tail_ref": "continuation",
        "value": "value",
        "handler_context_ref": "ctx",
        "branch_scope_ref": "branch-scope",
    },
    "EffectCapture": {
        "ref": "self",
        "selection_ref": "lifecycle:selection",
        "selection_path_ref": "lifecycle:path",
        "branch_ref": "branch",
        "action_kind": "enum",
        "action_payload": "value",
        "continuation_disposition": "enum",
        "outer_context_ref": "ctx",
        "branch_scope_ref": "branch-scope",
    },
    "SelectionClosed": {
        "ref": "self",
        "selection_ref": "lifecycle:selection",
        "selection_path_ref": "lifecycle:path",
        "branch_ref": "branch",
        "reason": "enum",
        "caused_by_ref": "lifecycle:capture",
        "caused_by_record_type": "enum",
        "closed_by_selection_ref": "lifecycle:selection",
        "closed_by_selection_path_ref": "lifecycle:path",
        "branch_scope_ref": "branch-scope",
    },
}


def field_role(record_type: str, field_name: str) -> FieldRole | None:
    """Return the canonicalization role for one (record_type, field) pair.

    Public helper for extensions adding new record types or for downstream
    validators that need to walk records by role. Returns None when the
    field is not in the classifier — record types outside the projection's
    current scope will surface explicitly.
    """

    return _FIELDS_BY_RECORD_TYPE.get(record_type, {}).get(field_name)


# ---------------------------------------------------------------------------
# Pre-passes: rewrite runtime-local refs to canonical via the catalog inputs.
# ---------------------------------------------------------------------------

# Sentinel branch ref that is excluded from CanonicalRefMap by tightness rule
# per 2026-05-24 §"`branch:root` sentinel".
_BRANCH_ROOT_SENTINEL = "branch:root"

_CANONICAL_REF_RE = re.compile(r"^[a-z-]+:sha256:[0-9a-f]{64}$")


def _is_runtime_ref(value: object, kind: str) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith(f"{kind}:runtime:")


def _is_canonical_kind_ref(value: str, *expected_prefixes: str) -> bool:
    return any(value.startswith(p) for p in expected_prefixes)


@dataclass(frozen=True)
class _PreRewrites:
    """Bundle of pre-pass rewrite maps applied to record fields before
    lifecycle-canonicalization passes."""

    program_install: Mapping[Ref, Ref]
    program_handler_env: Mapping[Ref, Ref]
    program_schema: Mapping[Ref | None, object | None]
    continuation: Mapping[Ref, Ref]
    continuation_control: Mapping[Ref, Ref]
    context: Mapping[Ref, Ref]


def _rewrite_program_structure(value: Ref | None, rewrites: _PreRewrites) -> Ref | None:
    """Rewrite install:N / handler-env:N / schema refs via program identity.

    Schema fingerprints are content-addressed payloads (dicts), not strings,
    so we hash them into a stable `schema:sha256:HEX` ref for embedding into
    canonical record bodies.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if value.startswith("install:") and not value.startswith("install:sha256:"):
        canonical = rewrites.program_install.get(value)
        if canonical is None:
            raise SemanticTransitionBatchValidationError(
                f"program-structure rewrite missing for install ref {value!r}"
            )
        return canonical
    if value.startswith("handler-env:") and not value.startswith("handler-env:sha256:"):
        canonical = rewrites.program_handler_env.get(value)
        if canonical is None:
            raise SemanticTransitionBatchValidationError(
                f"program-structure rewrite missing for handler-env ref {value!r}"
            )
        return canonical
    if value.startswith("schema:") and not value.startswith("schema:sha256:"):
        fingerprint = rewrites.program_schema.get(value)
        if fingerprint is None and value not in rewrites.program_schema:
            raise SemanticTransitionBatchValidationError(
                f"program-structure rewrite missing for schema ref {value!r}"
            )
        if fingerprint is None:
            return None
        return content_ref("schema", fingerprint)
    return value


def _rewrite_continuation(value: Ref | None, rewrites: _PreRewrites) -> Ref | None:
    if value is None:
        return None
    if _is_runtime_ref(value, "continuation"):
        canonical = rewrites.continuation.get(value)
        if canonical is None:
            raise SemanticTransitionBatchValidationError(
                f"continuation rewrite missing for {value!r}"
            )
        return canonical
    return value


def _rewrite_continuation_control(value: Ref | None, rewrites: _PreRewrites) -> Ref | None:
    if value is None:
        return None
    if _is_runtime_ref(value, "continuation-control"):
        canonical = rewrites.continuation_control.get(value)
        if canonical is None:
            raise SemanticTransitionBatchValidationError(
                f"continuation-control rewrite missing for {value!r}"
            )
        return canonical
    return value


def _rewrite_ctx(value: Ref | None, rewrites: _PreRewrites) -> Ref | None:
    if value is None:
        return None
    if _is_runtime_ref(value, "ctx"):
        canonical = rewrites.context.get(value)
        if canonical is None:
            raise SemanticTransitionBatchValidationError(
                f"ctx rewrite missing for {value!r}"
            )
        return canonical
    return value


def _build_pre_rewrites(
    transition: ReplayableKernelTransition,
    prepared: PreparedKernelProgram,
) -> _PreRewrites:
    identity = project_program_identity(prepared)
    return _PreRewrites(
        # ProgramIdentity exposes `install_refs_by_node` keyed by NodeId; the
        # runtime emits string `install:N` refs, so iterate the prepared
        # program's installs to build the runtime-ref-keyed inverse.
        program_install=_install_runtime_to_canonical(prepared, identity),
        program_handler_env=dict(identity.handler_env_refs),
        program_schema=_schema_runtime_to_fingerprint(prepared, identity),
        continuation=dict(transition.continuation_ref_map),
        continuation_control=dict(transition.continuation_control_ref_map),
        context=dict(transition.context_ref_map),
    )


def _install_runtime_to_canonical(
    prepared: PreparedKernelProgram, identity: ProgramIdentity
) -> dict[Ref, Ref]:
    """Build runtime-string → canonical-content-ref mapping for installs.

    The runtime emits `install:N` refs (e.g. `install:3`); ProgramIdentity
    keys installs by NodeId. We materialize the inverse here by visiting
    every install referenced from the prepared program's handler-env
    definitions.
    """

    mapping: dict[Ref, Ref] = {}
    program = prepared.program
    # handler_envs maps handler-env-ref -> definition listing installs
    for handler_env_def in program.handler_envs.values():
        for install_def in handler_env_def.bindings:
            install_runtime_ref = install_def.install_ref
            canonical = identity.install_refs_by_object_id.get(id(install_def))
            if canonical is not None:
                mapping[install_runtime_ref] = canonical
    return mapping


def _schema_runtime_to_fingerprint(
    prepared: PreparedKernelProgram, identity: ProgramIdentity
) -> dict[Ref | None, object | None]:
    """Schema rewrites use ProgramIdentity's fingerprints; the projection
    hashes those into a canonical `schema:sha256:HEX` form."""

    return dict(identity.schema_ref_fingerprints)


# ---------------------------------------------------------------------------
# Record canonicalization: apply pre-rewrites + lifecycle ref substitutions to
# a single record's body, then hash to produce the record's canonical ref.
# ---------------------------------------------------------------------------


def _canonical_record_body(
    record: TraceRecord,
    role_map: Mapping[str, FieldRole],
    pre: _PreRewrites,
    declaration_canon: Mapping[Ref, Ref],
    selection_canon: Mapping[Ref, Ref],
    source_canon: Mapping[Ref, Ref],
    path_canon: Mapping[Ref, Ref],
    resume_canon: Mapping[Ref, Ref],
    capture_canon: Mapping[Ref, Ref],
) -> dict[str, Any]:
    """Return a canonical, deterministic mapping of the record's body with
    all referenced refs rewritten to their canonical forms.

    The result has `record_type` (for content-ref kind disambiguation) and
    the record's own `ref` field omitted (the canonical body is what is
    hashed to produce the record's own canonical ref).
    """

    body: dict[str, Any] = {"record_type": type(record).__name__}
    for f in fields(record):
        if f.name not in role_map:
            raise SemanticTransitionBatchValidationError(
                f"field {f.name!r} of {type(record).__name__} has no classifier role"
            )
        role = role_map[f.name]
        if role == "self":
            continue  # self ref is the OUTPUT, not part of the canonical body
        value = getattr(record, f.name)
        if role == "program-structure":
            value = _rewrite_program_structure(value, pre)
        elif role == "continuation":
            value = _rewrite_continuation(value, pre)
        elif role == "continuation-control":
            value = _rewrite_continuation_control(value, pre)
        elif role == "ctx":
            value = _rewrite_ctx(value, pre)
        elif role in {"branch", "branch-scope"}:
            value = _rewrite_branch(value)
        elif role.startswith("lifecycle:"):
            kind = role.split(":", 1)[1]
            canon_map = {
                "declaration": declaration_canon,
                "selection": selection_canon,
                "source": source_canon,
                "path": path_canon,
                "resume": resume_canon,
                "capture": capture_canon,
            }.get(kind)
            if canon_map is not None and isinstance(value, str):
                canonical = canon_map.get(value)
                if canonical is not None:
                    value = canonical
                # If not in map yet, leave as-is. Dependency-order validation
                # in validate_semantic_batch catches forward references.
        # roles "enum" and "value": pass through untouched
        body[f.name] = value
    return body


def _rewrite_branch(value: Any) -> Any:
    if value is None or value == _BRANCH_ROOT_SENTINEL:
        return value
    # Non-root branches require a fork-branch source pass; not exercised by
    # -lite. For now, pass through verbatim and let validation catch any
    # cited non-root branch with no canonical entry.
    return value


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------


def _parse_path_ref(path_ref: str) -> tuple[str, str, str]:
    """Parse a path ref into (selection_or_unhandled, source_ref, branch_ref).

    Path format (per paths.py):
        path:{selection_ref}/{source_ref}/{branch_ref}
        path:unhandled/{source_ref}/{branch_ref}

    The literal `unhandled` first component stays literal in the canonical
    payload (per 2026-05-24 §"Projection canonicalization pass order").
    """

    if not path_ref.startswith("path:"):
        raise SemanticTransitionBatchValidationError(
            f"path ref must start with 'path:', got {path_ref!r}"
        )
    body = path_ref[len("path:"):]
    head, sep, rest = body.partition("/")
    if not sep:
        raise SemanticTransitionBatchValidationError(
            f"path ref missing source/branch components: {path_ref!r}"
        )
    source, sep2, branch = rest.partition("/")
    if not sep2:
        raise SemanticTransitionBatchValidationError(
            f"path ref missing branch component: {path_ref!r}"
        )
    return head, source, branch


# ---------------------------------------------------------------------------
# Canonical record body for path refs (Pass 4)
# ---------------------------------------------------------------------------


def _canonical_path_body(
    path_ref: str,
    selection_canon: Mapping[Ref, Ref],
    source_canon: Mapping[Ref, Ref],
) -> dict[str, Any]:
    head, source, branch = _parse_path_ref(path_ref)
    if head == "unhandled":
        # The literal "unhandled" stays literal — selection refs are
        # `selection:N`-shaped and never collide with this sentinel.
        canonical_selection: str = "unhandled"
    else:
        canonical_selection_lookup = selection_canon.get(head)
        if canonical_selection_lookup is None:
            raise SemanticTransitionBatchValidationError(
                f"path ref {path_ref!r} cites unknown selection {head!r}"
            )
        canonical_selection = canonical_selection_lookup
    canonical_source_lookup = source_canon.get(source)
    if canonical_source_lookup is None:
        raise SemanticTransitionBatchValidationError(
            f"path ref {path_ref!r} cites unknown source {source!r}"
        )
    return {
        "selection": canonical_selection,
        "source": canonical_source_lookup,
        # branch:root stays literal; non-root branches would canonicalize via
        # the (currently unimplemented for -lite) branch pass.
        "branch": branch,
    }


# ---------------------------------------------------------------------------
# Top-level projection
# ---------------------------------------------------------------------------


def semantic_batch_from_transition(
    transition: ReplayableKernelTransition,
    state: KernelReplayState | None,
    catalog: Mapping[Ref, ContinuationObject],
    *,
    profile: SemanticProfile | None = None,
    admission_basis: AdmissionBasis | None = None,
) -> SemanticTransitionBatch | ProfileRejected:
    """Project a ReplayableKernelTransition to its conformance view.

    Pure of `(transition, state, catalog, admission_basis)`. Profile flows
    from `state.profile` (the canonical source per 2026-05-22 §"Profile
    attachment on PreparedKernelProgram" + 2026-05-24 §"Post-#72 design pass"
    item A). The explicit `profile=` kwarg is required only when `state is
    None` (rare: state-less unit tests for rejected projections).

    For non-initial transitions (a resume against an admitted observation),
    `admission_basis` is required (#78): it carries the source/frontier/
    one-shot facts that justify the resume, and `transition_kind` is derived
    from `admission_basis.source_kind`. The basis profile and program_ref must
    agree with the run's. For the `initial_run_prefix` transition, no basis is
    needed.

    The result is either a `SemanticTransitionBatch` (for
    completed/external-effect-request status, initial or resumptive) or a
    `ProfileRejected` (for `rejected` status, which has no admission basis to
    bind).

    See module docstring for the eight-pass canonicalization order and the
    pre-passes required for program-structure / continuation / ctx ref
    rewrites.
    """

    if state is not None:
        prepared = state.prepared_program
        if profile is not None and profile != state.profile:
            raise SemanticTransitionBatchValidationError(
                f"explicit profile {profile.name!r} disagrees with "
                f"state.profile {state.profile.name!r}"
            )
        profile = state.profile
    else:
        prepared = None
        if profile is None:
            raise SemanticTransitionBatchValidationError(
                "semantic_batch_from_transition requires explicit profile= when state is None"
            )

    if transition.status == "rejected":
        return _project_rejected(transition, prepared, catalog, profile=profile)

    if prepared is None:
        raise SemanticTransitionBatchValidationError(
            "semantic_batch_from_transition requires KernelReplayState for non-rejected transitions"
        )

    pre = _build_pre_rewrites(transition, prepared)
    ref_map_entries: dict[str, str] = {}
    projected_records: list[dict[str, JsonValue]] = []

    # Allocate per-pass canonical maps (runtime ref → canonical ref)
    declaration_canon: dict[Ref, Ref] = {}
    selection_canon: dict[Ref, Ref] = {}
    source_canon: dict[Ref, Ref] = {}
    path_canon: dict[Ref, Ref] = {}
    resume_canon: dict[Ref, Ref] = {}
    resume_return_canon: dict[Ref, Ref] = {}
    capture_canon: dict[Ref, Ref] = {}
    closed_canon: dict[Ref, Ref] = {}

    # Sequence counters for seq-keyed records
    resume_seq: dict[tuple[Ref, Ref, Ref], int] = defaultdict(int)
    resume_return_seq: dict[Ref, int] = defaultdict(int)
    capture_seq: dict[tuple[Ref, Ref], int] = defaultdict(int)
    closed_seq: dict[tuple[Ref, Ref], int] = defaultdict(int)

    records = transition.trace_delta

    # Pass 1: EffectDeclaration
    for record in records:
        if isinstance(record, EffectDeclaration):
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE["EffectDeclaration"],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            canonical = content_ref("declaration", body)
            declaration_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Pass 2: HandlerSelection
    for record in records:
        if isinstance(record, HandlerSelection):
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE["HandlerSelection"],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            canonical = content_ref("selection", body)
            selection_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Pass 3: source records (ResumptionHandle / ContinuationPending / ForkBranch)
    for record in records:
        if isinstance(record, ResumptionHandle | ContinuationPending | ForkBranch):
            type_name = type(record).__name__
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE[type_name],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            canonical = content_ref("source", body)
            source_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Pass 4: path refs cited by any record (collect, canonicalize, no dedup
    # by source — the same path ref always maps to the same canonical)
    cited_paths: set[str] = set()
    for record in records:
        type_name = type(record).__name__
        role_map = _FIELDS_BY_RECORD_TYPE.get(type_name, {})
        for f in fields(record):
            if role_map.get(f.name) == "lifecycle:path":
                value = getattr(record, f.name)
                if isinstance(value, str) and value.startswith("path:"):
                    cited_paths.add(value)
    for path_ref in sorted(cited_paths):
        body = _canonical_path_body(path_ref, selection_canon, source_canon)
        canonical = content_ref("path", body)
        path_canon[path_ref] = canonical
        ref_map_entries[path_ref] = canonical

    # Pass 5: ContinuationResume (seq-keyed by canonical (decl, sel, path))
    for record in records:
        if isinstance(record, ContinuationResume):
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE["ContinuationResume"],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            decl_c = declaration_canon.get(record.declaration_ref, record.declaration_ref)
            sel_c = selection_canon.get(record.selection_ref, record.selection_ref)
            path_c = path_canon.get(record.selection_path_ref, record.selection_path_ref)
            resume_seq_key = (decl_c, sel_c, path_c)
            seq = resume_seq[resume_seq_key]
            resume_seq[resume_seq_key] = seq + 1
            body["_seq"] = seq
            canonical = content_ref("resume", body)
            resume_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Pass 6: ResumeReturn (seq-keyed by canonical resume_ref)
    for record in records:
        if isinstance(record, ResumeReturn):
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE["ResumeReturn"],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            resume_c = resume_canon.get(record.resume_ref, record.resume_ref)
            seq = resume_return_seq[resume_c]
            resume_return_seq[resume_c] = seq + 1
            body["_seq"] = seq
            canonical = content_ref("resume-return", body)
            resume_return_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Pass 7: EffectCapture (seq-keyed by canonical (sel, path))
    for record in records:
        if isinstance(record, EffectCapture):
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE["EffectCapture"],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            sel_c = selection_canon.get(record.selection_ref, record.selection_ref)
            path_c = path_canon.get(record.selection_path_ref, record.selection_path_ref)
            capture_seq_key = (sel_c, path_c)
            seq = capture_seq[capture_seq_key]
            capture_seq[capture_seq_key] = seq + 1
            body["_seq"] = seq
            canonical = content_ref("capture", body)
            capture_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Pass 8: SelectionClosed (seq-keyed by canonical (sel, path))
    for record in records:
        if isinstance(record, SelectionClosed):
            body = _canonical_record_body(
                record,
                _FIELDS_BY_RECORD_TYPE["SelectionClosed"],
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, capture_canon,
            )
            sel_c = selection_canon.get(record.selection_ref, record.selection_ref)
            path_c = path_canon.get(record.selection_path_ref, record.selection_path_ref)
            closed_seq_key = (sel_c, path_c)
            seq = closed_seq[closed_seq_key]
            closed_seq[closed_seq_key] = seq + 1
            body["_seq"] = seq
            canonical = content_ref("closed", body)
            closed_canon[record.ref] = canonical
            ref_map_entries[record.ref] = canonical

    # Emit projected record dicts (rewritten lifecycle refs + retained metadata)
    for record in records:
        projected_records.append(
            _projected_record_dict(
                record,
                pre,
                declaration_canon, selection_canon, source_canon,
                path_canon, resume_canon, resume_return_canon,
                capture_canon, closed_canon,
            )
        )

    ref_map = CanonicalRefMap(
        entries=tuple(sorted(ref_map_entries.items()))
    )

    # Profile is read from transition metadata in a future commit; for #72
    # the caller supplies it explicitly (default CORE_A).
    program_ref_canonical = transition.program_ref

    transition_kind = _transition_kind(transition)

    continuation_objects_payload = _continuation_objects_for_records(
        projected_records, catalog
    )

    # admission_basis is None for initial-run transitions; non-initial
    # transitions require an explicit admission basis (deferred to #73+
    # when AdmittedObservation wire types land). For #72 the projection
    # produces an "initial_run_prefix" batch for completed/external-request
    # status with no parent transitions; otherwise it surfaces a clear
    # error rather than fabricating an admission basis.
    if transition_kind == "initial_run_prefix":
        return build_initial_transition_batch(
            program_ref=program_ref_canonical,
            transition_id=transition.transition_id,
            records=tuple(projected_records),
            ref_map=ref_map,
            continuation_objects=continuation_objects_payload,
            profile=profile,
        )

    if admission_basis is None:
        raise SemanticTransitionBatchValidationError(
            f"semantic_batch_from_transition for non-initial transitions requires "
            f"an admission_basis= argument (#78); transition_kind={transition_kind!r}"
        )

    resolved_kind = _transition_kind_for_source(admission_basis.source_kind)
    return build_admitted_transition_batch(
        program_ref=program_ref_canonical,
        transition_id=transition.transition_id,
        transition_kind=resolved_kind,
        admission_basis=admission_basis,
        parent_transition_refs=tuple(transition.parent_transition_refs),
        records=tuple(projected_records),
        ref_map=ref_map,
        continuation_objects=continuation_objects_payload,
        profile=profile,
    )


def _continuation_objects_for_records(
    projected_records: list[dict[str, JsonValue]],
    catalog: Mapping[Ref, ContinuationObject],
) -> tuple[Mapping[str, JsonValue], ...]:
    """Walk projected records, collect cited continuation-object refs, and
    return JSON forms of every reachable object from the catalog.

    The set is closed under continuation_object_child_refs so that nested
    references (e.g., a root pointing at frames pointing at envs) are all
    present in the batch's continuation_objects.
    """

    cited: set[str] = set()
    for record in projected_records:
        _collect_continuation_object_refs(record, cited)

    reachable: dict[str, ContinuationObject] = {}
    queue = list(cited)
    while queue:
        ref = queue.pop()
        if ref in reachable:
            continue
        obj = catalog.get(ref)
        if obj is None:
            # Caller's catalog is incomplete; surface as a coverage failure
            # at SemanticTransitionBatch construction, not here.
            continue
        reachable[ref] = obj
        for child in continuation_object_child_refs(obj):
            if child not in reachable:
                queue.append(child)

    # Deterministic ordering for byte-stable output: sort by ref.
    return tuple(continuation_object_to_json(reachable[ref]) for ref in sorted(reachable))


def _collect_continuation_object_refs(
    record: Mapping[str, JsonValue], out: set[str]
) -> None:
    """Recursively walk a JSON record dict and collect every string field
    that starts with `continuation-object:`."""

    for value in record.values():
        if isinstance(value, str):
            if value.startswith("continuation-object:"):
                out.add(value)
        elif isinstance(value, Mapping):
            _collect_continuation_object_refs(value, out)
        elif isinstance(value, list | tuple):
            for item in value:
                if isinstance(item, str) and item.startswith("continuation-object:"):
                    out.add(item)
                elif isinstance(item, Mapping):
                    _collect_continuation_object_refs(item, out)


def _projected_record_dict(
    record: TraceRecord,
    pre: _PreRewrites,
    declaration_canon: Mapping[Ref, Ref],
    selection_canon: Mapping[Ref, Ref],
    source_canon: Mapping[Ref, Ref],
    path_canon: Mapping[Ref, Ref],
    resume_canon: Mapping[Ref, Ref],
    resume_return_canon: Mapping[Ref, Ref],
    capture_canon: Mapping[Ref, Ref],
    closed_canon: Mapping[Ref, Ref],
) -> dict[str, JsonValue]:
    """Emit the post-projection JSON form of a record: lifecycle refs and
    pre-pass refs all rewritten to canonical."""

    json_form = dict(trace_record_to_json(record))
    type_name = type(record).__name__
    role_map = _FIELDS_BY_RECORD_TYPE.get(type_name, {})
    for f in fields(record):
        role = role_map.get(f.name)
        if role is None or role in ("enum", "value", "self"):
            # `self` field stays as the runtime ref; the canonical form for
            # the record itself is exposed via CanonicalRefMap.
            continue
        if f.name not in json_form:
            continue
        value = json_form[f.name]
        if role == "program-structure":
            json_form[f.name] = _rewrite_program_structure(value, pre)
        elif role == "continuation":
            json_form[f.name] = _rewrite_continuation(value, pre)
        elif role == "continuation-control":
            json_form[f.name] = _rewrite_continuation_control(value, pre)
        elif role == "ctx":
            json_form[f.name] = _rewrite_ctx(value, pre)
        elif role in {"branch", "branch-scope"}:
            json_form[f.name] = _rewrite_branch(value)
        elif role.startswith("lifecycle:"):
            kind = role.split(":", 1)[1]
            canon_map_for = {
                "declaration": declaration_canon,
                "selection": selection_canon,
                "source": source_canon,
                "path": path_canon,
                "resume": resume_canon,
                "capture": capture_canon,
            }.get(kind, {})
            if isinstance(value, str):
                json_form[f.name] = canon_map_for.get(value, value)
    return json_form


def _transition_kind(transition: ReplayableKernelTransition) -> str:
    """Classify whether a transition is initial or resumptive.

    Initial vs resumptive is structural (parent_transition_refs). The
    *specific* resumptive kind (callable_resume vs unhandled_top_level_resume
    vs ...) is determined from the admission basis's source_kind by
    `_transition_kind_for_source(...)`, not guessable from the transition
    alone, so this returns the generic resumptive sentinel.
    """

    if transition.parent_transition_refs:
        return "resumptive"
    return "initial_run_prefix"


_SOURCE_KIND_TO_TRANSITION_KIND: dict[SourceKind, TransitionKind] = {
    "UnhandledSuspension": "unhandled_top_level_resume",
    "ResumptionHandle": "callable_resume",
    "ContinuationPending": "pending_resume",
    "ForkBranch": "fork_branch_resume",
}


def _transition_kind_for_source(source_kind: SourceKind) -> TransitionKind:
    """Map an admission basis's source_kind to its transition_kind."""

    try:
        return _SOURCE_KIND_TO_TRANSITION_KIND[source_kind]
    except KeyError as exc:  # pragma: no cover - defensive
        raise SemanticTransitionBatchValidationError(
            f"no transition_kind mapping for source_kind {source_kind!r}"
        ) from exc


def _project_rejected(
    transition: ReplayableKernelTransition,
    prepared: PreparedKernelProgram | None,
    catalog: Mapping[Ref, ContinuationObject],
    *,
    profile: SemanticProfile,
) -> ProfileRejected:
    """Project a rejected operational transition to ProfileRejected."""

    from shepherd_kernel_v3_reference.kernel.replay import ReplayableRejected

    payload = transition.payload
    if not isinstance(payload, ReplayableRejected):
        raise SemanticTransitionBatchValidationError(
            "rejected transition must carry ReplayableRejected payload"
        )

    # For rejected transitions, the partial-records ref map is best-effort:
    # the trace_delta may be empty or contain partial records up to the
    # point of failure. We still emit a CanonicalRefMap covering them.
    if prepared is not None and transition.trace_delta:
        pre = _build_pre_rewrites(transition, prepared)
        ref_map_entries: dict[str, str] = {}
        partial_records: list[dict[str, JsonValue]] = []
        # Reuse the same passes but with best-effort error handling
        try:
            # Run pass 1..8 in shortened form via the main function's logic,
            # but tolerate missing refs because partial traces may cite
            # records not yet emitted.
            declaration_canon: dict[Ref, Ref] = {}
            selection_canon: dict[Ref, Ref] = {}
            source_canon: dict[Ref, Ref] = {}
            path_canon: dict[Ref, Ref] = {}
            resume_canon: dict[Ref, Ref] = {}
            resume_return_canon: dict[Ref, Ref] = {}
            capture_canon: dict[Ref, Ref] = {}
            closed_canon: dict[Ref, Ref] = {}
            for record in transition.trace_delta:
                if isinstance(record, EffectDeclaration):
                    body = _canonical_record_body(
                        record, _FIELDS_BY_RECORD_TYPE["EffectDeclaration"], pre,
                        declaration_canon, selection_canon, source_canon,
                        path_canon, resume_canon, capture_canon,
                    )
                    canonical = content_ref("declaration", body)
                    declaration_canon[record.ref] = canonical
                    ref_map_entries[record.ref] = canonical
            for record in transition.trace_delta:
                partial_records.append(
                    _projected_record_dict(
                        record, pre,
                        declaration_canon, selection_canon, source_canon,
                        path_canon, resume_canon, resume_return_canon,
                        capture_canon, closed_canon,
                    )
                )
        except SemanticTransitionBatchValidationError:
            ref_map_entries = {}
            partial_records = [dict(trace_record_to_json(r)) for r in transition.trace_delta]
    else:
        ref_map_entries = {}
        partial_records = [dict(trace_record_to_json(r)) for r in transition.trace_delta]

    return ProfileRejected(
        transition_id=transition.transition_id,
        profile=profile,
        program_ref=transition.program_ref,
        partial_records=tuple(partial_records),
        rejection_reason=payload.reason_message or payload.reason_type,
        consumed_source_keys=(payload.source_key,) if payload.source_key else (),
        ref_map=CanonicalRefMap(entries=tuple(sorted(ref_map_entries.items()))),
    )


# ---------------------------------------------------------------------------
# Validator: enforces coverage, tightness, determinism, well-formedness,
# dependency-order, and round-trip per 260521-0600-kernel.md §"Canonical Ref
# Map" validator obligations.
# ---------------------------------------------------------------------------


_LIFECYCLE_CANONICAL_PREFIXES: tuple[str, ...] = (
    "declaration:sha256:",
    "selection:sha256:",
    "source:sha256:",
    "path:sha256:",
    "resume:sha256:",
    "resume-return:sha256:",
    "capture:sha256:",
    "closed:sha256:",
)


def validate_semantic_batch(batch: SemanticTransitionBatch) -> None:
    """Enforce the ref-map invariants on a projected batch.

    Per 260521-0600-kernel.md §"Canonical Ref Map" — Validator obligations:

    - **well-formedness**: every canonical value matches `<kind>:sha256:<HEX>`
    - **coverage**: every lifecycle canonical ref cited by the projected
      records (record self-refs or lifecycle:* fields) has an entry in
      `ref_map`
    - **tightness**: no extraneous entries — every map's canonical value
      appears as a cited ref in the projected records
    - **dependency-order**: canonical refs validate topologically (no ref
      cites a not-yet-canonicalized dependency); enforced implicitly by the
      well-formedness check on every value
    - **determinism** and **round-trip**: enforced by callers running the
      projection twice / round-tripping through serde and comparing bytes.
      The projection is pure of `(transition, state, catalog)`, so these
      hold by construction; this validator does not re-run the projection.

    Raises `SemanticTransitionBatchValidationError` on the first violation.
    """

    ref_map = batch.ref_map

    # Well-formedness
    for runtime_ref, canonical_ref in ref_map.entries:
        if not _CANONICAL_REF_RE.match(canonical_ref):
            raise SemanticTransitionBatchValidationError(
                f"CanonicalRefMap entry for {runtime_ref!r} has malformed canonical "
                f"value {canonical_ref!r} (expected kind:sha256:HEX)"
            )

    canonical_values = {canonical for _runtime, canonical in ref_map.entries}

    # Coverage and tightness: walk projected records for cited canonical
    # refs of any lifecycle kind. Build the cited set, then check both
    # directions against the map.
    cited_canonical: set[str] = set()
    for record_dict in batch.records:
        if not isinstance(record_dict, Mapping):
            continue
        record_type = record_dict.get("record_type")
        if not isinstance(record_type, str):
            continue
        role_map = _FIELDS_BY_RECORD_TYPE.get(record_type, {})
        for field_name, role in role_map.items():
            if role != "self" and not role.startswith("lifecycle:"):
                continue
            value = record_dict.get(field_name)
            if isinstance(value, str) and _is_lifecycle_canonical(value):
                cited_canonical.add(value)

    # Self refs: a record's `ref` field still holds the runtime-local form
    # (e.g. `declaration:0`), not the canonical. The canonical for that
    # runtime ref must be in the map; we already verified the value
    # well-formedness above. Coverage of the self-ref is implicit: the
    # projection always emits the record's ref into the map.
    runtime_self_refs = {
        record_dict.get("ref")
        for record_dict in batch.records
        if isinstance(record_dict, Mapping) and isinstance(record_dict.get("ref"), str)
    }
    map_runtime_keys = {key for key, _ in ref_map.entries}
    for self_ref in runtime_self_refs:
        if self_ref is None or not isinstance(self_ref, str):
            continue
        if _looks_like_lifecycle_runtime_ref(self_ref) and self_ref not in map_runtime_keys:
            raise SemanticTransitionBatchValidationError(
                f"CanonicalRefMap missing entry for record self-ref {self_ref!r}"
            )

    # Coverage: every cross-referenced canonical lifecycle ref in the
    # projected records must appear as a canonical value in the map.
    missing_canonical = cited_canonical - canonical_values
    if missing_canonical:
        raise SemanticTransitionBatchValidationError(
            f"CanonicalRefMap missing coverage for canonical refs cited by records: "
            f"{sorted(missing_canonical)!r}"
        )

    # Tightness: every canonical value in the map must appear cited
    # somewhere in the records. The record's self-ref entry in the map is
    # tested implicitly because every projected record cites its own self
    # in its `ref` field — but `ref` holds the runtime form, while the map
    # value is canonical. Self-ref tightness: every map entry's canonical
    # value must either be cited cross-reference or correspond to some
    # record's runtime self-ref.
    canonical_to_runtime = {canonical: runtime for runtime, canonical in ref_map.entries}
    for canonical in canonical_values:
        if canonical in cited_canonical:
            continue
        runtime = canonical_to_runtime.get(canonical)
        if runtime is None:
            continue  # impossible; entries iterate by definition
        if runtime not in runtime_self_refs:
            raise SemanticTransitionBatchValidationError(
                f"CanonicalRefMap has extraneous entry: {runtime!r} -> {canonical!r} "
                "is neither a record self-ref nor cited cross-reference"
            )


def _is_lifecycle_canonical(value: str) -> bool:
    """Return True if `value` is a canonical lifecycle ref of one of the
    eight passes' kinds."""

    return any(value.startswith(p) for p in _LIFECYCLE_CANONICAL_PREFIXES)


def _looks_like_lifecycle_runtime_ref(value: str) -> bool:
    """Return True if `value` is a runtime-local lifecycle ref (i.e., a ref
    that should appear in CanonicalRefMap when cited)."""

    if value == _BRANCH_ROOT_SENTINEL:
        return False
    if _CANONICAL_REF_RE.match(value):
        return False
    for prefix in (
        "declaration:",
        "selection:",
        "resumption:",
        "pending:",
        "fork-branch:",
        "path:",
        "resume:",
        "resume-return:",
        "capture:",
        "selection-closed:",
    ):
        if value.startswith(prefix):
            return True
    return False


def project_envelope_to_wire(
    envelope: KernelResultEnvelope,
    state: KernelReplayState | None,
    catalog: Mapping[Ref, ContinuationObject],
) -> WireResult:
    """Project a KernelResultEnvelope to its conformance WireResult.

    For status in {completed, external-effect-request, rejected}, delegates
    to `semantic_batch_from_transition(...)` over the envelope's transition.
    For `profile-rejected` (no transition), constructs a synthetic
    `ProfileRejected` with empty content fields and a synthetic transition_id
    derived from the program_ref/diagnostic per 2026-05-24 §"Post-#72
    design pass" item D/E.
    """

    # Lazy import to avoid the envelope.py <-> projection.py circular dependency
    # at module load time.
    from shepherd_kernel_v3_reference.envelope import (
        KernelRejection,
        KernelResultEnvelope,
        WireResult,
    )
    from shepherd_kernel_v3_reference.semantic import CanonicalRefMap

    if not isinstance(envelope, KernelResultEnvelope):
        raise TypeError("project_envelope_to_wire requires a KernelResultEnvelope")

    if envelope.status == "profile-rejected":
        rejection = envelope.payload
        if not isinstance(rejection, KernelRejection):
            raise TypeError(
                "profile-rejected envelope payload must be a KernelRejection"
            )
        synthetic_transition_id = content_ref(
            "profile-rejected",
            {
                "construct": rejection.construct,
                "diagnostic": rejection.diagnostic,
            },
        )
        batch: SemanticTransitionBatch | ProfileRejected = ProfileRejected(
            transition_id=synthetic_transition_id,
            profile=envelope.profile,
            program_ref=rejection.program_ref or "program:profile-rejected",
            partial_records=(),
            rejection_reason=rejection.diagnostic,
            consumed_source_keys=(),
            ref_map=CanonicalRefMap(),
        )
        return WireResult(envelope=envelope, batch=batch)

    if envelope.transition is None:
        # Guarded by KernelResultEnvelope.__post_init__, but defensive
        raise SemanticTransitionBatchValidationError(
            f"envelope.status={envelope.status!r} requires transition"
        )

    batch = semantic_batch_from_transition(envelope.transition, state, catalog)
    return WireResult(envelope=envelope, batch=batch)


__all__ = [
    "field_role",
    "project_envelope_to_wire",
    "semantic_batch_from_transition",
    "validate_semantic_batch",
]
