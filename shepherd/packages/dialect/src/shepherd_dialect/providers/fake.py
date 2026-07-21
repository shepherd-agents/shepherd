"""The deterministic offline execution provider that proves the capture lane."""

from __future__ import annotations

import sys
from collections.abc import Mapping  # noqa: TC003 — annotations must resolve at runtime (typing.get_type_hints)
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Any

from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect.provider_capabilities import AgentProviderCapabilities
from shepherd_dialect.provider_runtime import (
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED,
    PROVIDER_INVOCATION_STARTED,
    ExecutionProviderResult,
    ProviderEvent,
    ProviderInvocationError,
    ProviderInvocationResult,
    digest_jsonable,
    provider_invocation_outcome,
    redacted_text_payload,
)
from shepherd_dialect.providers._common import _invocation_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext


@dataclass(frozen=True)
class DeterministicFakeProvider:
    """A confined-subprocess body with a fixed, replayable effect.

    The subprocess writes one deterministic artifact into the run scope's
    working path **via the jail** (``launch_confined`` — the only
    real-execution verb). Under ``may=ReadOnly`` the write is refused at the
    syscall and the run fails closed; under ``Permissive`` the artifact is
    captured implicitly at merge. The capture lane is primary; across-jail
    command-lane effects are Phase E.
    """

    provider_id: str = "deterministic-fake"
    artifact: str = "fake-artifact.txt"
    content: str = "deterministic output\n"

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="deterministic_fake",
            confined=True,
            network_required=False,
            structured_output=True,
            session_resume=False,
            workspace_tools=frozenset(),
        )

    def execute(
        self,
        task_body: Callable[..., Any] | None,
        stack: HandlerStack,
        context: DriverContext,
        args: Mapping[str, Any],
        *,
        execution: Any = None,
        confinement: Any = None,
    ) -> ExecutionProviderResult:
        del task_body, stack, context, args  # the fake's body is the canned command
        if execution is None or confinement is None:
            raise ExecutionAuthorityRequired(
                "the deterministic fake runs only jailed: it needs the per-run "
                "ExecutionCapability and a lowered ConfinementSpec."
            )
        invocation_id = _invocation_id(self.provider_id, execution)
        sequence = count()
        script = f"import pathlib\npathlib.Path({self.artifact!r}).write_text({self.content!r})\n"
        command = [sys.executable, "-c", script]
        started = ProviderEvent(
            kind=PROVIDER_INVOCATION_STARTED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next(sequence),
            event_id=f"{invocation_id}:started",
            payload={
                "command_digest": digest_jsonable(command),
                "artifact": self.artifact,
            },
        )
        proc = execution.launch_confined(command, confinement)
        if proc.returncode != 0:
            message = (
                f"confined body refused (rc={proc.returncode}): {(proc.stderr or proc.stdout or '').strip()[-300:]}"
            )
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                payload={
                    "returncode": proc.returncode,
                    "error_type": "ConfinedProcessRefused",
                    **redacted_text_payload(message, field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                },
            )
            raise ProviderInvocationError(
                message,
                provider_events=(started, failed),
            )
        completed = ProviderEvent(
            kind=PROVIDER_INVOCATION_COMPLETED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next(sequence),
            event_id=f"{invocation_id}:completed",
            payload={
                "returncode": proc.returncode,
                "artifact": self.artifact,
                **redacted_text_payload(proc.stdout or "", field="stdout"),
                **redacted_text_payload(proc.stderr or "", field="stderr"),
            },
        )
        result = ProviderInvocationResult(
            output_text=proc.stdout or "",
            structured_output={"status": "ok", "artifact": self.artifact},
            events=(started, completed),
            metadata={"artifact": self.artifact},
        )
        outcome = provider_invocation_outcome(
            result,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
        )
        outcome.update({"status": "ok", "artifact": self.artifact})
        return ExecutionProviderResult(
            outcome=outcome,
            provider_events=result.events,
        )
