"""World-integrity fsck for the world-storage layer.

Extracted from ``WorldStorageManager`` (260704-1410-plan.md V2.3). Off-hot-path
structural/deep validation of world commits and their retention closure. Consumes
the publication/retention controller (injected) for closure/retention checks; holds
no back-reference to WSM. Method bodies moved verbatim except self.<retention method>
calls re-pointed to self._pubret (behaviourally identical to the retired WSM shims).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._world_refs import (
    world_publication_lease_index_ref,
)
from vcs_core._world_storage_records import (
    DEFAULT_GROUND_REF,
    WorldFsckReport,
    _issue,
    _ProtectedRetention,
    _world_selected_pins_are_authoritative,
)
from vcs_core._world_store import WorldStore, WorldValidationProfile

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vcs_core._publication_retention_controller import PublicationRetentionController
    from vcs_core._substrate_store import SubstrateStore
    from vcs_core._world_closure import WorldClosure
    from vcs_core._world_store import WorldStore
    from vcs_core._world_types import (
        StructuredIssue,
    )


def _world_validation_issue(message: str, *, world_oid: str) -> StructuredIssue:
    if "evidence ref is missing" in message:
        return _issue(
            "missing_evidence_ref",
            message,
            world_oid=world_oid,
            recovery_hint="Restore the coordinator evidence record or archive the affected operation history.",
        )
    if "selected candidate outcome lacks a durable candidate ref" in message:
        return _issue(
            "missing_candidate_ref",
            message,
            world_oid=world_oid,
            recovery_hint="Restore the operation-scoped candidate ref or rely on published selected-head pins.",
        )
    if "missing substrate store" in message:
        return _issue("missing_store", message, world_oid=world_oid)
    if "does not contain selected head" in message:
        return _issue("missing_selected_head", message, world_oid=world_oid)
    if "operation-final digest" in message:
        return _issue("operation_final_digest_mismatch", message, world_oid=world_oid)
    return _issue("world_validation_failed", message, world_oid=world_oid)


class WorldFsckController:
    """Owns off-hot-path world-integrity fsck. Constructed by :class:`WorldStorageManager`."""

    def __init__(
        self,
        *,
        stores: Mapping[str, SubstrateStore],
        world_store: WorldStore,
        pubret: PublicationRetentionController,
    ) -> None:
        self._stores = stores
        self._world_store = world_store
        self._pubret = pubret

    def fsck_world(
        self,
        oid: str,
        *,
        authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,),
        mode: Literal["structural", "deep"] = "structural",
    ) -> WorldFsckReport:
        if mode == "structural":
            return self._fsck_world_structural(oid)
        if mode != "deep":
            raise ValueError(f"unsupported world fsck mode: {mode!r}")
        issues: list[StructuredIssue] = []
        pin_classification: dict[str, tuple[str, ...]] = {}
        closure: WorldClosure | None = None
        protected_retention = _ProtectedRetention(world_oids=frozenset(), refs=frozenset())
        try:
            closure = self._pubret.compute_resume_retention_closure(oid)
            pin_classification = self._pubret.classify_world_closure_retention(closure, authority_refs=authority_refs)
            protected_retention = self._pubret._protected_retention(authority_refs)
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            issues.append(_issue("pin_classification_failed", str(exc), world_oid=oid))
            closure = None
        if closure is not None:
            for world in closure.worlds:
                selected_pins_are_authoritative = _world_selected_pins_are_authoritative(
                    closure,
                    world_store_id=self._world_store.world_store_id,
                    world_oid=world.oid,
                    protected_world_oids=protected_retention.world_oids,
                    pin_classification=pin_classification,
                )
                try:
                    self._world_store.validate_world_commit(
                        world.oid,
                        self._stores,
                        require_selected_candidate_refs=not selected_pins_are_authoritative,
                        validate_input_worlds=False,
                        profile=WorldValidationProfile.DEEP,
                    )
                except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
                    issues.append(_world_validation_issue(str(exc), world_oid=world.oid))
        if pin_classification.get("missing_for_published_world"):
            issues.append(
                _issue(
                    "missing_selected_head_pins",
                    "published world is missing selected-head pins",
                    world_oid=oid,
                    recovery_hint="Re-pin the world's retention closure (repin_world_retention) or repair the substrate store.",
                ),
            )
        if pin_classification.get("corrupt"):
            issues.append(
                _issue(
                    "corrupt_selected_head_pins",
                    "world selected-head pins disagree with snapshot",
                    world_oid=oid,
                    recovery_hint="Do not trust the corrupted pins; inspect the affected substrate refs before repair.",
                ),
            )
        if closure is not None:
            self._pubret._extend_authority_lineage_retention_receipt_issues(
                issues,
                oid,
                authority_refs=authority_refs,
            )
        # Deep, off-hot-path stale/corrupt check of the active-lease accelerator against the
        # authoritative lease refs. This is the ONLY place the authority-comparing verify (a full
        # ref scan) runs from health reporting; the readiness/recovery-inventory probe is cheap
        # (index-only, corrupt detection only) — see _recovery_inventory._active_lease_index_items.
        lease_index_health = self._pubret.verify_active_lease_index()
        if lease_index_health.status in ("stale", "corrupt"):
            issues.append(
                _issue(
                    f"active_lease_index_{lease_index_health.status}",
                    f"active-lease accelerator is {lease_index_health.status} versus the authoritative "
                    f"lease refs: {lease_index_health.detail}",
                    store_id=self._world_store.world_store_id,
                    ref=world_publication_lease_index_ref(self._world_store.world_store_id),
                    recovery_hint="Run rebuild_active_lease_index() to reconcile the accelerator; the authority is unaffected.",
                ),
            )
        return WorldFsckReport(world_oid=oid, pin_classification=pin_classification, issue_details=tuple(issues))

    def fsck_world_structural(self, oid: str) -> WorldFsckReport:
        return self.fsck_world(oid, mode="structural")

    def fsck_world_deep(
        self,
        oid: str,
        *,
        authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,),
    ) -> WorldFsckReport:
        return self.fsck_world(oid, authority_refs=authority_refs, mode="deep")

    def _fsck_world_structural(self, oid: str) -> WorldFsckReport:
        issues: list[StructuredIssue] = []
        pin_classification: dict[str, tuple[str, ...]] = {}
        closure: WorldClosure | None = None
        protected_retention = _ProtectedRetention(world_oids=frozenset(), refs=frozenset())
        try:
            closure = self._pubret.compute_resume_retention_closure(oid)
            pin_classification = self._pubret.classify_world_closure_retention(
                closure, authority_refs=(DEFAULT_GROUND_REF,)
            )
            protected_retention = self._pubret._protected_retention((DEFAULT_GROUND_REF,))
        except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
            issues.append(_issue("pin_classification_failed", str(exc), world_oid=oid))
            closure = None
        worlds = closure.worlds if closure is not None else (self._world_store.read_world_commit(oid),)
        for world in worlds:
            selected_pins_are_authoritative = closure is not None and _world_selected_pins_are_authoritative(
                closure,
                world_store_id=self._world_store.world_store_id,
                world_oid=world.oid,
                protected_world_oids=protected_retention.world_oids,
                pin_classification=pin_classification,
            )
            try:
                self._world_store.validate_world_commit(
                    world.oid,
                    self._stores,
                    require_selected_candidate_refs=not selected_pins_are_authoritative,
                    validate_input_worlds=False,
                    profile=WorldValidationProfile.STRUCTURAL,
                )
            except (InvalidRepositoryStateError, KeyError, TypeError, ValueError) as exc:
                issues.append(_world_validation_issue(str(exc), world_oid=world.oid))
        if pin_classification.get("missing_for_published_world"):
            issues.append(
                _issue(
                    "missing_selected_head_pins",
                    "published world is missing selected-head pins",
                    world_oid=oid,
                    recovery_hint="Re-pin the world's retention closure (repin_world_retention) or repair the substrate store.",
                ),
            )
        if pin_classification.get("corrupt"):
            issues.append(
                _issue(
                    "corrupt_selected_head_pins",
                    "world selected-head pins disagree with snapshot",
                    world_oid=oid,
                    recovery_hint="Do not trust the corrupted pins; inspect the affected substrate refs before repair.",
                ),
            )
        return WorldFsckReport(world_oid=oid, pin_classification=pin_classification, issue_details=tuple(issues))
