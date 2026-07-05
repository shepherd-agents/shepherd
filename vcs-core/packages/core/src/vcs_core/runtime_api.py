"""The runtime call API — vcs-core's consumer surface (the kernel's "syscalls").

What a consumer (a dialect shim, the ``vcs-core`` CLI, another execution driver)
calls to *use* vcs-core as the runtime. Counterpart to the substrate SPI
(``vcs_core.spi`` — what substrates and drivers *implement*).
Shape per ``docs/engineering/convergence/runtime-call-api.md`` §4; placement
ratified 2026-06-10 (execplan §6 open decision 2 — top-level, not
``experimental/``: the most stable surface doesn't hide there).

Stability: "don't break userspace" — ``CALL_API_VERSION`` versions this
surface independently of the SPI's ``SPI_VERSION``; the shared command-seam
types (Group B below) are the one point the two must move together
(co-versioned; ``runtime-call-api.md`` §5).

The consumer surface is deliberately small:

- **Group A — construction**: open a store, build the substrate context,
  construct and activate the coordinator.
- **Group B — the run / command seam**: build a ``CommandRequest`` and
  dispatch it (in-process via ``VcsCore.exec``; CLI via
  ``vcs-core exec <binding> <command>``); read identity and outcome back from the
  ``DriverIngressResult``.

NOT on this surface — ``run``-internal (vcs-core's own, driven by the run
dispatch): ``may=`` lowering/enforcement (Group D), world-OID identity
composition (Group E — ``VcsCore.world_oid(scope)``,
``VcsCore.read_selected_binding_revision(binding_name, scope=...)``, and
``VcsCore.read_trace_revision(head)`` are the read-only queries; composition
stays internal), scope lifecycle (Group C —
public-but-advanced on ``VcsCore``), and direct substrate-method
application. A consumer that reaches for those is bypassing ``run``; the
conformance ratchet's route-through-``run`` invariant exists to catch it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# --- Group A: construction (re-exported for one import home) ---
from vcs_core import build_builtin_substrate_context
from vcs_core._authority import (
    AuthorityDecision,
    AuthorityMergeResult,
    AuthorityOutcome,
    AuthzMatchView,
    GitRepoAuthorityRequest,
)

# Boot-time workspace adoption — the Python twin of `vcs-core init --adopt`:
# record pre-existing workspace files as ordinary effects on ground so that
# later materialization (`push`) owns them. Promoted at B3c-3 alongside
# `VcsCore.world_oid()`; the CLI was previously the only caller.
from vcs_core._command_envelope import AuthorityMergeControl, CommandExecutionOptions
from vcs_core._identity import initialize_ground_world_id
from vcs_core._workspace_adoption import adopt_workspace_baseline

# --- Group B: the run / command seam (shared with the SPI; consumer send-side) ---
from vcs_core.spi import (
    EXECUTION_CAPABILITY_VERSION,
    SPI_VERSION,
    CommandRequest,
    DriverContext,
    DriverIngressResult,
)
from vcs_core.store import Store
from vcs_core.types import AuthorityExecutionOutcome
from vcs_core.vcscore import VcsCore

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core._binding_contracts import ResolvedDriverBinding
    from vcs_core._command_contract import CommandContract
    from vcs_core.spi import DriverSchema
    from vcs_core.types import RecordedCommandOutcome, ScopeInfo

__all__ = [
    "CALL_API_VERSION",
    "AuthorityDecision",
    "AuthorityExecutionOutcome",
    "AuthorityMergeControl",
    "AuthorityMergeResult",
    "AuthorityOutcome",
    "AuthzMatchView",
    "CommandExecutionOptions",
    "CommandRequest",
    "DriverContext",
    "DriverIngressResult",
    "GitRepoAuthorityRequest",
    "Store",
    "VcsCore",
    "adopt_workspace_baseline",
    "build_builtin_substrate_context",
    "initialize_ground_world_id",
    "native_jail_available",
    "substrate_client",
    "version_surfaces",
]

#: The consumer call-API version — independent of SPI_VERSION; the shared
#: command-seam types co-version with the SPI (runtime-call-api.md §5).
CALL_API_VERSION = "v0.1"


def version_surfaces() -> dict[str, object]:
    """The version surfaces at the execution seam, interrogable from one place.

    Which one binds you is audience-keyed (the decision procedure in
    ``runtime-call-api.md`` §5): a *consumer* binds to ``call_api`` only; a
    *driver author* binds to ``ingestion_spi`` and — iff opting into execution
    — ``execution_capability`` (skew fails closed, never a silent in-process
    fallback). The shared command-seam types co-version with both sides.
    """
    return {
        "call_api": CALL_API_VERSION,
        "ingestion_spi": SPI_VERSION,
        "execution_capability": EXECUTION_CAPABILITY_VERSION,
    }


def native_jail_available() -> bool:
    """Return whether the current host exposes an available native syscall-deny jail backend."""
    from vcs_core._execution_capability import detect_containment_backend

    backend = detect_containment_backend()
    if backend is None:
        return False
    try:
        available, _reason = backend.available()
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(available)


def substrate_client(mg: VcsCore, binding: str, *, scope: ScopeInfo | None = None) -> object:
    """Return a dynamic Python command proxy for one binding."""
    return _SubstrateClient(mg=mg, binding=binding, scope=scope)


@dataclass(frozen=True)
class _SubstrateClient:
    mg: VcsCore
    binding: str
    scope: ScopeInfo | None = None

    def __getattr__(self, name: str) -> Callable[..., RecordedCommandOutcome]:
        command_name = self._resolve_attribute_command(name)
        if command_name is None:
            raise AttributeError(name)
        return self.command(command_name)

    def __getitem__(self, name: str) -> Callable[..., RecordedCommandOutcome]:
        return self.command(name)

    def command(self, name: str) -> Callable[..., RecordedCommandOutcome]:
        resolved = self._resolved()
        command_contract = resolved.command_contracts.get(name)
        if command_contract is None:
            available = ", ".join(sorted(resolved.command_contracts)) or "(none)"
            raise AttributeError(f"unknown command {name!r} on binding {self.binding!r}; available: {available}")

        def _call(**kwargs: Any) -> RecordedCommandOutcome:
            return self._call(command_contract, kwargs)

        _call.__name__ = name.replace("-", "_")
        _call.__doc__ = command_contract.description
        return _call

    def _call(
        self,
        command_contract: CommandContract,
        kwargs: dict[str, Any],
    ) -> RecordedCommandOutcome:
        from vcs_core._command_contract import normalize_command_params

        raw_execution_options = kwargs.pop("execution_options", None)
        if raw_execution_options is None:
            execution_options = CommandExecutionOptions()
        elif isinstance(raw_execution_options, CommandExecutionOptions):
            execution_options = raw_execution_options
        else:
            raise TypeError(
                f"execution_options must be CommandExecutionOptions, got {type(raw_execution_options).__name__}."
            )
        params = normalize_command_params(command_contract, kwargs).params
        return self.mg.exec(
            self.binding,
            command_contract.command_name,
            scope=self.scope if self.scope is not None else self.mg.ground,
            execution_options=execution_options,
            **params,
        )

    def _resolve_attribute_command(self, name: str) -> str | None:
        command_contracts = self._resolved().command_contracts
        if name in command_contracts:
            return name
        dashed = name.replace("_", "-")
        if dashed in command_contracts:
            return dashed
        return None

    def _resolved(self) -> ResolvedDriverBinding:
        return self.mg.binding_contracts.resolve_driver(self.binding)

    def _schema(self) -> DriverSchema:
        return self._resolved().schema
