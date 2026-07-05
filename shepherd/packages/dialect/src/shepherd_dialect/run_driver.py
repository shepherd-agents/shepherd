"""The Shepherd run driver — the dialect side of the execution boundary (PD5).

Move-not-build discharge of vcs-core's experimental ``RuntimeSubstrateDriver``
(`decisions.md` ``dialect-composes-boundary``): the run *driver* lives in the
dialect; vcs-core exposes task-agnostic execution-mechanism verbs through its
SPI, and this driver composes them. The driver opts into execution authority
(``ExecutionBoundDriver``); the coordinator's reversible wrap forks an
isolated run scope, hands a per-run ``ExecutionCapability`` through
``prepare_bound``, captures the workspace delta implicitly at merge, and
discards on failure.

Import discipline (the no-private-coupling invariant, from day one): this
package imports only ``vcs_core.runtime_api``, ``vcs_core.spi``, and the
curated ``vcs_core.runtime_substrate`` support surface — never ``vcs_core._*``.

In-process scope (PD5): the body runs in-process, *pointed at* the run
scope's working path but only advisorily confined to it — the dev column of
the run-mode matrix. The jailed column (``may=`` → ``ConfinementSpec`` →
``execution.launch_confined``) is B3c and slots into ``prepare_bound`` below.
"""

from __future__ import annotations

import contextlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

from vcs_core.runtime_substrate import (
    ExecutionProvider,
    FileCreate,
    FilePatch,
    HandlerStack,
    InProcessExecutionProvider,
    SubstrateOperationProposed,
    TaskIdResolutionError,
    UnhandledAsk,
    resolve_task_id,
)
from vcs_core.spi import (
    BaseSubstrateDriver,
    CapabilitySet,
    CommandRequest,
    CommandSpec,
    DriverContext,
    DriverIngressResult,
    DriverSchema,
    ExecutionAuthorityRequired,
    ExecutionBoundDriver,
    ExecutionCapability,
    ParamSpec,
    SubstrateDriver,
    TransitionDraft,
    verify_execution_negotiation,
)

from shepherd_dialect.confinement import (
    MayResolution,
    resolve_may,
)
from shepherd_dialect.permission_plan import install as install_permission_plan
from shepherd_dialect.provider_runtime import (
    ProviderInvocationError,
    observations_from_provider_events,
    outcome_mapping_from_execution_result,
    provider_events_from_execution_result,
)
from shepherd_dialect.runtime_options import parse_runtime_options

__all__ = ["ShepherdRunDriver"]


def _scan_tree(root: Any) -> dict[str, bytes]:
    """Snapshot the run scope's working tree (the supervised-delta baseline)."""
    from pathlib import Path

    root = Path(root)
    if not root.exists():
        return {}
    out: dict[str, bytes] = {}
    for candidate in sorted(root.rglob("*")):
        rel = candidate.relative_to(root).as_posix()
        if rel == ".vcscore" or rel.startswith(".vcscore/"):
            continue
        if candidate.is_file():
            out[rel] = candidate.read_bytes()
    return out


def _rebased_binding_grants(binding_grants: Any, working_path: Any) -> list[Any]:
    """Re-root working-path-relative per-binding grants against the run's clone working path.

    A run executes in an isolated overlay clone whose absolute path differs from the bound
    workspace roots, so the jail's writable roots must name the clone's sub-roots, not the
    workspace's. Lane C stages each bound sub-root as a working-path-relative POSIX path; here we
    join it to ``execution.working_path`` (the only point that holds both the relative sub-root and
    the clone path). An **absolute** grant root is used as-is — byte-identical to the pre-Lane-C
    per-binding install path (the run-driver unit coverage passes absolute roots).
    """
    from dataclasses import replace
    from pathlib import Path, PurePosixPath

    working = Path(working_path)
    rebased: list[Any] = []
    for grant in binding_grants:
        root = grant.root
        if PurePosixPath(root).is_absolute() or Path(root).is_absolute():
            rebased.append(grant)
        else:
            # Defense in depth at the seam: the facade's staging already refuses `..`/`.`
            # relative roots fail-closed, but a joined `..` here would authorize a subtree
            # OUTSIDE the run clone — refuse rather than trust the (Python-only) caller.
            parts = PurePosixPath(root).parts
            if root in {"", "."} or any(part in {".", ".."} for part in parts):
                raise ValueError(
                    f"per-binding grant root {root!r} must be a clean working-path-relative "
                    "POSIX sub-root (no '.'/'..' segments); refusing to rebase it"
                )
            rebased.append(replace(grant, root=str(working / PurePosixPath(root))))
    return rebased


def _resolve_enforced_may(params: Mapping[str, Any]) -> MayResolution:
    """Resolve the single authority source for a run.

    Confinement is lowered from this value and provenance records this same
    value. Future envelope admission must reconcile into top-level ``may`` via
    ``normalize_task_run_params`` before the driver; raw ``envelope`` params are
    rejected by the command contract before this function is reachable.
    """
    return resolve_may(params.get("may"))


