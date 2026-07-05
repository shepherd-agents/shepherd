"""Frozen-contract pin for vcs-core's candidate/selection kernel (D12).

This is the kernel API-stability gate from the seal-and-select build plan
(``260614-2100-capability-c-seal-and-select-build-plan.md`` D12). The
seal-and-select build (capability C / best-of-N) writes new *durable* retained
state keyed on this kernel's primitives and persisted candidate records — but the
kernel is alpha (``SPI_VERSION == 0``, breaking changes permitted) with a live
T3 driver-dispatch cutover one layer below. These tests freeze the contract the
build depends on, so a kernel reshape trips loudly here instead of silently
corrupting retained state downstream.

The pins are *static* — signatures, version constants, the candidate-outcome
record fields, and the persisted serialization of a fixed record. They need no
pygit2 repo: ``CandidateOutcomeRecord`` is a plain frozen dataclass and serializes
deterministically (the heavy cross-validation lives on the *other* records).

If a change here is *intentional*, regenerate the golden and review the diff:

    REGEN_CANDIDATE_KERNEL_PIN=1 uv run --group test pytest \\
        tests/contract/test_candidate_kernel_pin.py
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import re
import typing
from pathlib import Path

from vcs_core._substrate_driver import SUBSTRATE_DRIVER_CONTRACT_REVISION
from vcs_core._substrate_store import SubstrateStore
from vcs_core._transition_kernel_records import (
    CANDIDATE_OUTCOME_SCHEMA,
    CandidateOutcomeRecord,
    CandidateOutcomeStatus,
)
from vcs_core._world_authority_finalizer import WorldAuthorityFinalizer
from vcs_core._world_operation_builder import CandidateSelection, OperationFinalBuilder
from vcs_core._world_storage_manager import (
    DEFAULT_GROUND_REF,
    SubstrateStoreSpec,
    WorldStorageManager,
)
from vcs_core._world_types import (
    OPERATION_FINAL_SCHEMA,
    WORLD_TRANSITION_SCHEMA,
    SubstrateStoreIdentity,
    WorldSnapshot,
)
from vcs_core.spi import SPI_VERSION

GOLDEN_PATH = Path(__file__).parent / "golden" / "candidate_kernel_pin_v0.json"

_FIX = (
    "candidate-kernel contract drift — see 260614-2100 D12. If intentional, "
    "regenerate the golden with REGEN_CANDIDATE_KERNEL_PIN=1 and review the diff."
)


def _representative_record() -> CandidateOutcomeRecord:
    """A fixed CandidateOutcomeRecord whose serialization is byte-stable."""
    return CandidateOutcomeRecord(
        binding="workspace",
        candidate="b3635880000000000000000000000000000000f1",
        outcome="selected",
        transition_digest="sha256:" + "11" * 32,
        content_digest="sha256:" + "22" * 32,
    )


def _sig(obj: object) -> str:
    return str(inspect.signature(obj))


def _live_contract() -> dict:
    rec = _representative_record()
    return {
        "_comment": "Frozen contract for vcs-core candidate/selection kernel. See 260614-2100 D12.",
        "spi_version": SPI_VERSION,
        "substrate_driver_contract_revision": SUBSTRATE_DRIVER_CONTRACT_REVISION,
        "candidate_outcome_schema": CANDIDATE_OUTCOME_SCHEMA,
        "operation_final_schema": OPERATION_FINAL_SCHEMA,
        "candidate_outcome_status_args": list(typing.get_args(CandidateOutcomeStatus)),
        "signatures": {
            "OperationFinalBuilder.select_candidate_plan": _sig(OperationFinalBuilder.select_candidate_plan),
            "OperationFinalBuilder.archive_candidate": _sig(OperationFinalBuilder.archive_candidate),
            "OperationFinalBuilder.build_prepared": _sig(OperationFinalBuilder.build_prepared),
            "WorldAuthorityFinalizer.publish_prepared": _sig(WorldAuthorityFinalizer.publish_prepared),
            "SubstrateStore.create_candidate_from_prepared": _sig(SubstrateStore.create_candidate_from_prepared),
            "SubstrateStore.create_unsafe_unprepared_candidate": _sig(
                SubstrateStore.create_unsafe_unprepared_candidate
            ),
        },
        "candidate_outcome_record_fields": [
            [f.name, getattr(f.type, "__name__", str(f.type))] for f in dataclasses.fields(CandidateOutcomeRecord)
        ],
        "candidate_outcome_record_to_json": rec.to_json(final_operation_id="op-1"),
        "candidate_outcome_record_to_record_json": rec.to_record_json(final_operation_id="op-1"),
    }


if os.environ.get("REGEN_CANDIDATE_KERNEL_PIN") == "1":
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(_live_contract(), indent=2, sort_keys=True) + "\n")


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def test_version_constants_frozen() -> None:
    golden = _golden()
    # Literal intent: the alpha kernel must still announce v0 / v0.1.
    assert SPI_VERSION == 0, _FIX
    assert SUBSTRATE_DRIVER_CONTRACT_REVISION == "v0.1", _FIX
    assert CANDIDATE_OUTCOME_SCHEMA == "vcscore/candidate-outcome/v1", _FIX
    assert OPERATION_FINAL_SCHEMA == "vcscore/operation-final/v2", _FIX
    # Golden tracks live (so the golden can't silently drift from reality).
    assert golden["spi_version"] == SPI_VERSION, _FIX
    assert golden["substrate_driver_contract_revision"] == SUBSTRATE_DRIVER_CONTRACT_REVISION, _FIX
    assert golden["candidate_outcome_schema"] == CANDIDATE_OUTCOME_SCHEMA, _FIX
    assert golden["operation_final_schema"] == OPERATION_FINAL_SCHEMA, _FIX


def test_candidate_outcome_status_literal_frozen() -> None:
    assert typing.get_args(CandidateOutcomeStatus) == ("selected", "archived"), _FIX
    assert _golden()["candidate_outcome_status_args"] == ["selected", "archived"], _FIX


def test_candidate_primitive_signatures_frozen() -> None:
    live = _live_contract()["signatures"]
    golden = _golden()["signatures"]
    assert set(live) == set(golden), _FIX
    for name in sorted(golden):
        assert live[name] == golden[name], f"{name}: {_FIX}"


def test_candidate_outcome_record_fields_frozen() -> None:
    assert _live_contract()["candidate_outcome_record_fields"] == (_golden()["candidate_outcome_record_fields"]), _FIX


def test_candidate_outcome_record_serialization_frozen() -> None:
    # The persisted shape the D11 seal->candidate handoff durably writes; pinning
    # both the compact (operation-final.json) and full (record) emitters.
    rec = _representative_record()
    golden = _golden()
    assert rec.to_json(final_operation_id="op-1") == golden["candidate_outcome_record_to_json"], _FIX
    assert rec.to_record_json(final_operation_id="op-1") == (golden["candidate_outcome_record_to_record_json"]), _FIX


def test_full_and_legacy_candidate_paths_both_present() -> None:
    # D12 requires seal to use the FULL sidecar candidate, never the legacy path.
    # Seal lifecycle coverage now asserts the full prepared tuple is written to
    # the D11 handoff; here we pin that the discriminator surface remains named.
    assert hasattr(SubstrateStore, "create_candidate_from_prepared"), _FIX
    assert hasattr(SubstrateStore, "create_unsafe_unprepared_candidate"), _FIX


# --- Full persisted operation-final.json vector (the durable shape a reactivate reads) ---
#
# Complements the record-level pins above: those freeze the exact serialization +
# digest algorithm of a single CandidateOutcomeRecord; this freezes the *envelope*
# structure of the whole persisted operation-final.json (the sibling candidate_commits /
# head_selections / selection_evidence arrays + the top-level key set) that the D11
# seal->candidate handoff must round-trip across a reactivate.
#
# 23 leaf fields are OID/digest-derived and vary run-to-run because the candidate
# substrate commit is stamped with wall-clock time (`_substrate_store.py:700` builds
# `pygit2.Signature(...)` with no fixed time). We therefore pin the *structure*: the
# volatile leaves are redacted to sentinels, leaving every schema string, key set, ref
# scheme, and stable field frozen. (The digest *algorithm* is already pinned exactly by
# `test_candidate_outcome_record_serialization_frozen` on fixed inputs.)

_PAYLOAD_GOLDEN_PATH = Path(__file__).parent / "golden" / "operation_final_payload_v0.json"

_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_REF_RE = re.compile(r"^(refs/vcscore/evidence/[^/]+/)[0-9a-f]{64}$")


def _redact(obj: object) -> object:
    """Replace commit-time-dependent OIDs/digests with sentinels, keeping structure."""
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        if _OID_RE.match(obj):
            return "<OID>"
        if _DIGEST_RE.match(obj):
            return "sha256:<DIGEST>"
        evidence = _EVIDENCE_REF_RE.match(obj)
        if evidence:
            return evidence.group(1) + "<DIGEST>"
    return obj


def _build_operation_final_payload(repo_root: Path) -> dict:
    """Persisted operation-final.json payload for a fixed 1-candidate 'selected'
    scenario. Build-only: FinalizedWorldOperation.operation_final.payload IS the
    decoded persisted dict (identical to a read-back of meta/operation-final.json)."""
    manager = WorldStorageManager.open_or_init(
        repo_root / ".vcscore",
        world_store_id="store_world_test",
        stores=(
            SubstrateStoreSpec(
                identity=SubstrateStoreIdentity(
                    store_id="store_workspace", kind="filesystem", resource_id="fs:repo-main"
                ),
                locator="substrates/workspace.git",
            ),
        ),
    )
    parent = manager.create_unsafe_unprepared_json_revision(
        "store_workspace", "refs/heads/main", {"schema": "example/workspace", "n": 1}
    )
    bundle = manager.create_prepared_json_candidate_bundle(
        "store_workspace",
        operation_id="op-1",
        binding="workspace",
        candidate_id="primary",
        payload={"schema": "example/workspace", "n": 2},
        parents=(parent,),
    )
    snapshot = WorldSnapshot(
        (manager.substrate_head("store_workspace", binding="workspace", head=bundle.candidate.head, role="r"),)
    )
    plan = manager.plan_candidate_selection(
        operation_id="op-1", selection=CandidateSelection.from_bundle(bundle), role="r"
    )
    transition = {
        "schema": WORLD_TRANSITION_SCHEMA,
        "operation_id": "op-1",
        "parent_worlds": ["f" * 40],
        "input_world": "f" * 40,
    }
    finalized = (
        OperationFinalBuilder("op-1")
        .select_candidate_plan(plan=plan)
        .build(
            operation_kind="merge",
            target_ref=DEFAULT_GROUND_REF,
            input_world_oid="f" * 40,
            snapshot=snapshot,
            transition=transition,
            parents=("f" * 40,),
            candidate_refs=(bundle.candidate,),
        )
    )
    return finalized.operation_final.payload


def test_operation_final_payload_persisted_shape_frozen(tmp_path) -> None:
    payload = _build_operation_final_payload(tmp_path)

    # Readable contract: the durable file's exact top-level key set (enforced by
    # `_validate_operation_final_payload`, _world_types.py).
    assert set(payload) == {
        "schema",
        "operation_id",
        "selected",
        "candidate_commits",
        "candidate_outcomes",
        "head_selections",
        "selection_evidence",
    }, _FIX
    assert payload["schema"] == "vcscore/operation-final/v2", _FIX

    # One real (stable, unpinned) digest value: content_digest hashes only the JSON
    # payload, so it pins the canonical-hash path end-to-end without clock-pinning.
    assert payload["candidate_outcomes"][0]["content_digest"] == (
        "sha256:1a9417386b64865fc607b6f304225c3d6d09671f578e9da6c7861b72095df162"
    ), _FIX

    redacted = _redact(payload)
    if os.environ.get("REGEN_CANDIDATE_KERNEL_PIN") == "1":
        _PAYLOAD_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PAYLOAD_GOLDEN_PATH.write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n")
    golden = json.loads(_PAYLOAD_GOLDEN_PATH.read_text())
    assert redacted == golden, _FIX
