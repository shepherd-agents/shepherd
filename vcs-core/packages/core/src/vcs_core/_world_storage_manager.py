"""Installation-local manager for v2 world storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._substrate_store import SubstrateStore
from vcs_core._world_operation_journal import OperationJournalStore
from vcs_core._world_refs import (
    world_fork_origin_receipt_ref,
    world_open_operation_journal_index_ref,
)
from vcs_core._world_storage_records import (
    DEFAULT_GROUND_REF,
    OperationJournalFsckReport,
    WorldFsckReport,
    _AuthorityLineageSegments,
    _current_ref_target,
    _ForkOriginReceipt,
    _issue,
    _object_list,
    _ProtectedRetention,
    _read_world_fork_origin_receipt,
    _validate_advance_basis,
    _world_operation_id,
)
from vcs_core._world_store import WorldStore
from vcs_core._world_transition_coordinator import (
    CoordinatorEvidenceOnlyIngress,
    WorldTransitionCoordinator,
    WorldTransitionCoordinatorProtocol,
)
from vcs_core._world_types import (
    CandidateRevision,
    OperationFinalRecord,
    StructuredIssue,
    SubstrateHead,
    SubstrateStoreIdentity,
    WorldCommit,
    WorldRefPayload,
    WorldSnapshot,
    compact_json_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pygit2

    from vcs_core._incremental import Health, OpenOperationJournalIndex

    # Return types of the publication delegation shims (logic in PublicationRetentionController).
    from vcs_core._publication_retention_controller import PreparedPublication
    from vcs_core._substrate_driver import (
        DriverContext,
        DriverIngressResult,
        IngressRequest,
        ReductionBatch,
        SubstrateDriver,
    )
    from vcs_core._transition_kernel import TransitionKernelDriver
    from vcs_core._transition_kernel_records import (
        CandidateCommitRecord,
        EvidenceRef,
        LogicalTransition,
        PreparedRevisionPlan,
        RelationshipRequirement,
        RetentionPolicyRequirement,
        RevisionPreparationRecord,
    )
    from vcs_core._world_closure import WorldClosure
    from vcs_core._world_operation_builder import (
        CandidateSelection,
        CandidateSelectionPlan,
        PreparedCandidateTupleRecord,
        PreparedWorldOperation,
        SelectionRequirementPlan,
    )
    from vcs_core._world_operation_journal import (
        OperationJournalEntry,
        OperationJournalHistory,
        OperationJournalSummary,
    )
    from vcs_core._world_publication_plan import PublicationPlan

INSTALLATION_SCHEMA = "vcscore/world-storage-installation/v1"
DEFAULT_COORDINATOR_LOCATOR = "worlds.git"


@dataclass(frozen=True)
class SubstrateStoreSpec:
    """Installation-local locator plus stable substrate store identity."""

    identity: SubstrateStoreIdentity
    locator: str

    def __post_init__(self) -> None:
        _validate_relative_locator(self.locator)

    def to_json(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_json(),
            "locator": self.locator,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> SubstrateStoreSpec:
        raw_identity = value.get("identity")
        if not isinstance(raw_identity, dict):
            raise TypeError("substrate store spec identity must be an object")
        locator = value.get("locator")
        if not isinstance(locator, str) or not locator:
            raise ValueError("substrate store spec locator is required")
        return cls(identity=SubstrateStoreIdentity.from_json(raw_identity), locator=locator)


@dataclass(frozen=True)
class OperationJournalsFsckReport:
    """Store-global integrity report over EVERY operation-journal ref.

    Covers all families, including unknown/unsupported ones and corrupt terminals. This is the
    canonical, explicit, off-hot-path home for terminal-journal integrity: open-only admission
    deliberately stops *blocking* on corrupt non-`open` journals (over-blocking), but their
    *detection* must not be lost — it surfaces here (and via the read-only inspect path) instead.
    Distinct from the per-operation :class:`OperationJournalFsckReport`. ``scanned`` is the number
    of v2-shaped refs walked.
    """

    scanned: int
    issue_details: tuple[StructuredIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issue_details

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issue_details)


@dataclass(frozen=True)
class SelectedHeadCandidateProvenance:
    """Candidate custody for a selected binding head, traced through unchanged worlds."""

    world_oid: str
    producer_world_oid: str
    producer_operation_id: str
    selected_head: SubstrateHead
    candidate_tuple: PreparedCandidateTupleRecord


@dataclass(frozen=True)
class PreparedCandidateBundle:
    """Manager-produced candidate plus typed evidence needed by operation-final records."""

    candidate: CandidateRevision
    candidate_commit: CandidateCommitRecord
    transition: LogicalTransition
    plan: PreparedRevisionPlan
    preparation: RevisionPreparationRecord


@dataclass(frozen=True)
class PreparedRevisionBundle:
    """Manager-produced non-candidate revision plus typed provenance records."""

    head: str
    ref: str
    transition: LogicalTransition
    plan: PreparedRevisionPlan
    preparation: RevisionPreparationRecord


class WorldStorageManager:
    """Private manager that binds one v2 world installation to its stores."""

    def __init__(
        self,
        *,
        root: Path,
        world_store: WorldStore,
        store_specs: Mapping[str, SubstrateStoreSpec],
        stores: Mapping[str, SubstrateStore],
    ) -> None:
        self._root = root
        self._world_store = world_store
        self._operation_journal = OperationJournalStore(world_store.repo)
        self._store_specs = dict(store_specs)
        self._stores = dict(stores)
        self._transition_coordinator: WorldTransitionCoordinatorProtocol = WorldTransitionCoordinator(
            world_store=world_store,
            stores=self._stores,
        )
        # Publication + retention state machine, extracted to PublicationRetentionController
        # (V2.2c). WSM keeps delegation shims below; the controller holds the real logic with
        # injected state + outbound callables. Constructed first because the journal controller's
        # publication-plan validator is one of its methods (the retired V2.1 residual). Lazy
        # imports keep both edges out of the module-level import graph.
        from vcs_core._operation_journal_controller import OperationJournalController
        from vcs_core._publication_retention_controller import PublicationRetentionController

        self._pubret = PublicationRetentionController(
            stores=self._stores,
            transition_coordinator=self._transition_coordinator,
            world_store=self._world_store,
            authority_lineage_segments=self._authority_lineage_segments,
            input_world_lineage=self._input_world_lineage,
            fsck_world=self.fsck_world,
            read_operation_journal=self.read_operation_journal,
        )

        # Journal state machine, extracted to OperationJournalController (V2.1). Its
        # publication-plan validator now binds to the pub/ret controller (V2.2c tripwire flip).
        self._journal = OperationJournalController(
            operation_journal=self._operation_journal,
            world_store=self._world_store,
            stores=self._stores,
            validate_prepared_operation_admission=self._transition_coordinator.validate_prepared_operation_admission,
            validate_publication_plan=self._pubret._validate_publication_plan,
        )
        # Genuine mutual reference: pub/ret reads the journal controller. Wired by WSM as the
        # composition root once both exist (neither can be constructed fully before the other).
        self._pubret._journal = self._journal

        # World-integrity fsck, extracted to WorldFsckController (V2.3). Consumes the
        # pub/ret controller; WSM keeps fsck_world/_deep shims (fsck_world is also an
        # injected pub/ret dependency).
        from vcs_core._world_fsck import WorldFsckController

        self._fsck = WorldFsckController(
            stores=self._stores,
            world_store=self._world_store,
            pubret=self._pubret,
        )

    # --- WorldFsckController delegation shims (V2.3) ---
    # fsck_world is also injected into PublicationRetentionController; keep the shim.
    def fsck_world(
        self,
        oid: str,
        *,
        authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,),
        mode: Literal["structural", "deep"] = "structural",
    ) -> WorldFsckReport:
        return self._fsck.fsck_world(oid, authority_refs=authority_refs, mode=mode)

    def fsck_world_deep(self, oid: str, *, authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,)) -> WorldFsckReport:
        return self._fsck.fsck_world_deep(oid, authority_refs=authority_refs)

    # --- PublicationRetentionController delegation shims (V2.2c) ---
    # Import compatibility for externally-referenced publication/retention methods;
    # logic lives in self._pubret. Do not add a self._pubret bypass here (rule 9).
    def repin_world_retention(self, oid: str) -> tuple[str, ...]:
        return self._pubret.repin_world_retention(oid)

    def compute_publish_retention_closure(self, oid: str) -> WorldClosure:
        return self._pubret.compute_publish_retention_closure(oid)

    def _active_lease_targets_via_index(self) -> frozenset[str]:
        return self._pubret._active_lease_targets_via_index()

    def _active_publication_lease_refs(self) -> tuple[str, ...]:
        return self._pubret._active_publication_lease_refs()

    def _active_publication_lease_targets(self) -> frozenset[str]:
        return self._pubret._active_publication_lease_targets()

    def _extend_authority_lineage_retention_receipt_issues(
        self, issues: list[StructuredIssue], oid: str, *, authority_refs: tuple[str, ...]
    ) -> None:
        return self._pubret._extend_authority_lineage_retention_receipt_issues(
            issues, oid, authority_refs=authority_refs
        )

    def _protected_retention(self, authority_refs: tuple[str, ...]) -> _ProtectedRetention:
        return self._pubret._protected_retention(authority_refs)

    def _publish_world(
        self,
        *,
        ref: str,
        world_oid: str,
        expected_oid: str | None,
        allow_same_resource_alias: bool,
        authority_refs: tuple[str, ...] | None,
    ) -> bool:
        return self._pubret._publish_world(
            ref=ref,
            world_oid=world_oid,
            expected_oid=expected_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def _record_lease_index(self, *, add: tuple[str, str, str] | None = None, remove: str | None = None) -> None:
        return self._pubret._record_lease_index(add=add, remove=remove)

    def _release_publication_leases(self, lease_refs: tuple[str, ...], *, world_oid: str) -> None:
        return self._pubret._release_publication_leases(lease_refs, world_oid=world_oid)

    def _reset_lease_index(self) -> None:
        return self._pubret._reset_lease_index()

    def _validate_authority_retention_preflight(
        self, authority_refs: tuple[str, ...], *, allow_same_resource_alias: bool
    ) -> None:
        return self._pubret._validate_authority_retention_preflight(
            authority_refs, allow_same_resource_alias=allow_same_resource_alias
        )

    def _validate_publication_plan(
        self,
        plan: PublicationPlan,
        *,
        expected_world_oid: str | None = None,
        expected_authority_ref: str | None = None,
        expected_input_world_oid: str | None = None,
    ) -> None:
        return self._pubret._validate_publication_plan(
            plan,
            expected_world_oid=expected_world_oid,
            expected_authority_ref=expected_authority_ref,
            expected_input_world_oid=expected_input_world_oid,
        )

    def _write_publication_leases(self, authority_refs: tuple[str, ...], world: WorldCommit) -> tuple[str, ...]:
        return self._pubret._write_publication_leases(authority_refs, world)

    def active_lease_index_corruption(self) -> str | None:
        return self._pubret.active_lease_index_corruption()

    def advance_publication(self, prepared: PreparedPublication) -> bool:
        return self._pubret.advance_publication(prepared)

    def build_advance_publication_plan(
        self,
        *,
        ref: str,
        world_oid: str,
        expected_oid: str,
        input_world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> PublicationPlan:
        return self._pubret.build_advance_publication_plan(
            ref=ref,
            world_oid=world_oid,
            expected_oid=expected_oid,
            input_world_oid=input_world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def build_publication_plan(
        self,
        *,
        ref: str,
        world_oid: str,
        expected_oid: str | None,
        input_world_oid: str | None,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> PublicationPlan:
        return self._pubret.build_publication_plan(
            ref=ref,
            world_oid=world_oid,
            expected_oid=expected_oid,
            input_world_oid=input_world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def build_root_publication_plan(
        self,
        *,
        ref: str,
        world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> PublicationPlan:
        return self._pubret.build_root_publication_plan(
            ref=ref,
            world_oid=world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def classify_world_closure_retention(
        self, closure: WorldClosure, *, authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,)
    ) -> dict[str, tuple[str, ...]]:
        return self._pubret.classify_world_closure_retention(closure, authority_refs=authority_refs)

    def cleanup_orphan_pins(
        self, oid: str, *, authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,)
    ) -> tuple[str, ...]:
        return self._pubret.cleanup_orphan_pins(oid, authority_refs=authority_refs)

    def cleanup_stale_publication_leases(
        self, *, authority_refs: tuple[str, ...] = (DEFAULT_GROUND_REF,), abandon_journalless: bool = False
    ) -> tuple[str, ...]:
        return self._pubret.cleanup_stale_publication_leases(
            authority_refs=authority_refs, abandon_journalless=abandon_journalless
        )

    def cleanup_stale_terminal_operation_open_ref(self, operation_id: str, *, terminal_family: str) -> bool:
        return self._pubret.cleanup_stale_terminal_operation_open_ref(operation_id, terminal_family=terminal_family)

    def complete_publication(self, prepared: PreparedPublication) -> None:
        return self._pubret.complete_publication(prepared)

    def compute_resume_retention_closure(self, oid: str) -> WorldClosure:
        return self._pubret.compute_resume_retention_closure(oid)

    def compute_world_closure(self, oid: str) -> WorldClosure:
        return self._pubret.compute_world_closure(oid)

    def pin_world_closure(self, closure: WorldClosure) -> tuple[str, ...]:
        return self._pubret.pin_world_closure(closure)

    def prepare_publication(self, plan: PublicationPlan) -> PreparedPublication:
        return self._pubret.prepare_publication(plan)

    def publish_root_world(
        self,
        *,
        ref: str,
        world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> bool:
        return self._pubret.publish_root_world(
            ref=ref,
            world_oid=world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )

    def rebuild_active_lease_index(self) -> None:
        return self._pubret.rebuild_active_lease_index()

    def record_operation_published(self, operation_id: str, *, world_oid: str) -> OperationJournalEntry:
        return self._pubret.record_operation_published(operation_id, world_oid=world_oid)

    def record_operation_publishing(
        self, operation_id: str, *, world_oid: str, publication_plan: PublicationPlan
    ) -> OperationJournalEntry:
        return self._pubret.record_operation_publishing(
            operation_id, world_oid=world_oid, publication_plan=publication_plan
        )

    def validate_publish_closure(
        self, oid: str, *, authority_refs: tuple[str, ...] = (), allow_same_resource_alias: bool = False
    ) -> WorldClosure:
        return self._pubret.validate_publish_closure(
            oid, authority_refs=authority_refs, allow_same_resource_alias=allow_same_resource_alias
        )

    def verify_active_lease_index(self) -> Health:
        return self._pubret.verify_active_lease_index()

    def write_world_fork_origin_receipt(
        self, *, authority_ref: str, first_world_oid: str, forked_from_authority_ref: str, forked_from_world_oid: str
    ) -> str:
        return self._pubret.write_world_fork_origin_receipt(
            authority_ref=authority_ref,
            first_world_oid=first_world_oid,
            forked_from_authority_ref=forked_from_authority_ref,
            forked_from_world_oid=forked_from_world_oid,
        )

    def write_world_retention_receipt(
        self, *, authority_ref: str, world_oid: str, closure: WorldClosure, retained_refs: tuple[str, ...]
    ) -> str:
        return self._pubret.write_world_retention_receipt(
            authority_ref=authority_ref, world_oid=world_oid, closure=closure, retained_refs=retained_refs
        )

    @classmethod
    def open_or_init(
        cls,
        root: str | Path,
        *,
        world_store_id: str,
        stores: tuple[SubstrateStoreSpec, ...],
        substrate_shared_object_repo_path: str | Path | None = None,
    ) -> WorldStorageManager:
        """Open or initialize a v2 world installation.

        ``substrate_shared_object_repo_path``, when set, points at a Git
        repository whose ODB substrate stores need to read from.  In the
        production install, this is the scalar vcs-core store: the workspace
        bytes used by tree-backed substrate revisions live there, and the
        substrate store needs alternates pointing at it before libgit2 will
        accept a ``workspace/`` tree entry referencing a foreign tree oid.
        """
        root_path = Path(root)
        specs_by_id = _specs_by_id(stores)
        root_path.mkdir(parents=True, exist_ok=True)
        if _installation_config_path(root_path).exists():
            _validate_installation_config(root_path, world_store_id=world_store_id, specs_by_id=specs_by_id)
            world_store = WorldStore.open_existing(
                root_path / DEFAULT_COORDINATOR_LOCATOR,
                world_store_id=world_store_id,
            )
            substrate_stores = {
                store_id: SubstrateStore.open_existing(
                    root_path / spec.locator,
                    spec.identity,
                    shared_object_repo_path=substrate_shared_object_repo_path,
                )
                for store_id, spec in specs_by_id.items()
            }
        else:
            _write_installation_config(root_path, world_store_id=world_store_id, specs_by_id=specs_by_id)
            world_store = WorldStore.open_or_init(
                root_path / DEFAULT_COORDINATOR_LOCATOR,
                world_store_id=world_store_id,
            )
            substrate_stores = {
                store_id: SubstrateStore.open_or_init(
                    root_path / spec.locator,
                    spec.identity,
                    shared_object_repo_path=substrate_shared_object_repo_path,
                )
                for store_id, spec in specs_by_id.items()
            }
        return cls(root=root_path, world_store=world_store, store_specs=specs_by_id, stores=substrate_stores)

    @classmethod
    def open_existing(
        cls,
        root: str | Path,
        *,
        world_store_id: str,
        stores: tuple[SubstrateStoreSpec, ...],
        substrate_shared_object_repo_path: str | Path | None = None,
    ) -> WorldStorageManager:
        """Open an existing v2 world installation without creating filesystem state.

        See :meth:`open_or_init` for the ``substrate_shared_object_repo_path``
        alternates contract.
        """
        root_path = Path(root)
        specs_by_id = _specs_by_id(stores)
        _validate_installation_config(root_path, world_store_id=world_store_id, specs_by_id=specs_by_id)
        world_store = WorldStore.open_existing(
            root_path / DEFAULT_COORDINATOR_LOCATOR,
            world_store_id=world_store_id,
        )
        substrate_stores = {
            store_id: SubstrateStore.open_existing(
                root_path / spec.locator,
                spec.identity,
                shared_object_repo_path=substrate_shared_object_repo_path,
            )
            for store_id, spec in specs_by_id.items()
        }
        return cls(root=root_path, world_store=world_store, store_specs=specs_by_id, stores=substrate_stores)

    @classmethod
    def rebind_store_locator(
        cls,
        root: str | Path,
        *,
        world_store_id: str,
        store_id: str,
        locator: str,
    ) -> None:
        """Rewrite one install-local locator after validating the target existing store."""
        root_path = Path(root)
        _validate_relative_locator(locator)
        current = _read_installation_config(root_path)
        if current.get("world_store_id") != world_store_id:
            raise InvalidRepositoryStateError("world storage installation world_store_id mismatch")
        specs_by_id = _store_specs_from_config(current)
        try:
            current_spec = specs_by_id[store_id]
        except KeyError as exc:
            raise InvalidRepositoryStateError(f"world storage installation has no store {store_id!r}") from exc
        target_path = root_path / locator
        SubstrateStore.open_existing(target_path, current_spec.identity)
        next_specs = {
            existing_store_id: (
                SubstrateStoreSpec(identity=spec.identity, locator=locator) if existing_store_id == store_id else spec
            )
            for existing_store_id, spec in specs_by_id.items()
        }
        _write_installation_config(root_path, world_store_id=world_store_id, specs_by_id=next_specs)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def world_store(self) -> WorldStore:
        return self._world_store

    @property
    def stores(self) -> dict[str, SubstrateStore]:
        return dict(self._stores)

    def store(self, store_id: str) -> SubstrateStore:
        try:
            return self._stores[store_id]
        except KeyError as exc:
            raise KeyError(f"world installation has no substrate store {store_id!r}") from exc

    def locator_hints(self) -> dict[str, str]:
        return {store_id: spec.locator for store_id, spec in sorted(self._store_specs.items())}

    def create_unsafe_unprepared_json_revision(
        self,
        store_id: str,
        ref: str,
        payload: dict[str, Any],
        *,
        parents: tuple[str | pygit2.Oid, ...] = (),
        message: str | None = None,
    ) -> str:
        """Create a provenance-free JSON revision for tests and migration tools."""
        return self.store(store_id).create_unsafe_unprepared_json_revision(
            ref,
            payload,
            parents=parents,
            message=message,
        )

    def create_unsafe_unprepared_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        candidate_id: str = "primary",
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        message: str | None = None,
    ) -> CandidateRevision:
        """Create a legacy candidate ref without full transition-kernel sidecars."""
        return self.store(store_id).create_unsafe_unprepared_candidate(
            operation_id=operation_id,
            binding=binding,
            candidate_id=candidate_id,
            payload=payload,
            parents=parents,
            message=message,
        )

    def create_prepared_json_candidate(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        candidate_id: str = "primary",
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> tuple[CandidateRevision, CandidateCommitRecord]:
        bundle = self.create_prepared_json_candidate_bundle(
            store_id,
            operation_id=operation_id,
            binding=binding,
            candidate_id=candidate_id,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            driver=driver,
            relationship_requirements=relationship_requirements,
            message=message,
        )
        return bundle.candidate, bundle.candidate_commit

    def create_prepared_json_revision(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> str:
        return self.create_prepared_json_revision_bundle(
            store_id,
            ref,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            driver=driver,
            relationship_requirements=relationship_requirements,
            message=message,
        ).head

    def create_prepared_json_revision_bundle(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> PreparedRevisionBundle:
        prepared = self._transition_coordinator.create_prepared_json_revision(
            store_id,
            ref,
            operation_id=operation_id,
            binding=binding,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            driver=driver,
            relationship_requirements=relationship_requirements,
            message=message,
        )
        return PreparedRevisionBundle(
            head=prepared.head,
            ref=prepared.ref,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=prepared.preparation,
        )

    def create_prepared_driver_revision_bundle(
        self,
        store_id: str,
        ref: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        driver_id: str,
        driver_version: str,
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
        message: str | None = None,
    ) -> PreparedRevisionBundle:
        prepared = self._transition_coordinator.create_prepared_driver_revision(
            store_id,
            ref,
            operation_id=operation_id,
            binding=binding,
            result=result,
            driver_id=driver_id,
            driver_version=driver_version,
            parents=parents,
            ingress_kind=ingress_kind,
            relationship_requirements=relationship_requirements,
            reduction_batch=reduction_batch,
            message=message,
        )
        return PreparedRevisionBundle(
            head=prepared.head,
            ref=prepared.ref,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=prepared.preparation,
        )

    def create_prepared_json_candidate_bundle(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        candidate_id: str = "primary",
        payload: dict[str, Any],
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        semantic_op: str = "json-revision",
        driver: TransitionKernelDriver | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        message: str | None = None,
    ) -> PreparedCandidateBundle:
        prepared = self._transition_coordinator.create_prepared_json_candidate(
            store_id,
            operation_id=operation_id,
            binding=binding,
            candidate_id=candidate_id,
            payload=payload,
            parents=parents,
            ingress_kind=ingress_kind,
            semantic_op=semantic_op,
            driver=driver,
            relationship_requirements=relationship_requirements,
            message=message,
        )
        return PreparedCandidateBundle(
            candidate=prepared.candidate,
            candidate_commit=prepared.candidate_commit,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=prepared.preparation,
        )

    def create_prepared_driver_candidate_bundle(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        driver_id: str,
        driver_version: str,
        candidate_id: str = "primary",
        parents: tuple[str | pygit2.Oid, ...] = (),
        ingress_kind: str = "command",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        reduction_batch: ReductionBatch | None = None,
        message: str | None = None,
    ) -> PreparedCandidateBundle:
        prepared = self._transition_coordinator.create_prepared_driver_candidate(
            store_id,
            operation_id=operation_id,
            binding=binding,
            result=result,
            driver_id=driver_id,
            driver_version=driver_version,
            candidate_id=candidate_id,
            parents=parents,
            ingress_kind=ingress_kind,
            relationship_requirements=relationship_requirements,
            reduction_batch=reduction_batch,
            message=message,
        )
        return PreparedCandidateBundle(
            candidate=prepared.candidate,
            candidate_commit=prepared.candidate_commit,
            transition=prepared.transition,
            plan=prepared.plan,
            preparation=prepared.preparation,
        )

    def dispatch_driver_ingress(
        self,
        driver: SubstrateDriver,
        context: DriverContext,
        request: IngressRequest,
    ) -> DriverIngressResult:
        return self._transition_coordinator.dispatch(driver, context, request)

    def validate_active_surface_result(
        self,
        driver: SubstrateDriver,
        context: DriverContext,
        result: DriverIngressResult,
    ) -> None:
        self._transition_coordinator.validate_active_surface_result(driver, context, result)

    def persist_driver_evidence_only(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        ingress_kind: str,
        driver_id: str,
        driver_version: str,
        envelope_id: str = "primary",
    ) -> CoordinatorEvidenceOnlyIngress:
        return self._transition_coordinator.persist_driver_evidence_only(
            store_id,
            operation_id=operation_id,
            binding=binding,
            result=result,
            ingress_kind=ingress_kind,
            driver_id=driver_id,
            driver_version=driver_version,
            envelope_id=envelope_id,
        )

    def persist_driver_diagnostics(
        self,
        store_id: str,
        *,
        operation_id: str,
        binding: str,
        result: DriverIngressResult,
        ingress_kind: str,
        driver_id: str,
        driver_version: str,
        envelope_id: str = "diagnostics",
    ) -> CoordinatorEvidenceOnlyIngress:
        return self._transition_coordinator.persist_driver_diagnostics(
            store_id,
            operation_id=operation_id,
            binding=binding,
            result=result,
            ingress_kind=ingress_kind,
            driver_id=driver_id,
            driver_version=driver_version,
            envelope_id=envelope_id,
        )

    def build_reduction_batch(
        self,
        evidence_refs: tuple[EvidenceRef, ...],
        *,
        citation_prefix: str = "evidence",
    ) -> ReductionBatch:
        return self._transition_coordinator.build_reduction_batch(
            evidence_refs,
            citation_prefix=citation_prefix,
        )

    def substrate_head(
        self,
        store_id: str,
        *,
        binding: str,
        head: str,
        role: str,
        store_scope: str = "resource",
    ) -> SubstrateHead:
        return self.store(store_id).substrate_head(
            binding=binding,
            head=head,
            role=role,
            store_scope=store_scope,
        )

    def create_existing_head_selection_evidence(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        selection_kind: Literal["bootstrap", "checkpoint", "import", "revert"],
        selected_from: str | None = None,
        mechanism: str | None = None,
        correlation_id: str | None = None,
    ) -> EvidenceRef:
        """Persist operation-local evidence for selecting an existing prepared substrate head."""
        return self._transition_coordinator.create_existing_head_selection_evidence(
            operation_id=operation_id,
            head=head,
            selection_kind=selection_kind,
            selected_from=selected_from,
            mechanism=mechanism,
            correlation_id=correlation_id,
        )

    def plan_existing_head_selection(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        selection_kind: Literal["bootstrap", "checkpoint", "import", "revert"],
        selected_from: str | None = None,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
        mechanism: str | None = None,
        correlation_id: str | None = None,
    ) -> SelectionRequirementPlan:
        """Validate and plan coordinator-owned selection of an existing prepared head."""
        return self._transition_coordinator.plan_existing_head_selection(
            operation_id=operation_id,
            head=head,
            selection_kind=selection_kind,
            selected_from=selected_from,
            relationship_requirements=relationship_requirements,
            retention_policy_requirements=retention_policy_requirements,
            selection_policy_digest=selection_policy_digest,
            mechanism=mechanism,
            correlation_id=correlation_id,
        )

    def plan_unchanged_selection(
        self,
        *,
        operation_id: str,
        head: SubstrateHead,
        input_world_oid: str,
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
    ) -> SelectionRequirementPlan:
        """Validate and plan selection of an input-world head without a new candidate."""
        return self._transition_coordinator.plan_unchanged_selection(
            operation_id=operation_id,
            head=head,
            input_world_oid=input_world_oid,
            relationship_requirements=relationship_requirements,
            retention_policy_requirements=retention_policy_requirements,
            selection_policy_digest=selection_policy_digest,
        )

    def plan_candidate_selection(
        self,
        *,
        operation_id: str,
        selection: CandidateSelection,
        selection_kind: Literal["new-candidate", "child-produced"] | None = None,
        producer_operation_id: str | None = None,
        producer_world_oid: str | None = None,
        role: str = "",
        relationship_requirements: tuple[RelationshipRequirement, ...] = (),
        retention_policy_requirements: tuple[RetentionPolicyRequirement, ...] = (),
        selection_policy_digest: str | None = None,
    ) -> CandidateSelectionPlan:
        """Validate and plan coordinator-owned selection of a prepared candidate."""
        return self._transition_coordinator.plan_candidate_selection(
            operation_id=operation_id,
            selection=selection,
            selection_kind=selection_kind,
            producer_operation_id=producer_operation_id,
            producer_world_oid=producer_world_oid,
            role=role,
            relationship_requirements=relationship_requirements,
            retention_policy_requirements=retention_policy_requirements,
            selection_policy_digest=selection_policy_digest,
        )

    def _read_world_ref_payload(self, head: SubstrateHead) -> WorldRefPayload:
        return self._transition_coordinator.read_world_ref_payload(head)

    def create_world_from_prepared(
        self,
        prepared: PreparedWorldOperation,
        *,
        include_gitlinks: bool = False,
    ) -> str:
        prepared.require_candidate_tuples()
        self._validate_prepared_operation_admission(prepared)
        finalized = prepared.finalize()
        return self._world_store.create_world_commit(
            snapshot=finalized.snapshot,
            transition=finalized.transition,
            operation_final=finalized.operation_final,
            parents=finalized.parents,
            locator_hints=self.locator_hints(),
            include_gitlinks=include_gitlinks,
        )

    def create_unsafe_world(
        self,
        *,
        snapshot: WorldSnapshot,
        transition: Mapping[str, Any],
        operation_final: Mapping[str, Any] | OperationFinalRecord,
        parents: tuple[str | pygit2.Oid, ...] = (),
        include_gitlinks: bool = False,
    ) -> str:
        """Create a world commit from caller-assembled evidence.

        This is test/migration scaffolding for low-level storage scenarios. New
        publication paths should use ``create_world_from_prepared`` so the
        operation-final evidence is derived from the prepared operation tuple.
        """
        return self._world_store.create_world_commit(
            snapshot=snapshot,
            transition=transition,
            operation_final=operation_final,
            parents=parents,
            locator_hints=self.locator_hints(),
            include_gitlinks=include_gitlinks,
        )

    def read_world(self, ref_or_oid: str) -> WorldCommit:
        if ref_or_oid.startswith("refs/"):
            target = self._world_store.repo.references[ref_or_oid].target
            return self._world_store.read_world_commit(str(target))
        return self._world_store.read_world_commit(ref_or_oid)

    def resolve_selected_head_candidate_provenance(
        self,
        world_oid: str,
        *,
        binding: str,
    ) -> SelectedHeadCandidateProvenance:
        """Resolve candidate custody for the selected head of one binding."""
        selected_world = self.read_world(world_oid)
        try:
            selected_head = selected_world.snapshot.head_for(binding)
        except KeyError as exc:
            raise InvalidRepositoryStateError(
                f"world {selected_world.oid!r} has no selected binding {binding!r}",
            ) from exc

        for lineage_world_oid in self._input_world_lineage(selected_world.oid):
            lineage_world = self._world_store.read_world_commit(lineage_world_oid)
            try:
                lineage_head = lineage_world.snapshot.head_for(binding)
            except KeyError:
                continue
            if not _same_selected_head(lineage_head, selected_head):
                continue
            outcome = _selected_candidate_outcome_for_head(lineage_world, selected_head)
            if outcome is None:
                continue
            operation_id = _world_operation_id(lineage_world)
            producer_operation_id = _candidate_outcome_producer_operation_id(lineage_world, outcome)
            candidate_id = _candidate_outcome_candidate_id(outcome)
            candidate_tuple = self._candidate_tuple_for_selected_head(
                operation_id=operation_id,
                producer_operation_id=producer_operation_id,
                candidate_id=candidate_id,
                head=selected_head,
            )
            candidate = candidate_tuple.candidate
            self.store(candidate.store_id).validate_candidate_ref(
                operation_id=candidate.operation_id,
                binding=candidate.binding,
                candidate_id=candidate.candidate_id,
                expected_head=candidate.head,
            )
            return SelectedHeadCandidateProvenance(
                world_oid=selected_world.oid,
                producer_world_oid=lineage_world.oid,
                producer_operation_id=producer_operation_id,
                selected_head=selected_head,
                candidate_tuple=candidate_tuple,
            )

        raise InvalidRepositoryStateError(
            "selected head has no full candidate custody in input-world lineage: "
            f"{binding}@{selected_head.store_id}/{selected_head.resource_id}:{selected_head.head}",
        )

    # --- Operation-journal delegation shims (V2.1) ---------------------------------
    # The journal state machine moved to OperationJournalController (self._journal).
    # These shims preserve every `manager.<journal_method>` call site and monkeypatch
    # target; internal callers stay on `self.<method>` so interception is preserved.

    def open_operation_journal(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        target_ref: str,
        input_world_oid: str | None,
        parent_operation_id: str | None = None,
        causal_links: Mapping[str, object] | None = None,
    ) -> OperationJournalEntry:
        return self._journal.open_operation_journal(
            operation_id=operation_id,
            operation_kind=operation_kind,
            target_ref=target_ref,
            input_world_oid=input_world_oid,
            parent_operation_id=parent_operation_id,
            causal_links=causal_links,
        )

    def _open_journal_index(self) -> OpenOperationJournalIndex:
        return self._journal._open_journal_index()

    def _scan_open_operation_journal_refs(self) -> frozenset[str]:
        return self._journal._scan_open_operation_journal_refs()

    def verify_open_operation_journal_index(self) -> Health:
        return self._journal.verify_open_operation_journal_index()

    def rebuild_open_operation_journal_index(self) -> None:
        self._journal.rebuild_open_operation_journal_index()

    def read_open_operation_journal_index(self) -> frozenset[str] | None:
        return self._journal.read_open_operation_journal_index()

    def open_operation_journal_index_corruption(self) -> str | None:
        return self._journal.open_operation_journal_index_corruption()

    def record_operation_prepared(
        self,
        operation_id: str,
        *,
        prepared: PreparedWorldOperation,
    ) -> OperationJournalEntry:
        return self._journal.record_operation_prepared(operation_id, prepared=prepared)

    def record_operation_finalized(
        self,
        operation_id: str,
    ) -> OperationJournalEntry:
        return self._journal.record_operation_finalized(operation_id)

    def _validate_prepared_operation_admission(self, prepared: PreparedWorldOperation) -> None:
        """Validate coordinator-owned evidence before journaling or committing a prepared world."""
        self._transition_coordinator.validate_prepared_operation_admission(prepared)

    def _candidate_tuple_for_selected_head(
        self,
        *,
        operation_id: str,
        producer_operation_id: str,
        candidate_id: str,
        head: SubstrateHead,
    ) -> PreparedCandidateTupleRecord:
        return self._journal._candidate_tuple_for_selected_head(
            operation_id=operation_id,
            producer_operation_id=producer_operation_id,
            candidate_id=candidate_id,
            head=head,
        )

    def record_operation_world_committed(
        self,
        operation_id: str,
        *,
        world_oid: str,
    ) -> OperationJournalEntry:
        return self._journal.record_operation_world_committed(operation_id, world_oid=world_oid)

    def close_operation_journal(
        self,
        operation_id: str,
        *,
        world_oid: str,
    ) -> OperationJournalEntry:
        return self._journal.close_operation_journal(operation_id, world_oid=world_oid)

    def fail_operation_journal(self, operation_id: str, *, error: str) -> OperationJournalEntry:
        return self._journal.fail_operation_journal(operation_id, error=error)

    def archive_operation_journal(self, operation_id: str, *, error: str | None = None) -> OperationJournalEntry:
        return self._journal.archive_operation_journal(operation_id, error=error)

    def read_operation_journal(self, operation_id: str, *, family: str = "open") -> OperationJournalHistory:
        return self._journal.read_operation_journal(operation_id, family=family)

    def list_operation_journals(self, *, family: str | None = None) -> tuple[OperationJournalSummary, ...]:
        return self._journal.list_operation_journals(family=family)

    def fsck_operation_journal(self, operation_id: str, *, family: str = "open") -> OperationJournalFsckReport:
        return self._journal.fsck_operation_journal(operation_id, family=family)

    def fsck_operation_journals(self) -> OperationJournalsFsckReport:
        """Store-global integrity scan over every v2-shaped operation-journal ref.

        The canonical, explicit, off-hot-path surface for terminal-journal integrity: open-only
        admission no longer *blocks* on corrupt non-`open` journals, so this is where their
        corruption is *detected*. Enumerates via the ref-walk inventory probe (``family=None``),
        which **preserves** present-invalid refs — unlike ``OperationJournalStore.list()``, which
        silently skips refs that fail to parse (the exact corruption being moved off admission).
        For each ref:

        * present-invalid (unreadable, unsupported/unknown family, identity mismatch, corrupt
          chain) → report its issues directly; do **not** attempt targeted fsck, because
          ``operation_id`` / ``family`` may be absent or untrustworthy in precisely these cases;
        * valid + known family + usable operation id → run the deeper targeted
          :meth:`fsck_operation_journal` and fold its issues in.

        Store-global, so it is its OWN entry point — deliberately NOT hung on per-world
        ``fsck_world(mode="deep")``, which reports a single world's integrity.
        """
        from vcs_core._operation_journal_inventory import probe_operation_journals

        issues: list[StructuredIssue] = []
        items = probe_operation_journals(self._world_store.repo)  # family=None: all v2 refs; invalid preserved
        for item in items:
            operation_id = _usable_journal_operation_id(item.fields)
            if item.health.validity == "invalid":
                # This surface recovers nothing — it is the diagnostic home for corruption moved
                # off admission. Override the probe's generic "recover this journal" hint with the
                # diagnostic-only framing, rather than inheriting a hint that implies recoverability.
                issues.extend(
                    _issue(
                        issue.code,
                        issue.message,
                        operation_id=operation_id,
                        ref=issue.locator or item.locator,
                        recovery_hint=_TERMINAL_JOURNAL_DIAGNOSTIC_HINT,
                    )
                    for issue in item.issues
                )
                continue
            family = item.fields.get("family")
            if isinstance(family, str) and operation_id is not None:
                issues.extend(self.fsck_operation_journal(operation_id, family=family).issue_details)
        # Deep, off-hot-path drift check of the open-journal accelerator against the authoritative
        # open refs. Atomic co-write precludes phantoms in the normal writer model, but an
        # out-of-model writer (manual/private-ref edit) can leave the index STALE, so the store-wide
        # journal fsck is where that drift surfaces (mirrors the lease verify in fsck_world).
        index_health = self.verify_open_operation_journal_index()
        if index_health.status in ("stale", "corrupt"):
            issues.append(
                _issue(
                    f"open_operation_journal_index_{index_health.status}",
                    f"open-operation-journal accelerator is {index_health.status} versus the authoritative "
                    f"open journal refs: {index_health.detail}",
                    store_id=self._world_store.world_store_id,
                    ref=world_open_operation_journal_index_ref(self._world_store.world_store_id),
                    recovery_hint=(
                        "Run rebuild_open_operation_journal_index() to reconcile the accelerator; "
                        "the authority is unaffected."
                    ),
                ),
            )
        return OperationJournalsFsckReport(scanned=len(items), issue_details=tuple(issues))

    def advance_world_ref(
        self,
        *,
        ref: str,
        world_oid: str,
        input_world_oid: str,
        allow_same_resource_alias: bool = False,
        authority_refs: tuple[str, ...] | None = None,
    ) -> bool:
        plan = self.build_advance_publication_plan(
            ref=ref,
            world_oid=world_oid,
            expected_oid=input_world_oid,
            input_world_oid=input_world_oid,
            allow_same_resource_alias=allow_same_resource_alias,
            authority_refs=authority_refs,
        )
        prepared = self.prepare_publication(plan)
        published = self.advance_publication(prepared)
        self.complete_publication(prepared)
        return published

    def fork_world_ref(
        self,
        *,
        ref: str,
        world_oid: str,
        forked_from_ref: str,
        forked_from_world_oid: str,
        allow_same_resource_alias: bool = False,
    ) -> bool:
        if _current_ref_target(self._world_store.repo, ref) is not None:
            return False
        forked_from_target = _current_ref_target(self._world_store.repo, forked_from_ref)
        if forked_from_target != forked_from_world_oid:
            raise InvalidRepositoryStateError("fork origin authority ref does not target forked_from_world_oid")
        world = self._world_store.read_world_commit(world_oid)
        if world_oid != forked_from_world_oid:
            _validate_advance_basis(world, input_world_oid=forked_from_world_oid)
        # Trust-by-default (260623-0640-plan.md, Part A): prior-lineage retention re-validation
        # is off the fork-publish hot path too; the immediate fork-origin shape check above
        # (_validate_advance_basis) stays. Deep lineage integrity runs in fsck_world(deep) (Part B).
        closure = self.validate_publish_closure(
            world_oid,
            authority_refs=(forked_from_ref,),
            allow_same_resource_alias=allow_same_resource_alias,
        )
        lease_refs = self._write_publication_leases((ref,), world)
        retained_refs = self.pin_world_closure(closure)
        self.write_world_retention_receipt(
            authority_ref=ref,
            world_oid=world_oid,
            closure=closure,
            retained_refs=retained_refs,
        )
        self.write_world_fork_origin_receipt(
            authority_ref=ref,
            first_world_oid=world_oid,
            forked_from_authority_ref=forked_from_ref,
            forked_from_world_oid=forked_from_world_oid,
        )
        published = self._world_store._publish_ref_unchecked(ref, world_oid, expected_oid=None)
        self._release_publication_leases(lease_refs, world_oid=world_oid)
        return published

    def fork_origin_world_oid(self, authority_ref: str, *, expected_forked_from_ref: str | None = None) -> str:
        """Return the parent world basis recorded when an authority ref was forked."""
        receipt = _read_world_fork_origin_receipt(self._world_store.repo, world_fork_origin_receipt_ref(authority_ref))
        if receipt.authority_ref != authority_ref:
            raise InvalidRepositoryStateError("fork origin receipt authority_ref disagrees with ref")
        if receipt.world_store_id != self._world_store.world_store_id:
            raise InvalidRepositoryStateError("fork origin receipt world_store_id disagrees with coordinator")
        if expected_forked_from_ref is not None and receipt.forked_from_authority_ref != expected_forked_from_ref:
            raise InvalidRepositoryStateError("fork origin receipt parent authority disagrees with retained handoff")
        return receipt.forked_from_world_oid

    def _authority_lineage_segments(
        self,
        authority_ref: str,
        oid: str,
    ) -> _AuthorityLineageSegments:
        fork_origin = _read_optional_world_fork_origin_receipt(
            self._world_store.repo,
            world_fork_origin_receipt_ref(authority_ref),
        )
        if fork_origin is None:
            return _AuthorityLineageSegments(local_world_oids=self._input_world_lineage(oid))
        if fork_origin.authority_ref != authority_ref:
            raise InvalidRepositoryStateError("fork origin receipt authority_ref disagrees with ref")
        if fork_origin.world_store_id != self._world_store.world_store_id:
            raise InvalidRepositoryStateError("fork origin receipt world_store_id disagrees with coordinator")
        lineage = self._input_world_lineage(oid)
        try:
            fork_base_index = lineage.index(fork_origin.forked_from_world_oid)
        except ValueError:
            return _AuthorityLineageSegments(
                local_world_oids=(),
                fork_origin=fork_origin,
                corrupt_fork_origin="fork origin forked_from_world_oid is not in child input lineage",
            )
        local_world_oids = tuple(lineage[:fork_base_index])
        if fork_origin.first_world_oid != fork_origin.forked_from_world_oid and (
            fork_origin.first_world_oid not in local_world_oids
        ):
            raise InvalidRepositoryStateError("fork origin first_world_oid is not in local authority lineage")
        return _AuthorityLineageSegments(local_world_oids=local_world_oids, fork_origin=fork_origin)

    def _input_world_lineage(self, oid: str) -> tuple[str, ...]:
        lineage: list[str] = []
        seen: set[str] = set()
        current_oid: str | None = oid
        while current_oid is not None:
            if current_oid in seen:
                raise InvalidRepositoryStateError("authority input_world lineage contains a cycle")
            seen.add(current_oid)
            lineage.append(current_oid)
            world = self._world_store.read_world_commit(current_oid)
            input_world = world.transition.get("input_world")
            if input_world is None:
                current_oid = None
                continue
            if not isinstance(input_world, str) or not input_world:
                raise InvalidRepositoryStateError("authority input_world lineage contains an invalid input_world")
            current_oid = input_world
        return tuple(lineage)


def _specs_by_id(stores: tuple[SubstrateStoreSpec, ...]) -> dict[str, SubstrateStoreSpec]:
    specs: dict[str, SubstrateStoreSpec] = {}
    for spec in stores:
        store_id = spec.identity.store_id
        if store_id in specs:
            raise ValueError(f"duplicate substrate store spec for {store_id!r}")
        specs[store_id] = spec
    return specs


def _installation_config_path(root: Path) -> Path:
    return root / "world-stores.json"


def _write_installation_config(
    root: Path,
    *,
    world_store_id: str,
    specs_by_id: Mapping[str, SubstrateStoreSpec],
) -> None:
    _installation_config_path(root).write_bytes(
        compact_json_bytes(_installation_config(world_store_id=world_store_id, specs_by_id=specs_by_id)) + b"\n",
    )


def _read_installation_config(root: Path) -> dict[str, object]:
    config_path = _installation_config_path(root)
    if not config_path.exists():
        raise InvalidRepositoryStateError(f"world storage installation config is missing: {config_path}")
    current = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise InvalidRepositoryStateError("world-stores.json must contain a JSON object")
    return current


def _validate_installation_config(
    root: Path,
    *,
    world_store_id: str,
    specs_by_id: Mapping[str, SubstrateStoreSpec],
) -> None:
    current = _read_installation_config(root)
    if current.get("schema") != INSTALLATION_SCHEMA:
        raise InvalidRepositoryStateError(f"unsupported world storage installation schema: {current.get('schema')!r}")
    if current.get("world_store_id") != world_store_id:
        raise InvalidRepositoryStateError("world storage installation world_store_id mismatch")
    current_specs = _store_specs_from_config(current)
    if set(current_specs) != set(specs_by_id):
        raise InvalidRepositoryStateError("world storage installation store set mismatch")
    for store_id, spec in specs_by_id.items():
        current_spec = current_specs[store_id]
        if current_spec.identity != spec.identity:
            raise InvalidRepositoryStateError(f"world storage installation identity mismatch for {store_id!r}")
        if current_spec.locator != spec.locator:
            raise InvalidRepositoryStateError(f"world storage installation locator mismatch for {store_id!r}")
    expected = _installation_config(world_store_id=world_store_id, specs_by_id=specs_by_id)
    if current != expected:
        raise InvalidRepositoryStateError("world storage installation config mismatch")


def _installation_config(
    *,
    world_store_id: str,
    specs_by_id: Mapping[str, SubstrateStoreSpec],
) -> dict[str, object]:
    return {
        "schema": INSTALLATION_SCHEMA,
        "world_store_id": world_store_id,
        "coordinator": DEFAULT_COORDINATOR_LOCATOR,
        "stores": {store_id: spec.to_json() for store_id, spec in sorted(specs_by_id.items())},
    }


def _store_specs_from_config(current: Mapping[str, object]) -> dict[str, SubstrateStoreSpec]:
    raw_stores = current.get("stores")
    if not isinstance(raw_stores, dict):
        raise InvalidRepositoryStateError("world storage installation stores must be an object")
    specs: dict[str, SubstrateStoreSpec] = {}
    for store_id, raw_spec in raw_stores.items():
        if not isinstance(store_id, str) or not isinstance(raw_spec, dict):
            raise InvalidRepositoryStateError("world storage installation stores must map strings to objects")
        specs[store_id] = SubstrateStoreSpec.from_json(raw_spec)
    return specs


def _same_selected_head(left: SubstrateHead, right: SubstrateHead) -> bool:
    return (
        left.binding == right.binding
        and left.store_id == right.store_id
        and left.resource_id == right.resource_id
        and left.head == right.head
    )


def _selected_candidate_outcome_for_head(
    world: WorldCommit,
    head: SubstrateHead,
) -> dict[str, object] | None:
    matches: list[dict[str, object]] = []
    for outcome in _object_list(world.operation_final.get("candidate_outcomes"), "operation-final candidate_outcomes"):
        if outcome.get("binding") != head.binding or outcome.get("outcome") != "selected":
            continue
        if outcome.get("candidate") != head.head:
            continue
        store_id = outcome.get("store_id")
        if store_id is not None and store_id != head.store_id:
            raise InvalidRepositoryStateError("selected candidate outcome store_id disagrees with selected head")
        resource_id = outcome.get("resource_id")
        if resource_id is not None and resource_id != head.resource_id:
            raise InvalidRepositoryStateError("selected candidate outcome resource_id disagrees with selected head")
        matches.append(outcome)
    if len(matches) > 1:
        raise InvalidRepositoryStateError("operation-final contains duplicate selected candidate outcomes")
    return matches[0] if matches else None


def _candidate_outcome_producer_operation_id(world: WorldCommit, outcome: Mapping[str, object]) -> str:
    producer_operation_id = outcome.get("producer_operation_id")
    if producer_operation_id is None:
        return _world_operation_id(world)
    if not isinstance(producer_operation_id, str) or not producer_operation_id:
        raise InvalidRepositoryStateError("candidate outcome producer_operation_id must be a non-empty string")
    return producer_operation_id


def _candidate_outcome_candidate_id(outcome: Mapping[str, object]) -> str:
    candidate_id = outcome.get("candidate_id", "primary")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise InvalidRepositoryStateError("candidate outcome candidate_id must be a non-empty string")
    return candidate_id


_TERMINAL_JOURNAL_DIAGNOSTIC_HINT = (
    "Diagnostic only: corrupt or unknown-family operation-journal refs no longer block admission. "
    "Inspect via `vcs-core inspect --domain operation_journal`; they are not auto-recoverable."
)


def _usable_journal_operation_id(fields: dict[str, object]) -> str | None:
    """First trustworthy operation id on a journal inventory item, or None if none is usable."""
    for key in ("operation_id", "payload_operation_id", "locator_operation_id"):
        value = fields.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _read_optional_world_fork_origin_receipt(repo: pygit2.Repository, ref: str) -> _ForkOriginReceipt | None:
    try:
        return _read_world_fork_origin_receipt(repo, ref)
    except KeyError:
        return None


def _validate_relative_locator(locator: str) -> None:
    path = Path(locator)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"substrate store locator must be a relative path without traversal: {locator!r}")
