from __future__ import annotations

from dataclasses import replace

import pytest

from shepherd_kernel_v3_reference.proof_envelope import ProofProfile, ProofStrength
from shepherd_kernel_v3_reference.vcscore_certificate import (
    VCSCORE_RUN_THEOREM_IDS,
    VcsCoreCertificateError,
    validate_vcscore_run_proof_envelope,
    vcscore_run_certificate_from_json,
    vcscore_run_certificate_from_run_record,
    vcscore_run_proof_envelope,
    vcscore_run_proof_envelope_from_run_record,
)


def _merged_record() -> dict[str, object]:
    return {
        "run_ref": "run-1",
        "task_id": "tasks.fix_bug",
        "task_version": "v1",
        "task_schema_digest": "sha256:task",
        "args_digest": "sha256:args",
        "may_profile": "ReadWrite",
        "provider": "shepherd.nucleus.v1",
        "status": "merged",
        "input_workspace_world_oid": "world-in",
        "terminal_workspace_world_oid": "world-out",
        "trace_ref": {
            "run_id": "run-1",
            "execution_id": "execution-1",
            "frontier_id": "frontier-1",
        },
        "operation_refs": {
            "runtime_operation": "operation-runtime",
            "trace_head": "trace-head",
        },
        "outputs": {},
        "terminalization": {
            "body_status": "completed",
            "world_disposition": "merged",
            "output_publication_status": "not_applicable",
        },
    }


def _retained_record() -> dict[str, object]:
    record = _merged_record()
    trace_ref = record["trace_ref"]
    terminal_world = record["terminal_workspace_world_oid"]
    record["status"] = "retained"
    record["outputs"] = {
        "workspace": {
            "output_name": "workspace",
            "output_id": "output-workspace",
            "output_world_oid": terminal_world,
            "trace_ref": trace_ref,
        },
    }
    record["terminalization"] = {
        "body_status": "completed",
        "world_disposition": "retained",
        "output_publication_status": "published",
    }
    return record


def test_vcscore_run_certificate_from_merged_run_record_round_trips() -> None:
    certificate = vcscore_run_certificate_from_run_record(_merged_record())

    decoded = vcscore_run_certificate_from_json(certificate.to_json())

    assert decoded == certificate
    assert certificate.run_ref == "run-1"
    assert certificate.theorem_ids == VCSCORE_RUN_THEOREM_IDS
    assert certificate.certificate_ref().startswith("vcscore-run-certificate:sha256:")
    assert certificate.program_ref().startswith("program:sha256:")
    assert certificate.trace_content_ref().startswith("trace:sha256:")


def test_vcscore_run_certificate_mints_lean_backed_extension_envelope() -> None:
    certificate = vcscore_run_certificate_from_run_record(_merged_record())

    envelope = vcscore_run_proof_envelope(certificate)

    assert envelope.profile is ProofProfile.EXTENSION
    assert envelope.strength is ProofStrength.SEMANTIC_ADEQUACY
    assert envelope.lean_backed
    assert envelope.proof_backed
    assert envelope.theorem_ids == VCSCORE_RUN_THEOREM_IDS
    assert envelope.evidence_id is not None
    assert envelope.evidence_id.startswith("proof-evidence:sha256:")
    assert envelope.program_ref == certificate.program_ref()
    assert envelope.trace_ref == certificate.trace_content_ref()


def test_vcscore_run_proof_envelope_from_run_record_matches_certificate() -> None:
    record = _merged_record()
    certificate = vcscore_run_certificate_from_run_record(record)

    envelope = vcscore_run_proof_envelope_from_run_record(record)

    assert envelope == vcscore_run_proof_envelope(certificate)
    validate_vcscore_run_proof_envelope(record, envelope)


def test_validate_vcscore_run_proof_envelope_rejects_forged_refs() -> None:
    record = _merged_record()
    envelope = vcscore_run_proof_envelope_from_run_record(record)
    forged = replace(envelope, trace_ref=f"trace:sha256:{'a' * 64}")

    with pytest.raises(VcsCoreCertificateError, match="does not match"):
        validate_vcscore_run_proof_envelope(record, forged)


def test_vcscore_run_certificate_rejects_trace_run_drift() -> None:
    record = _merged_record()
    trace_ref = dict(record["trace_ref"])  # type: ignore[arg-type]
    trace_ref["run_id"] = "run-other"
    record["trace_ref"] = trace_ref

    with pytest.raises(VcsCoreCertificateError, match="trace_ref.run_id"):
        vcscore_run_certificate_from_run_record(record)


def test_vcscore_run_certificate_rejects_non_terminal_rows() -> None:
    record = _merged_record()
    record["status"] = "running"

    with pytest.raises(VcsCoreCertificateError, match="terminal run status"):
        vcscore_run_certificate_from_run_record(record)


def test_vcscore_run_certificate_rejects_discarded_terminal_world() -> None:
    record = _merged_record()
    record["status"] = "failed"
    record["terminalization"] = {
        "body_status": "failed",
        "world_disposition": "discarded",
        "output_publication_status": "not_applicable",
    }

    with pytest.raises(VcsCoreCertificateError, match="terminal world"):
        vcscore_run_certificate_from_run_record(record)


@pytest.mark.parametrize("body_status", ["pending", "running", "completed"])
def test_vcscore_run_certificate_rejects_nonterminal_failed_body_status(body_status: str) -> None:
    record = _merged_record()
    record["status"] = "failed"
    record["terminal_workspace_world_oid"] = None
    record["terminalization"] = {
        "body_status": body_status,
        "world_disposition": "discarded",
        "output_publication_status": "not_applicable",
    }

    with pytest.raises(VcsCoreCertificateError, match="terminal failure body_status"):
        vcscore_run_certificate_from_run_record(record)


def test_vcscore_run_certificate_requires_retained_workspace_trace_ref() -> None:
    record = _retained_record()
    output = dict(record["outputs"]["workspace"])  # type: ignore[index]
    output.pop("trace_ref")
    record["outputs"] = {"workspace": output}

    with pytest.raises(VcsCoreCertificateError, match="trace_ref"):
        vcscore_run_certificate_from_run_record(record)


def test_vcscore_run_certificate_requires_retained_workspace_world() -> None:
    record = _retained_record()
    output = dict(record["outputs"]["workspace"])  # type: ignore[index]
    output.pop("output_world_oid")
    record["outputs"] = {"workspace": output}

    with pytest.raises(VcsCoreCertificateError, match="output_world_oid"):
        vcscore_run_certificate_from_run_record(record)
