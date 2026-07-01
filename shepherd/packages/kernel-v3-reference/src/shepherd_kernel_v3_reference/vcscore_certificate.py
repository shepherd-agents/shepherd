"""VcsCore proof certificates for the shipped workspace-control run shape.

This module deliberately validates a narrow certificate over public run-ledger
JSON, not the operating system, Git implementation, or carrier backend. It is
the bridge from durable VcsCore records to an auditable proof envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.proof_envelope import (
    EXTENSION_PROOF_SURFACE_THEOREM_IDS,
    PROOF_ENVELOPE_SCHEMA_VERSION,
    PROOF_ENVELOPE_VALIDATOR,
    PROOF_EVIDENCE_REF_KIND,
    ProofEnvelope,
    ProofProfile,
    ProofStrength,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import Ref

JsonValue: TypeAlias = Any
JsonObject: TypeAlias = dict[str, JsonValue]

VCSCORE_RUN_CERTIFICATE_SCHEMA_VERSION = "shepherd_kernel_v3_reference.vcscore-run-certificate.v1"
VCSCORE_RUN_CERTIFICATE_VALIDATOR = "shepherd-kernel-v3-reference.vcscore-run-certificate.v1"
VCSCORE_RUN_EXTENSION_NAME = "vcscore_run_record_sound.v1"
VCSCORE_RUN_THEOREM_IDS = ("vcscore_run_record_sound",)
_TERMINAL_STATUSES = frozenset({"merged", "retained", "failed", "discarded", "cancelled"})
_TERMINAL_FAILURE_BODY_STATUSES = frozenset({"failed", "stopped", "exhausted", "refused"})


class VcsCoreCertificateError(ValueError):
    """Raised when a VcsCore proof certificate overclaims its ledger evidence."""


@dataclass(frozen=True)
class VcsCoreRunCertificate:
    """Canonical certificate for a terminal VcsCore run-ledger row."""

    run_ref: str
    task_id: str
    task_version: str
    task_schema_digest: str
    args_digest: str
    provider: str
    may_profile: str
    status: str
    body_status: str
    world_disposition: str
    output_publication_status: str
    input_workspace_world_oid: str
    terminal_workspace_world_oid: str | None
    trace_ref: Mapping[str, JsonValue]
    trace_head: str
    runtime_operation: str | None = None
    run_start_revision: str | None = None
    output_names: tuple[str, ...] = ()
    schema_version: str = VCSCORE_RUN_CERTIFICATE_SCHEMA_VERSION
    validator: str = VCSCORE_RUN_CERTIFICATE_VALIDATOR
    theorem_ids: tuple[str, ...] = field(default=VCSCORE_RUN_THEOREM_IDS)

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_ref", dict(self.trace_ref))
        object.__setattr__(self, "output_names", tuple(sorted(self.output_names)))
        object.__setattr__(self, "theorem_ids", tuple(self.theorem_ids))

        _require_equal(self.schema_version, VCSCORE_RUN_CERTIFICATE_SCHEMA_VERSION, "schema_version")
        _require_equal(self.validator, VCSCORE_RUN_CERTIFICATE_VALIDATOR, "validator")
        if self.theorem_ids != VCSCORE_RUN_THEOREM_IDS:
            raise VcsCoreCertificateError(f"VcsCore theorem ids must be {VCSCORE_RUN_THEOREM_IDS!r}")
        for field_name, value in (
            ("run_ref", self.run_ref),
            ("task_id", self.task_id),
            ("task_version", self.task_version),
            ("task_schema_digest", self.task_schema_digest),
            ("args_digest", self.args_digest),
            ("provider", self.provider),
            ("may_profile", self.may_profile),
            ("status", self.status),
            ("body_status", self.body_status),
            ("world_disposition", self.world_disposition),
            ("output_publication_status", self.output_publication_status),
            ("input_workspace_world_oid", self.input_workspace_world_oid),
            ("trace_head", self.trace_head),
        ):
            _require_non_empty_str(value, field_name)
        _require_optional_str(self.terminal_workspace_world_oid, "terminal_workspace_world_oid")
        _require_optional_str(self.runtime_operation, "runtime_operation")
        _require_optional_str(self.run_start_revision, "run_start_revision")
        _validate_trace_ref(self.trace_ref, run_ref=self.run_ref)
        _validate_terminal_state(self)

    def to_json(self) -> JsonObject:
        """Return the stable certificate payload."""

        return {
            "schema_version": self.schema_version,
            "validator": self.validator,
            "run_ref": self.run_ref,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "task_schema_digest": self.task_schema_digest,
            "args_digest": self.args_digest,
            "provider": self.provider,
            "may_profile": self.may_profile,
            "status": self.status,
            "body_status": self.body_status,
            "world_disposition": self.world_disposition,
            "output_publication_status": self.output_publication_status,
            "input_workspace_world_oid": self.input_workspace_world_oid,
            "terminal_workspace_world_oid": self.terminal_workspace_world_oid,
            "trace_ref": dict(self.trace_ref),
            "trace_head": self.trace_head,
            "runtime_operation": self.runtime_operation,
            "run_start_revision": self.run_start_revision,
            "output_names": list(self.output_names),
            "theorem_ids": list(self.theorem_ids),
        }

    def certificate_ref(self) -> Ref:
        """Content ref for this certificate."""

        return content_ref("vcscore-run-certificate", self.to_json())

    def program_ref(self) -> Ref:
        """Program-shaped ref used by the generic proof envelope ABI."""

        return content_ref(
            "program",
            {
                "certificate_ref": self.certificate_ref(),
                "run_ref": self.run_ref,
                "task_id": self.task_id,
                "task_version": self.task_version,
                "task_schema_digest": self.task_schema_digest,
            },
        )

    def trace_content_ref(self) -> Ref:
        """Trace-shaped ref used by the generic proof envelope ABI."""

        return content_ref(
            "trace",
            {
                "run_ref": self.run_ref,
                "trace_ref": dict(self.trace_ref),
                "trace_head": self.trace_head,
            },
        )


def vcscore_run_certificate_from_json(data: Mapping[str, JsonValue]) -> VcsCoreRunCertificate:
    """Decode and validate a VcsCore run certificate."""

    return VcsCoreRunCertificate(
        run_ref=_required_str(data, "run_ref"),
        task_id=_required_str(data, "task_id"),
        task_version=_required_str(data, "task_version"),
        task_schema_digest=_required_str(data, "task_schema_digest"),
        args_digest=_required_str(data, "args_digest"),
        provider=_required_str(data, "provider"),
        may_profile=_required_str(data, "may_profile"),
        status=_required_str(data, "status"),
        body_status=_required_str(data, "body_status"),
        world_disposition=_required_str(data, "world_disposition"),
        output_publication_status=_required_str(data, "output_publication_status"),
        input_workspace_world_oid=_required_str(data, "input_workspace_world_oid"),
        terminal_workspace_world_oid=_optional_str(data, "terminal_workspace_world_oid"),
        trace_ref=_required_mapping(data, "trace_ref"),
        trace_head=_required_str(data, "trace_head"),
        runtime_operation=_optional_str(data, "runtime_operation"),
        run_start_revision=_optional_str(data, "run_start_revision"),
        output_names=_string_tuple(data.get("output_names", ()), "output_names"),
        schema_version=_required_str(data, "schema_version"),
        validator=_required_str(data, "validator"),
        theorem_ids=_string_tuple(data.get("theorem_ids", ()), "theorem_ids"),
    )


def vcscore_run_certificate_from_run_record(record: Mapping[str, JsonValue]) -> VcsCoreRunCertificate:
    """Project a public workspace-control RunRecord JSON row into a certificate."""

    trace_ref = _required_mapping(record, "trace_ref")
    operation_refs = _required_mapping(record, "operation_refs")
    terminalization = _required_mapping(record, "terminalization")
    outputs = _optional_mapping(record, "outputs")
    _validate_outputs(outputs, trace_ref=trace_ref, terminal_world=_optional_str(record, "terminal_workspace_world_oid"))
    return VcsCoreRunCertificate(
        run_ref=_required_str(record, "run_ref"),
        task_id=_required_str(record, "task_id"),
        task_version=_required_str(record, "task_version"),
        task_schema_digest=_required_str(record, "task_schema_digest"),
        args_digest=_required_str(record, "args_digest"),
        provider=_required_str(record, "provider"),
        may_profile=_required_str(record, "may_profile"),
        status=_required_str(record, "status"),
        body_status=_required_str(terminalization, "body_status"),
        world_disposition=_required_str(terminalization, "world_disposition"),
        output_publication_status=_required_str(terminalization, "output_publication_status"),
        input_workspace_world_oid=_required_str(record, "input_workspace_world_oid"),
        terminal_workspace_world_oid=_optional_str(record, "terminal_workspace_world_oid"),
        trace_ref=trace_ref,
        trace_head=_required_str(operation_refs, "trace_head"),
        runtime_operation=_optional_str(operation_refs, "runtime_operation"),
        run_start_revision=_optional_str(operation_refs, "run_start_revision"),
        output_names=tuple(outputs),
    )


def vcscore_run_proof_envelope(certificate: VcsCoreRunCertificate) -> ProofEnvelope:
    """Mint the Lean-backed extension envelope for a validated VcsCore certificate."""

    certificate = vcscore_run_certificate_from_json(certificate.to_json())
    program_ref = certificate.program_ref()
    trace_ref = certificate.trace_content_ref()
    evidence_payload = {
        "proof_authority": PROOF_ENVELOPE_VALIDATOR,
        "validator": VCSCORE_RUN_CERTIFICATE_VALIDATOR,
        "envelope_schema_version": PROOF_ENVELOPE_SCHEMA_VERSION,
        "extension_name": VCSCORE_RUN_EXTENSION_NAME,
        "profile": ProofProfile.EXTENSION.value,
        "strength": ProofStrength.SEMANTIC_ADEQUACY.value,
        "certificate_ref": certificate.certificate_ref(),
        "program_ref": program_ref,
        "trace_ref": trace_ref,
        "theorem_ids": list(VCSCORE_RUN_THEOREM_IDS),
    }
    return ProofEnvelope(
        profile=ProofProfile.EXTENSION,
        strength=ProofStrength.SEMANTIC_ADEQUACY,
        evidence_id=content_ref(PROOF_EVIDENCE_REF_KIND, evidence_payload),
        program_ref=program_ref,
        trace_ref=trace_ref,
        theorem_ids=VCSCORE_RUN_THEOREM_IDS,
        metadata=(
            ("certificate_ref", certificate.certificate_ref()),
            ("extension_name", VCSCORE_RUN_EXTENSION_NAME),
            ("validator", VCSCORE_RUN_CERTIFICATE_VALIDATOR),
        ),
    )


def vcscore_run_proof_envelope_from_run_record(record: Mapping[str, JsonValue]) -> ProofEnvelope:
    """Mint the exact VcsCore proof envelope for a public RunRecord JSON row."""

    return vcscore_run_proof_envelope(vcscore_run_certificate_from_run_record(record))


def validate_vcscore_run_proof_envelope(record: Mapping[str, JsonValue], envelope: ProofEnvelope) -> None:
    """Require an envelope to be the exact certificate envelope for a RunRecord."""

    expected = vcscore_run_proof_envelope_from_run_record(record)
    if envelope.to_json() != expected.to_json():
        raise VcsCoreCertificateError("VcsCore proof envelope does not match the run record certificate")


def is_vcscore_run_proof_envelope(envelope: ProofEnvelope) -> bool:
    """Return whether an envelope has the VcsCore run extension shape."""

    return (
        envelope.profile is ProofProfile.EXTENSION
        and envelope.strength is ProofStrength.SEMANTIC_ADEQUACY
        and envelope.theorem_ids == VCSCORE_RUN_THEOREM_IDS
        and dict(envelope.metadata).get("extension_name") == VCSCORE_RUN_EXTENSION_NAME
    )


def _validate_terminal_state(certificate: VcsCoreRunCertificate) -> None:
    if certificate.status not in _TERMINAL_STATUSES:
        raise VcsCoreCertificateError(f"VcsCore certificate requires a terminal run status, got {certificate.status!r}")
    terminal_world = certificate.terminal_workspace_world_oid
    if certificate.status == "merged":
        _require_equal(certificate.body_status, "completed", "merged body_status")
        _require_equal(certificate.world_disposition, "merged", "merged world_disposition")
        _require_equal(certificate.output_publication_status, "not_applicable", "merged output_publication_status")
        _require_non_empty_str(terminal_world, "merged terminal_workspace_world_oid")
        return
    if certificate.status == "retained":
        _require_equal(certificate.body_status, "completed", "retained body_status")
        _require_equal(certificate.world_disposition, "retained", "retained world_disposition")
        _require_equal(certificate.output_publication_status, "published", "retained output_publication_status")
        _require_non_empty_str(terminal_world, "retained terminal_workspace_world_oid")
        if "workspace" not in certificate.output_names:
            raise VcsCoreCertificateError("retained VcsCore certificates require a published workspace output")
        return
    _require_equal(certificate.world_disposition, "discarded", f"{certificate.status} world_disposition")
    _require_equal(certificate.output_publication_status, "not_applicable", f"{certificate.status} output status")
    if certificate.body_status not in _TERMINAL_FAILURE_BODY_STATUSES:
        allowed = sorted(_TERMINAL_FAILURE_BODY_STATUSES)
        raise VcsCoreCertificateError(
            f"{certificate.status} VcsCore certificates require terminal failure body_status in {allowed!r}"
        )
    if terminal_world is not None:
        raise VcsCoreCertificateError(f"{certificate.status} VcsCore certificates must not carry a terminal world")


def _validate_trace_ref(trace_ref: Mapping[str, JsonValue], *, run_ref: str) -> None:
    if _required_str(trace_ref, "run_id") != run_ref:
        raise VcsCoreCertificateError("VcsCore trace_ref.run_id must match run_ref")
    _required_str(trace_ref, "execution_id")
    _required_str(trace_ref, "frontier_id")


def _validate_outputs(
    outputs: Mapping[str, JsonValue],
    *,
    trace_ref: Mapping[str, JsonValue],
    terminal_world: str | None,
) -> None:
    for key, raw in outputs.items():
        if not isinstance(key, str) or not key:
            raise VcsCoreCertificateError("run output keys must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise VcsCoreCertificateError("run output citations must be objects")
        output_name = _required_str(raw, "output_name")
        if output_name != key:
            raise VcsCoreCertificateError("run output citation output_name must match output key")
        raw_trace = _required_mapping(raw, "trace_ref")
        if dict(raw_trace) != dict(trace_ref):
            raise VcsCoreCertificateError("run output citation trace_ref must match run trace_ref")
        if key == "workspace" and terminal_world is not None:
            output_world = _required_str(raw, "output_world_oid")
            if output_world != terminal_world:
                raise VcsCoreCertificateError("workspace output world must match terminal world")


def _required_mapping(data: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise VcsCoreCertificateError(f"VcsCore {key} must be an object")
    return value


def _optional_mapping(data: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise VcsCoreCertificateError(f"VcsCore {key} must be an object")
    return value


def _required_str(data: Mapping[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise VcsCoreCertificateError(f"VcsCore {key} must be a non-empty string")
    return value


def _optional_str(data: Mapping[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise VcsCoreCertificateError(f"VcsCore {key} must be null or a non-empty string")
    return value


def _string_tuple(value: JsonValue, key: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise VcsCoreCertificateError(f"VcsCore {key} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise VcsCoreCertificateError(f"VcsCore {key} entries must be non-empty strings")
        result.append(item)
    return tuple(result)


def _require_non_empty_str(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise VcsCoreCertificateError(f"VcsCore {field_name} must be a non-empty string")


def _require_optional_str(value: object, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise VcsCoreCertificateError(f"VcsCore {field_name} must be null or a non-empty string")


def _require_equal(actual: object, expected: object, field_name: str) -> None:
    if actual != expected:
        raise VcsCoreCertificateError(f"VcsCore {field_name} must be {expected!r}, got {actual!r}")


__all__ = [
    "EXTENSION_PROOF_SURFACE_THEOREM_IDS",
    "VCSCORE_RUN_CERTIFICATE_SCHEMA_VERSION",
    "VCSCORE_RUN_CERTIFICATE_VALIDATOR",
    "VCSCORE_RUN_EXTENSION_NAME",
    "VCSCORE_RUN_THEOREM_IDS",
    "VcsCoreCertificateError",
    "VcsCoreRunCertificate",
    "is_vcscore_run_proof_envelope",
    "validate_vcscore_run_proof_envelope",
    "vcscore_run_certificate_from_json",
    "vcscore_run_certificate_from_run_record",
    "vcscore_run_proof_envelope",
    "vcscore_run_proof_envelope_from_run_record",
]
