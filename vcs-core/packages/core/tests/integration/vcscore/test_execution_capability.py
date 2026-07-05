"""PD1+PD2: the execution-mechanism capability surface and the reversible wrap.

PD1: execution authority is an opt-in capability — only an ``ExecutionBoundDriver``
dispatching one of its declared ``execution_commands`` receives a per-run
``ExecutionCapability``, and only through the dispatch call. PD2: that dispatch
runs inside the reversible-transaction wrap — fork isolated (always), merge on
success, discard on failure — with the two pre-land gates from the execplan §5:

- gate (i): the wrap's merge routes through the existing lifecycle seam — the
  merge hook fires, and the captured delta surfaces as recorded effects via
  ``prepare_merge`` (the seam check-at-commit supervision attaches to; the
  merge-time *surface refusal* cell itself is parked in core — see the seam
  test's docstring);
- gate (ii): an orphaned in-flight reversible run is never auto-merged on
  recovery, and the salvage path is inspect-the-archive (operation-journal
  entry + archived refs survive).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
import vcs_core._vcscore_lifecycle as lifecycle
from vcs_core._command_admission import CommandAdmissionError
from vcs_core._command_envelope import AuthorityMergeControl, CommandExecutionOptions
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._execution_capability import (
    ExecutionAuthorityRequired,
    ExecutionBoundDriver,
    ExecutionCapability,
    verify_execution_negotiation,
)
from vcs_core._fork_hints import ForkHints
from vcs_core._lock import release_session_lock
from vcs_core._permission_plan_evidence import permission_plan_digest
from vcs_core._projection_store import SEAL_AND_SELECT_ENV
from vcs_core._schema_errors import SchemaValidationError
from vcs_core._substrate_driver import (
    BaseSubstrateDriver,
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    ParamSpec,
    TransitionDraft,
)
from vcs_core._world_transition_coordinator import dispatch_driver
from vcs_core._world_types import SubstrateStoreIdentity
from vcs_core.store import Store
from vcs_core.substrates import FilesystemSubstrate, MarkerSubstrate
from vcs_core.types import AuthorityExecutionOutcome, ScopeInfo, SealedExecutionOutcome
from vcs_core.vcscore import VcsCore

from ...support.overlays import MockOverlayBackend

_EFFECTIVE_MATCH_DIGEST = "test-effective-match"
_AUTHORITY_SURFACE_PLAN_DIGEST = "test-authority-surface-plan"
_PERMISSION_PLAN_DESCRIPTOR = {
    "schema": "shepherd.permission-plan.v1",
    "fallback": "enforce",
    "assignments": [
        {
            "monitor": "carrier_check_at_commit",
            "timing": "commit",
            "route": "carrier_diff",
            "completeness_basis": "test reversible runtime carrier diff",
            "tamper_basis": "test runtime coordinator",
            "confinement": None,
            "evidence": {
                "effective_match_digest": _EFFECTIVE_MATCH_DIGEST,
                "authority_surface_plan_digest": _AUTHORITY_SURFACE_PLAN_DIGEST,
            },
        }
    ],
}
_PERMISSION_PLAN_DIGEST = permission_plan_digest(_PERMISSION_PLAN_DESCRIPTOR)


def _result(context: DriverContext, request: CommandRequest, *, reached: str) -> DriverIngressResult:
    return DriverIngressResult(
        transitions=(
            TransitionDraft(
                transition_id="primary",
                semantic_op="execute",
                payload={
                    "schema": "test/run-probe/v0",
                    "reached": reached,
                    "command": request.command,
                    "operation_id": context.operation_id,
                },
                observation_ids=(),
                base_heads=context.base_heads,
                materialization_class="noop",
            ),
        ),
    )


def _authority_options(outcome: Literal["allowed", "denied", "refused"] = "allowed") -> CommandExecutionOptions:
    from vcs_core._authority import AuthorityDecision

    def decide(request: object) -> AuthorityDecision:
        return AuthorityDecision(
            outcome=outcome,
            reason_code=f"test_{outcome}",
            request_id=getattr(request, "request_id", None),
        )

    return CommandExecutionOptions(
        success_disposition="authority_merge",
        authority_merge=AuthorityMergeControl(
            binding_roots={"workspace": ""},
            decide=decide,
            effective_match_digest=_EFFECTIVE_MATCH_DIGEST,
            authority_surface_plan_digest=_AUTHORITY_SURFACE_PLAN_DIGEST,
            permission_plan_digest=_PERMISSION_PLAN_DIGEST,
            permission_plan_descriptor=_PERMISSION_PLAN_DESCRIPTOR,
            authority_context={
                "schema": "test.runtime-authority-context.v1",
                "source": "test_execution_capability",
            },
        ),
    )


def _raising_authority_options() -> CommandExecutionOptions:
    def decide(request: object) -> object:
        del request
        raise RuntimeError("simulated authority provider failure")

    return CommandExecutionOptions(
        success_disposition="authority_merge",
        authority_merge=AuthorityMergeControl(
            binding_roots={"workspace": ""},
            decide=decide,
            effective_match_digest=_EFFECTIVE_MATCH_DIGEST,
            authority_surface_plan_digest=_AUTHORITY_SURFACE_PLAN_DIGEST,
            permission_plan_digest=_PERMISSION_PLAN_DIGEST,
            permission_plan_descriptor=_PERMISSION_PLAN_DESCRIPTOR,
        ),
    )


@dataclass(frozen=True)
class _PlainDriver(BaseSubstrateDriver):
    """Pure-data SPI driver: no prepare_bound, structurally no execution authority."""

    store_id: str = "store_plain"
    binding: str = "plain"
    role: str = "test.PlainDriver"
    driver_id: str = "test.plain_driver"
    driver_version: str = "v0.1"
    seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            accepts=frozenset({CommandRequest}),
            selectable=False,
            materializable=False,
            journal_only=True,
        )

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands={
                "ping": CommandSpec(description="Echo.", params={"message": ParamSpec(type="str", required=False)})
            },
        )

    def prepare(self, context: DriverContext, request: Any) -> DriverIngressResult:
        self.seen.append({"entry": "prepare", "command": request.command})
        return _result(context, request, reached="prepare")


@dataclass(frozen=True)
class _RunDriver(BaseSubstrateDriver):
    """Execution-bound driver: ``run`` carries execution; ``list`` does not."""

    store_id: str = "store_runprobe"
    binding: str = "runprobe"
    role: str = "test.RunDriver"
    driver_id: str = "test.run_driver"
    driver_version: str = "v0.1"
    backend: MockOverlayBackend | None = None
    admission_error: Exception | None = None
    seen: list[dict[str, Any]] = field(default_factory=list)
    admissions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            accepts=frozenset({CommandRequest}),
            selectable=False,
            materializable=False,
            journal_only=True,
        )

    @property
    def execution_commands(self) -> frozenset[str]:
        return frozenset({"run"})

    def describe(self) -> DriverSchema:
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands={
                "run": CommandSpec(
                    description="Run the probe body in the reversible wrap.",
                    params={"behavior": ParamSpec(type="str", required=False)},
                ),
                "list": CommandSpec(description="Listing carries no execution authority.", params={}),
            },
        )

    def prepare(self, context: DriverContext, request: Any) -> DriverIngressResult:
        if request.command in self.execution_commands:
            # The negotiation rule (PD4): never run an execution command
            # without execution authority — refuse before touching params.
            raise ExecutionAuthorityRequired(
                f"{self.binding}.{request.command} requires execution authority; refusing to run real."
            )
        self.seen.append({"entry": "prepare", "command": request.command})
        return _result(context, request, reached="prepare")

    def prepare_bound(
        self,
        context: DriverContext,
        request: Any,
        execution: ExecutionCapability,
    ) -> DriverIngressResult:
        behavior = request.params.get("behavior", "noop")
        self.seen.append(
            {
                "entry": "prepare_bound",
                "command": request.command,
                "behavior": behavior,
                "isolation": execution.isolation,
                "scope_name": execution.identity.scope_name,
                "working_path": str(execution.working_path),
            }
        )
        if behavior in {"write", "write-then-fail"}:
            assert self.backend is not None
            self.backend.write_file(execution.identity.scope_name, "run-artifact.txt", b"from the body\n")
        if behavior == "write-then-fail":
            raise RuntimeError("body failed after a write")
        return _result(context, request, reached="prepare_bound")

    def validate_command_invocation(
        self,
        command: str,
        scope: ScopeInfo,
        *,
        params: Mapping[str, Any],
    ) -> None:
        self.admissions.append(
            {
                "entry": "admit",
                "command": command,
                "scope_name": scope.name,
                "params": dict(params),
            }
        )
        if self.admission_error is not None:
            raise self.admission_error


@dataclass(frozen=True)
class _SelectableRunDriver(_RunDriver):
    binding: str = "selectable"

    @property
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            accepts=frozenset({CommandRequest}),
            selectable=True,
            materializable=False,
            journal_only=False,
        )


def _make_env(
    root: Path,
    *,
    isolation_capable: bool = True,
    admission_error: Exception | None = None,
) -> tuple[VcsCore, _RunDriver, MockOverlayBackend | None]:
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    from vcs_core._substrate_runtime import build_builtin_substrate_context

    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    backend = MockOverlayBackend() if isolation_capable else None
    driver = _RunDriver(backend=backend, admission_error=admission_error)
    filesystem = FilesystemSubstrate(ctx, backend=backend) if backend is not None else FilesystemSubstrate(ctx)
    mg = VcsCore(str(root), substrates=[MarkerSubstrate(ctx), filesystem, driver], store=store)
    mg.activate()
    return mg, driver, backend


@pytest.fixture
def env(tmp_path: Path) -> tuple[VcsCore, _RunDriver, MockOverlayBackend]:
    mg, driver, backend = _make_env(tmp_path / "ws")
    assert backend is not None
    yield mg, driver, backend
    mg.deactivate()


# --- PD1: least authority is structural --------------------------------------


def test_pure_data_driver_dispatches_plain_without_authority(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    from vcs_core._substrate_runtime import build_builtin_substrate_context

    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    driver = _PlainDriver()
    mg = VcsCore(str(root), substrates=[MarkerSubstrate(ctx), FilesystemSubstrate(ctx), driver], store=store)
    mg.activate()
    try:
        assert not isinstance(driver, ExecutionBoundDriver)
        outcome = mg.execute_recorded("plain", "ping", scope=mg.ground, message="hello")
        assert outcome.value.transitions[0].payload["reached"] == "prepare"
        assert driver.seen == [{"entry": "prepare", "command": "ping"}]
    finally:
        mg.deactivate()


def test_execution_authority_refused_for_unbound_driver() -> None:
    driver = _PlainDriver()
    context = DriverContext(
        operation_id="t-op",
        binding="plain",
        role="test.PlainDriver",
        store_identity=SubstrateStoreIdentity(store_id="s", kind="test", resource_id="r"),
        base_heads=(),
    )
    request = CommandRequest(command="ping", params={})
    with pytest.raises(TypeError, match="not opted in"):
        dispatch_driver(driver, context, request, execution=object())
    assert driver.seen == []


def test_non_execution_command_of_bound_driver_skips_wrap(env: tuple[VcsCore, _RunDriver, MockOverlayBackend]) -> None:
    mg, driver, backend = env
    outcome = mg.execute_recorded("runprobe", "list", scope=mg.ground)
    assert outcome.value.transitions[0].payload["reached"] == "prepare"
    assert driver.seen == [{"entry": "prepare", "command": "list"}]
    # No scope fork, no carrier traffic: a `list` never clones a workspace.
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []
    assert backend.discarded == []


def test_selectable_driver_refused_at_bridge(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    store = Store(str(root / ".vcscore"))
    from vcs_core._substrate_runtime import build_builtin_substrate_context

    ctx = build_builtin_substrate_context(store, workspace=root, config={})
    backend = MockOverlayBackend()
    driver = _SelectableRunDriver(backend=backend)
    mg = VcsCore(
        str(root), substrates=[MarkerSubstrate(ctx), FilesystemSubstrate(ctx, backend=backend), driver], store=store
    )
    mg.activate()
    try:
        with pytest.raises(ValueError, match="journal-only"):
            mg.execute_recorded("selectable", "run", scope=mg.ground, behavior="noop")
        assert driver.seen == []
    finally:
        mg.deactivate()


# --- PD2: the reversible wrap -------------------------------------------------


def test_ground_scope_mount_path_is_the_real_workspace_not_a_carrier(tmp_path: Path) -> None:
    """The auditable-ground invariant, pinned in the store's owning package (W2b).

    A non-reversible / ground run writes to the *real* working copy so its
    residue is auditable and persists through failure. PR#4's always-on
    copy-carrier floor regressed ``overlay_mount_path_for_scope(ground)`` to a
    carrier layer, silently breaking this — caught only by ``skeleton/tests``,
    which the public cut drops. This pin lives where the contract is owned:
    even with a carrier backend present, ground resolves to the real workspace.
    """
    root = tmp_path / "ws"
    mg, _driver, backend = _make_env(root)
    try:
        assert backend is not None  # a carrier IS present — the regression condition
        ground_mount = mg.overlay_mount_path_for_scope(mg.ground)
        assert ground_mount.resolve() == root.resolve(), (
            f"ground scope must mount the real workspace, never a carrier layer (got {ground_mount!r})"
        )
    finally:
        mg.deactivate()


def test_reversible_run_forks_isolated_and_merges_on_success(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env
    outcome = mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write")
    assert outcome.value.transitions[0].payload["reached"] == "prepare_bound"
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["isolation"] == "isolated"
    assert call["scope_name"].startswith("run-")
    # The run scope's layer was committed into its parent at merge: capture is
    # implicit (the carrier's merge diff IS the capture), nothing left live.
    assert (call["scope_name"], "ground") in backend.committed
    assert call["scope_name"] not in mg._scope_parents


def test_reversible_run_can_seal_on_success_without_advancing_parent_world(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    mg, driver, backend = env
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")
    parent_world_before = mg.world_oid(mg.ground)
    assert parent_world_before is not None

    outcome = mg.execute_recorded(
        "runprobe",
        "run",
        scope=mg.ground,
        behavior="write",
        execution_options=CommandExecutionOptions(success_disposition="seal"),
    )

    assert isinstance(outcome.value, SealedExecutionOutcome)
    assert outcome.value.driver_result.transitions[0].payload["reached"] == "prepare_bound"
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["isolation"] == "isolated"
    assert call["scope_name"].startswith("run-")
    assert backend.committed == []
    assert call["scope_name"] in backend.discarded
    assert call["scope_name"] not in mg._scope_parents
    assert mg.world_oid(mg.ground) == parent_world_before

    seal_result = outcome.value.seal_result
    assert seal_result.scope.name == call["scope_name"]
    assert seal_result.parent == mg.ground
    assert outcome.value.handoff == seal_result.handoff
    assert outcome.value.handoff.binding == "workspace"
    assert "run-artifact.txt" in outcome.value.handoff.changed_paths
    rows = mg.list_retained_outputs(parent=mg.ground, binding="workspace", state="unconsumed")
    assert len(rows) == 1
    assert rows[0].handoff_ref == outcome.value.handoff.handoff_ref
    assert rows[0].output_world_oid == outcome.value.handoff.output_world_oid


def test_reversible_run_can_authority_merge_on_success(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env
    parent_world_before = mg.world_oid(mg.ground)

    outcome = mg.execute_recorded(
        "runprobe",
        "run",
        scope=mg.ground,
        behavior="write",
        execution_options=_authority_options("allowed"),
    )

    assert isinstance(outcome.value, AuthorityExecutionOutcome)
    assert outcome.value.driver_result.transitions[0].payload["reached"] == "prepare_bound"
    assert outcome.value.authority_result.outcome == "allowed"
    assert outcome.value.authority_result.settlement == "merged"
    assert outcome.value.authority_result.parent_world_before == parent_world_before
    assert outcome.value.authority_result.parent_world_after == mg.world_oid(mg.ground)
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["isolation"] == "isolated"
    assert call["scope_name"].startswith("run-")
    assert backend.committed == [(call["scope_name"], "ground")]
    assert backend.discarded == []
    assert call["scope_name"] not in mg._scope_parents


def test_reversible_run_authority_merge_denial_discards_without_adopting(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env

    outcome = mg.execute_recorded(
        "runprobe",
        "run",
        scope=mg.ground,
        behavior="write",
        execution_options=_authority_options("denied"),
    )

    assert isinstance(outcome.value, AuthorityExecutionOutcome)
    assert outcome.value.driver_result.transitions[0].payload["reached"] == "prepare_bound"
    assert outcome.value.authority_result.outcome == "denied"
    assert outcome.value.authority_result.settlement == "discarded"
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["isolation"] == "isolated"
    assert call["scope_name"].startswith("run-")
    assert backend.committed == []
    assert backend.discarded == [call["scope_name"]]
    assert call["scope_name"] not in mg._scope_parents


def test_authority_merge_provider_failure_discards_without_pending_settlement(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env

    with pytest.raises(RuntimeError, match="simulated authority provider failure"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=_raising_authority_options(),
        )

    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert backend.committed == []
    assert call["scope_name"] in backend.discarded
    assert call["scope_name"] not in mg._scope_parents
    assert mg.list_authority_settlement_pending() == ()


def test_reversible_run_discards_on_failure_and_ground_stays_pristine(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env
    with pytest.raises(RuntimeError, match="body failed after a write"):
        mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write-then-fail")
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["scope_name"] in backend.discarded
    assert backend.committed == []
    assert "run-artifact.txt" not in backend.layers.get("ground", {})
    assert call["scope_name"] not in mg._scope_parents


def test_seal_disposition_rejects_non_reversible_run_before_fork_or_body(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env

    with pytest.raises(SchemaValidationError, match="requires a reversible execution-bound run"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=CommandExecutionOptions(
                non_reversible_run=True,
                success_disposition="seal",
            ),
        )

    assert driver.admissions == []
    assert driver.seen == []
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []
    assert backend.discarded == []


def test_authority_merge_disposition_requires_control_before_fork_or_body(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env

    with pytest.raises(SchemaValidationError, match="requires 'authority_merge'"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=CommandExecutionOptions(success_disposition="authority_merge"),
        )

    assert driver.admissions == []
    assert driver.seen == []
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []
    assert backend.discarded == []


def test_authority_merge_disposition_rejects_non_reversible_run_before_fork_or_body(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env
    options = _authority_options("allowed")
    options = CommandExecutionOptions(
        non_reversible_run=True,
        success_disposition=options.success_disposition,
        authority_merge=options.authority_merge,
    )

    with pytest.raises(SchemaValidationError, match="requires a reversible execution-bound run"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=options,
        )

    assert driver.admissions == []
    assert driver.seen == []
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []
    assert backend.discarded == []


def test_authority_merge_disposition_rejected_for_non_execution_command(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env

    with pytest.raises(SchemaValidationError, match="only valid for execution-bound commands"):
        mg.execute_recorded(
            "runprobe",
            "list",
            scope=mg.ground,
            execution_options=_authority_options("allowed"),
        )

    assert driver.admissions == []
    assert driver.seen == []
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []
    assert backend.discarded == []


def test_seal_disposition_feature_gate_refuses_before_fork_or_body(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SEAL_AND_SELECT_ENV, raising=False)
    mg, driver, backend = env

    with pytest.raises(InvalidRepositoryStateError, match=SEAL_AND_SELECT_ENV):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=CommandExecutionOptions(success_disposition="seal"),
        )

    assert driver.admissions == []
    assert driver.seen == []
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []
    assert backend.discarded == []
    assert all(not name.startswith("run-") for name in mg._scope_parents)


def test_seal_disposition_discards_on_body_failure_without_retained_custody(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    mg, driver, backend = env
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")

    with pytest.raises(RuntimeError, match="body failed after a write"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write-then-fail",
            execution_options=CommandExecutionOptions(success_disposition="seal"),
        )

    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["scope_name"] in backend.discarded
    assert backend.committed == []
    assert call["scope_name"] not in mg._scope_parents
    assert mg.list_retained_outputs(parent=mg.ground, binding="workspace") == ()


def test_seal_disposition_discards_on_post_body_seal_failure_without_retained_custody(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEAL_AND_SELECT_ENV, "1")
    mg, driver, backend = env
    mg.exec("filesystem", "write", scope=mg.ground, path="base.txt", content=b"base\n")

    with pytest.raises(InvalidRepositoryStateError, match=r"seal output.*no binding 'missing'"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=CommandExecutionOptions(
                success_disposition="seal",
                seal_output_binding="missing",
            ),
        )

    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["scope_name"] in backend.discarded
    assert backend.committed == []
    assert call["scope_name"] not in mg._scope_parents
    assert mg.list_retained_outputs(parent=mg.ground, binding="workspace") == ()


def test_authority_merge_settlement_failure_leaves_recoverable_pending(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mg, driver, backend = env
    original_record_settlement = lifecycle._record_authority_final_settlement
    fail_next = True

    def fail_first_settlement(*args: object, **kwargs: object) -> None:
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise RuntimeError("simulated runtime authority settlement failure")
        original_record_settlement(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_record_authority_final_settlement", fail_first_settlement)

    with pytest.raises(RuntimeError, match="simulated runtime authority settlement failure"):
        mg.execute_recorded(
            "runprobe",
            "run",
            scope=mg.ground,
            behavior="write",
            execution_options=_authority_options("allowed"),
        )

    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert backend.committed == [(call["scope_name"], "ground")]
    assert call["scope_name"] not in mg._scope_parents
    pending = mg.list_authority_settlement_pending()
    assert len(pending) == 1
    (pending_record,) = mg.authority_settlement_pending_records()
    assert pending_record["settlement_operation_id"] == pending[0]
    authority_context = pending_record["authority_context"]
    assert isinstance(authority_context, dict)
    assert authority_context["schema"] == "test.runtime-authority-context.v1"
    assert authority_context["source"] == "test_execution_capability"
    assert isinstance(authority_context["runtime_operation_id"], str)
    assert authority_context["runtime_operation_id"]
    with pytest.raises(InvalidRepositoryStateError, match="pending authority settlement"):
        mg.fork(mg.ground, "blocked-by-pending-runtime-authority")

    assert mg.recover_authority_settlements() == pending
    assert mg.list_authority_settlement_pending() == ()


def test_reversible_run_isolates_via_copy_floor_without_native_carrier(tmp_path: Path) -> None:
    """With the portable copy-carrier floor a reversible run no longer fails
    closed when no native overlay/clonefile carrier is configured: the copy
    carrier provides isolation on every platform, so the fork succeeds and the
    body runs bound to an execution capability. (Before the floor this raised
    "no overlay backend is available"; the branch-level guard in
    ``test_filesystem_runtime`` still covers the truly carrier-less substrate.)"""
    mg, driver, _ = _make_env(tmp_path / "ws", isolation_capable=False)
    try:
        mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="noop")
        # The copy floor supplied an isolation-capable carrier, so the driver ran
        # bound to an execution capability instead of the fork refusing.
        assert any(s.get("entry") == "prepare_bound" for s in driver.seen)
    finally:
        mg.deactivate()


@pytest.mark.parametrize(
    ("params", "execution_options", "expected_admission_params"),
    [
        ({"behavior": "noop"}, CommandExecutionOptions(), {"behavior": "noop"}),
        (
            {"behavior": "noop"},
            CommandExecutionOptions(non_reversible_run=True),
            {"behavior": "noop"},
        ),
    ],
)
def test_execution_bound_run_admission_rejects_before_fork_or_body(
    tmp_path: Path,
    params: dict[str, Any],
    execution_options: CommandExecutionOptions,
    expected_admission_params: dict[str, Any],
) -> None:
    mg, driver, backend = _make_env(
        tmp_path / "ws",
        admission_error=ValueError("blocked by execution admission"),
    )
    assert backend is not None
    try:
        with pytest.raises(CommandAdmissionError, match="blocked by execution admission"):
            mg.execute_recorded("runprobe", "run", scope=mg.ground, execution_options=execution_options, **params)

        assert driver.admissions == [
            {
                "entry": "admit",
                "command": "run",
                "scope_name": "ground",
                "params": expected_admission_params,
            }
        ]
        assert driver.seen == []
        assert all(not name.startswith("run-") for name in backend.layers)
        assert backend.committed == []
    finally:
        mg.deactivate()


def test_loud_opt_out_runs_against_ground(env: tuple[VcsCore, _RunDriver, MockOverlayBackend]) -> None:
    mg, driver, backend = env
    outcome = mg.execute_recorded(
        "runprobe",
        "run",
        scope=mg.ground,
        behavior="noop",
        execution_options=CommandExecutionOptions(non_reversible_run=True),
    )
    assert outcome.value.transitions[0].payload["reached"] == "prepare_bound"
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["isolation"] == "ground"
    assert all(not name.startswith("run-") for name in backend.layers)
    assert backend.committed == []


def test_explicit_false_opt_out_stays_reversible(env: tuple[VcsCore, _RunDriver, MockOverlayBackend]) -> None:
    mg, driver, backend = env
    outcome = mg.execute_recorded(
        "runprobe",
        "run",
        scope=mg.ground,
        behavior="noop",
        execution_options=CommandExecutionOptions(non_reversible_run=False),
    )
    assert outcome.value.transitions[0].payload["reached"] == "prepare_bound"
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert call["isolation"] == "isolated"
    assert call["scope_name"].startswith("run-")
    assert (call["scope_name"], "ground") in backend.committed


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_non_reversible_run_requires_bool_before_fork_or_body(
    tmp_path: Path,
    value: object,
) -> None:
    mg, driver, backend = _make_env(tmp_path / "ws")
    assert backend is not None
    try:
        with pytest.raises(SchemaValidationError, match="must be a bool"):
            mg.execute_recorded(
                "runprobe",
                "run",
                scope=mg.ground,
                behavior="noop",
                execution_options=CommandExecutionOptions(non_reversible_run=value),  # type: ignore[arg-type]
            )
        assert driver.admissions == []
        assert driver.seen == []
        assert all(not name.startswith("run-") for name in backend.layers)
        assert backend.committed == []
    finally:
        mg.deactivate()


def test_non_reversible_run_rejected_for_non_execution_command(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    mg, driver, backend = env
    with pytest.raises(SchemaValidationError, match="only valid for execution-bound commands"):
        mg.execute_recorded(
            "runprobe",
            "list",
            scope=mg.ground,
            execution_options=CommandExecutionOptions(non_reversible_run=True),
        )
    assert driver.admissions == []
    assert driver.seen == []
    assert all(not name.startswith("run-") for name in backend.layers)


# --- PD2 pre-land gate (i): the merge routes through the lifecycle seam -------


def test_wrap_merge_fires_the_merge_hook_seam(env: tuple[VcsCore, _RunDriver, MockOverlayBackend]) -> None:
    mg, driver, _ = env
    merged: list[str] = []
    discarded: list[str] = []
    mg.on_merge(merged.append)
    mg.on_discard(discarded.append)

    mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write")
    (ok_call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert merged == [ok_call["scope_name"]]

    with pytest.raises(RuntimeError, match="body failed"):
        mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write-then-fail")
    fail_call = [s for s in driver.seen if s["entry"] == "prepare_bound"][-1]
    assert discarded == [fail_call["scope_name"]]


def test_wrap_merge_routes_captured_delta_through_recording_seam(
    env: tuple[VcsCore, _RunDriver, MockOverlayBackend],
) -> None:
    """The captured delta flows through ``prepare_merge`` → effect recording.

    This is the seam check-at-commit supervision attaches to: a wrap that
    drove a store-level merge directly would land bytes with no recorded
    effects, and nothing supervising the merge could ever see (or refuse)
    them. Measured here: the body's write surfaces as a recorded effect on
    the merged history, alongside the run scope's ``ScopeMerge``.

    (Merge-time *surface refusal* — ``SurfacePolicyError`` against the
    captured diff — is deliberately NOT asserted: vcs-core's active-surface
    check today gates python-tier writes and session capture only; the
    per-effect / check-at-commit enforcement cell is parked, per
    ``effect-permissions-gradual-typing.md``, and arrives with the dialect's
    supervised variant. The wrap's job is to keep this seam routed so that
    cell stays structurally reachable.)
    """
    mg, driver, backend = env
    mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write")
    (call,) = [s for s in driver.seen if s["entry"] == "prepare_bound"]
    assert (call["scope_name"], "ground") in backend.committed

    effects = list(mg.log(max_count=30))
    captured = [
        effect
        for effect in effects
        if effect.metadata.get("type") == "FileCreate" and effect.metadata.get("path") == "run-artifact.txt"
    ]
    assert captured, "the captured delta must surface as a recorded effect on merged history"
    # Implicit capture, measured: the effect came from the carrier's merge diff.
    assert captured[0].metadata.get("capture_mechanism") == "overlay-diff"
    assert any(
        effect.metadata.get("type") == "ScopeMerge" and effect.metadata.get("scope") == call["scope_name"]
        for effect in effects
    )
    # The wrap's journal entry carries the auditable reversible_run marker.
    assert any(
        effect.metadata.get("reversible_run") is True and effect.metadata.get("scope") == call["scope_name"]
        for effect in effects
    )


# --- PD4: the negotiation rule — fail-closed under version skew ----------------


def test_negotiation_rule_conformance_for_the_run_driver() -> None:
    """An opted-in driver dispatched without authority refuses to run real."""
    verify_execution_negotiation(_RunDriver(backend=MockOverlayBackend()))


def test_negotiation_rule_catches_the_silent_in_process_fallback() -> None:
    @dataclass(frozen=True)
    class _LeakyDriver(_RunDriver):
        binding: str = "leaky"

        def prepare(self, context: DriverContext, request: Any) -> DriverIngressResult:
            # Deliberately non-conforming: runs the execution command in-process.
            return _result(context, request, reached="prepare")

    with pytest.raises(AssertionError, match="silent in-process fallback"):
        verify_execution_negotiation(_LeakyDriver(backend=MockOverlayBackend()))


def test_negotiation_rule_catches_undeclared_execution_commands() -> None:
    @dataclass(frozen=True)
    class _UndeclaredDriver(_RunDriver):
        binding: str = "undeclared"

        @property
        def execution_commands(self) -> frozenset[str]:
            return frozenset({"run", "ghost"})

    with pytest.raises(AssertionError, match="ghost"):
        verify_execution_negotiation(_UndeclaredDriver(backend=MockOverlayBackend()))


def test_skewed_dispatch_of_execution_command_refuses(env: tuple[VcsCore, _RunDriver, MockOverlayBackend]) -> None:
    """Simulated version skew: plain prepare of the execution command refuses."""
    _mg, driver, _ = env
    request = CommandRequest(command="run", params={})
    probe_context = DriverContext(
        operation_id="skew-probe",
        binding="runprobe",
        role=driver.role,
        store_identity=SubstrateStoreIdentity(store_id="s", kind="test", resource_id="r"),
        base_heads=(),
    )
    with pytest.raises(ExecutionAuthorityRequired, match="refusing to run real"):
        driver.prepare(probe_context, request)


# --- W2 (B3c-3): the run-identity readback ------------------------------------


def test_world_oid_is_the_run_identity_readback(env: tuple[VcsCore, _RunDriver, MockOverlayBackend]) -> None:
    """`mg.world_oid()` reads the durable ground world-commit OID around a run:
    input before dispatch (the rewind handle), output after a merged run
    (changed), unchanged after a discarded run. The one public Group-E query;
    composition stays run-internal."""
    mg, _driver, _backend = env
    input_oid = mg.world_oid()

    mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write")
    merged_oid = mg.world_oid()
    assert merged_oid is not None
    assert merged_oid != input_oid

    with pytest.raises(RuntimeError, match="body failed"):
        mg.execute_recorded("runprobe", "run", scope=mg.ground, behavior="write-then-fail")
    assert mg.world_oid() == merged_oid, "a discarded run must not move ground identity"


# --- PD2 pre-land gate (ii): recovery never auto-merges; archive survives -----


def test_recovery_of_orphaned_reversible_run_never_auto_merges_and_archives(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    mg1, _driver, backend = _make_env(root)
    assert backend is not None

    # Replicate the wrap's mid-flight state, then die without closing: an
    # isolated run scope with a half-written delta and an open operation.
    run_scope = mg1.fork(mg1.ground, "run-orphaned", hints=ForkHints(isolated=True))
    backend.write_file(run_scope.name, "half.txt", b"partial work")
    with mg1._lock:
        mg1._pipeline.reset()
        mg1._pipeline.begin_operation(handle_id="run-orphaned-op", kind="runprobe.run", scope=run_scope)
    mg1._pipeline.reset()
    mg1._active_scopes.clear()
    mg1._scope_parents.clear()
    mg1._isolated_scopes.clear()
    mg1._restored_scopes.clear()
    mg1._patch_manager.uninstall_all()
    for substrate in reversed(mg1.lifecycle_substrates):
        if hasattr(substrate, "deactivate"):  # SPI drivers have no activation lifecycle
            substrate.deactivate()
    release_session_lock(mg1._repo_path, mg1._session_id)

    mg2 = VcsCore(str(root))
    mg2.activate()
    try:
        # Orphan-detected, and the half-run delta was NOT auto-merged.
        orphaned = mg2.list_orphaned_operations()
        assert any(op.kind == "runprobe.run" for op in orphaned)
        assert backend.committed == []

        archived_ids = mg2.archive_orphaned_operations()
        assert "run-orphaned-op" in archived_ids or any("run-orphaned" in a for a in archived_ids)
        archived_scopes = mg2.archive_orphaned_scopes()
        assert any("run-orphaned" in ref for ref in archived_scopes)

        # Still never merged — the disposition is discard-shaped, and the
        # salvage path is inspect-the-archive: the operation journal entry
        # survives in the archived projections.
        assert backend.committed == []
        summaries = mg2.archived_operations(max_count=50)
        assert any(s.kind == "runprobe.run" for s in summaries)
        assert mg2.list_orphaned_operations() == ()
    finally:
        mg2.deactivate()