@dataclass(frozen=True)
class ShepherdRunDriver(BaseSubstrateDriver):
    """The production dialect's run driver: ``run`` composes vcs-core's verbs.

    Stateless and frozen like every SPI driver; per-run authority arrives
    through the ``prepare_bound`` call, never stored on the instance.
    """

    store_id: str = "store_runtime"
    binding: str = "runtime"
    role: str = "shepherd.RunDriver"
    driver_id: str = "shepherd.run_driver"
    driver_version: str = "v0.2"
    materialization_class: str = "noop"

    @property
    def capabilities(self) -> CapabilitySet:
        # The run-driver fingerprint: a run is a journaled operation, not a
        # selected head. (A selectable run-index face is B4b territory.)
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
        run_params = {
            "task_body": ParamSpec(
                type="callable",
                required=False,
                projectable=False,
                description="In-process Python task callable.",
            ),
            "task_id": ParamSpec(
                type="str",
                required=False,
                description="Tier-A task identity: the fully-qualified import path ('pkg.module:attr').",
            ),
            "args": ParamSpec(type="object", required=False),
            "may": ParamSpec(
                type="str",
                required=False,
                description="Declared effect-surface profile name. Recorded as provenance; "
                "advisory in-process — enforced at the jail from B3c.",
            ),
            "runtime": ParamSpec(
                type="object",
                required=False,
                description="Runtime option envelope. Current branch records it as provenance; "
                "execution selection still uses the explicit provider instance.",
            ),
            "binding_grants": ParamSpec(
                type="object",
                required=False,
                projectable=False,
                description="Per-binding grant sequence (Lane C LC-3d): a Sequence[BindingRootGrant]. "
                "When present, confinement lowers from these grants through the same "
                "install() seam instead of the whole-workspace may= profile.",
            ),
            "provider": ParamSpec(
                type="ExecutionProvider",
                required=False,
                projectable=False,
                description="ExecutionProvider instance; defaults to the in-process dev-tier provider.",
            ),
            "substrate_handlers": ParamSpec(
                type="Sequence[Handler]",
                required=False,
                projectable=False,
                description="In-process substrate handler stack.",
            ),
            "supervisor_handlers": ParamSpec(
                type="Sequence[Handler]",
                required=False,
                projectable=False,
                description="In-process supervisor handler stack.",
            ),
        }
        return DriverSchema(
            driver_id=self.driver_id,
            driver_version=self.driver_version,
            capabilities=self.capabilities,
            commands={
                "run": CommandSpec(
                    description="Run a task through the execution-mechanism verbs: reversible by "
                    "default, body pointed at the run scope's working path, delta "
                    "captured implicitly at merge.",
                    params=run_params,
                    required_one_of=(("task_body", "task_id"),),
                ),
            },
        )

    def prepare(self, context: DriverContext, request: Any) -> DriverIngressResult:
        del context
        if request.command in self.execution_commands:
            # The negotiation rule (PD4): an opted-in driver dispatched without
            # execution authority refuses to run real — before touching params,
            # never a silent in-process fallback.
            raise ExecutionAuthorityRequired(
                f"{self.binding}.{request.command} requires execution authority "
                f"(coordinator too old, or capability not offered); refusing to run real."
            )
        raise ValueError(f"unsupported runtime command: {request.command!r}")

    def prepare_bound(
        self,
        context: DriverContext,
        request: Any,
        execution: ExecutionCapability,
    ) -> DriverIngressResult:
        params: Mapping[str, Any] = request.params
        task_body = params.get("task_body")
        task_id = params.get("task_id")
        if (task_body is None) == (task_id is None):
            raise TaskIdResolutionError("exactly one of 'task_body' / 'task_id' must be supplied to the run command")
        if task_body is None:
            if not isinstance(task_id, str):
                raise TaskIdResolutionError("'task_id' must be a string when supplied")
            task_body = resolve_task_id(task_id)

        provider: ExecutionProvider = params.get("provider") or InProcessExecutionProvider()
        args = dict(params.get("args") or {})
        runtime_options = parse_runtime_options(params.get("runtime")).to_payload()
        # Point the body at the run scope's working path — injected only when
        # the body asks for it by name (no magic for bodies that don't).
        task_body_parameters = inspect.signature(task_body).parameters
        if "working_path" in task_body_parameters:
            args.setdefault("working_path", str(execution.working_path))
        if "execution" in task_body_parameters:
            args["execution"] = execution

        substrate_handlers: Sequence[Mapping[type, Callable[..., Any]]] = params.get("substrate_handlers", ())
        supervisor_handlers: Sequence[Mapping[type, Callable[..., Any]]] = params.get("supervisor_handlers", ())
        stack = HandlerStack()
        for frame in substrate_handlers:
            stack.push(frame)
        for frame in supervisor_handlers:
            stack.push(frame)

        # B3c-1 / seam: lower the declared may= through the monitor-assignment
        # compiler (install() -> PermissionPlan) rather than calling the jail lowering
        # directly. The plan names both monitors that enforce this run — the syscall
        # jail (pre-action) and the carrier check-at-commit gate (below) — so the two
        # enforcement lanes now meet in one plan instead of running side by side.
        # plan.confinement is the same ConfinementSpec the jail lowering produced, so
        # this is behaviour-preserving: a jailed provider composes launch_confined; the
        # in-process provider ignores it (advisory column). The resolution still carries
        # declared/resolved/source so a defaulted Permissive is recorded as such
        # (`may-default-is-permissive`, amended).
        #
        # LC-3d (Lane C): the multi-binding path carries per-binding grants end to end. When a
        # `Sequence[BindingRootGrant]` is present, confinement lowers from THOSE grants through the
        # SAME install() seam (never a collapsed whole-run may= scalar — that would trip the S2
        # amplification). The resulting PermissionPlan therefore cites the per-binding assignment.
        # `may_resolution` is still resolved and recorded below as run provenance.
        may_resolution = _resolve_enforced_may(params)
        binding_grants = params.get("binding_grants")
        if binding_grants:
            permission_plan = install_permission_plan(
                _rebased_binding_grants(binding_grants, execution.working_path), execution.working_path
            )
        else:
            permission_plan = install_permission_plan(may_resolution, execution.working_path)
        confinement = permission_plan.confinement
        if "confinement" in task_body_parameters:
            args["confinement"] = confinement
        # Pattern B (the check-at-commit cell): with supervisors installed,
        # baseline the working tree so the captured delta can be PROPOSED at
        # return time — after the body, before the wrap merges (the last undo
        # point). A denial raises out of prepare_bound and the wrap discards.
        baseline = _scan_tree(execution.working_path) if supervisor_handlers else None
        try:
            raw_outcome = provider.execute(
                task_body, stack, context, args, execution=execution, confinement=confinement
            )
        except ProviderInvocationError as exc:
            exc.runtime_operation_id = context.operation_id
            raise
        provider_events = provider_events_from_execution_result(raw_outcome)
        provider_observations = observations_from_provider_events(provider_events)
        outcome = outcome_mapping_from_execution_result(raw_outcome)
        supervision: list[dict[str, str]] = []
        if baseline is not None:
            after = _scan_tree(execution.working_path)
            for path in sorted(after):
                if baseline.get(path) == after[path]:
                    continue
                effect = (
                    FilePatch(path=path, content=after[path])
                    if path in baseline
                    else FileCreate(path=path, content=after[path])
                )
                # Absent-supervisor semantics are try_dispatch-shaped: a
                # stack with no handler for the hook approves by default.
                with contextlib.suppress(UnhandledAsk):
                    stack.dispatch(SubstrateOperationProposed(binding="workspace", effect=effect))
                supervision.append({"path": path, "op": type(effect).__name__, "decision": "approved"})

        identity = execution.identity
        provider_observation_ids = tuple(observation.observation_id for observation in provider_observations)
        return DriverIngressResult(
            observations=provider_observations,
            transitions=(
                TransitionDraft(
                    transition_id="primary",
                    semantic_op="execute",
                    payload={
                        "schema": "shepherd/run/v0",
                        # The portable half: device-independent run identity +
                        # outcome (runtime-substrate-revisions.md §5.2). The
                        # consumer READS identity here; it never composes it.
                        "portable_core": {
                            "operation_id": context.operation_id,
                            "binding": context.binding,
                            "run_scope": {
                                "scope_ref": identity.scope_ref,
                                "scope_name": identity.scope_name,
                                "scope_instance_id": identity.scope_instance_id,
                                "world_id": identity.world_id,
                                "session_id": identity.session_id,
                            },
                            "may": may_resolution.as_record(),
                            "runtime": runtime_options,
                            "supervision": tuple(supervision),
                            "outcome": outcome,
                        },
                        # The device-dependent half: how this run was carried.
                        "device_projection": {
                            "isolation": execution.isolation,
                            "working_path": str(execution.working_path),
                            "provider": getattr(provider, "provider_id", type(provider).__name__),
                        },
                    },
                    observation_ids=provider_observation_ids,
                    base_heads=context.base_heads,
                    materialization_class=self.materialization_class,
                ),
            ),
        )


# Structural self-checks, carried over from the discharged module: the driver
# satisfies both the SPI Protocol and the execution capability opt-in, and
# conforms to the negotiation rule (fail-closed under version skew).
assert isinstance(ShepherdRunDriver(), SubstrateDriver)
assert isinstance(ShepherdRunDriver(), ExecutionBoundDriver)
verify_execution_negotiation(ShepherdRunDriver())
