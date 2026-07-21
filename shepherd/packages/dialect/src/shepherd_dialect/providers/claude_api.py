"""The Claude Agent SDK worker execution provider (transport: agent_sdk_worker)."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Mapping  # noqa: TC003 — annotations must resolve at runtime (typing.get_type_hints)
from dataclasses import dataclass
from importlib import resources
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect.provider_capabilities import (
    CANONICAL_WORKSPACE_TOOL_NAMES,
    AgentProviderCapabilities,
)
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
from shepherd_dialect.provider_worker import parse_provider_worker_output
from shepherd_dialect.providers._common import (
    SCRATCH_RESIDUE_ERROR_TYPE,
    _invocation_id,
    _provider_prompt,
    hard_stop_prefix,
    scratch_residue_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext


def _claude_agent_sdk_worker_source() -> str:
    """Return the jailed Claude Agent SDK worker source copied into run scratch."""
    return (
        resources.files("shepherd_dialect.workers").joinpath("claude_agent_sdk_worker.py").read_text(encoding="utf-8")
    )


@dataclass(frozen=True)
class ClaudeApiProvider:
    """Claude API/SDK worker executor for the VcsCore-native run path.

    The public distinction is transport, not "agentness": this class invokes
    Claude through the Python SDK worker ABI, while ``ClaudeHeadlessProvider``
    invokes the local headless CLI. Both consume the rendered task prompt by
    default and do not consume ``workspace(model=...)`` for ordinary Python task
    bodies.
    """

    provider_id: str = "claude-api"
    prompt: str = ""
    model: str | None = None
    max_turns: int = 4
    # Plumbed through to the SDK's ``resume`` option, but not executable across
    # jailed runs today: the SDK spawns the CLI, whose resume needs the session
    # transcript under the redirected ``CLAUDE_CONFIG_DIR`` — and every run gets
    # a fresh scratch that is scrubbed on return, so the transcript is
    # guaranteed absent. Kept for a future slice that records session state as
    # an addressable input (the P-030 W3 recording-policy decision owns that).
    resume: str | None = None
    output_schema: Mapping[str, Any] | None = None
    python_path: str = sys.executable
    budget_seconds: int = 120

    _SCRATCH = ".claude-sdk-scratch"

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="agent_sdk_worker",
            confined=True,
            network_required=True,
            structured_output=True,
            # False despite the ``resume`` field: the executable claim fails on
            # the scrubbed-scratch lifecycle (see the field comment). Flip only
            # when session state survives runs as a recorded, addressable input.
            session_resume=False,
            workspace_tools=CANONICAL_WORKSPACE_TOOL_NAMES,
            custom_tools=False,
            mcp=False,
        )

    def command_argv(self, worker_path: Path | str, payload_path: Path | str, python: str) -> list[str]:
        return [
            *hard_stop_prefix(self.budget_seconds),
            "/usr/bin/env",
            f"HOME={Path(worker_path).parent / 'home'}",
            f"CLAUDE_CONFIG_DIR={Path(worker_path).parent / 'config'}",
            f"TMPDIR={Path(worker_path).parent / 'tmp'}",
            "DISABLE_AUTOUPDATER=1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            python,
            str(worker_path),
            str(payload_path),
        ]

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
        del stack, context
        if execution is None or confinement is None:
            raise ExecutionAuthorityRequired(
                "the Claude API provider runs only jailed: it needs the per-run "
                "ExecutionCapability and a lowered ConfinementSpec."
            )
        prompt = _provider_prompt(self.prompt, task_body, args, "ClaudeApiProvider")
        python = shutil.which(self.python_path) if Path(self.python_path).name == self.python_path else self.python_path
        invocation_id = _invocation_id(self.provider_id, execution)
        sequence = count()
        started = ProviderEvent(
            kind=PROVIDER_INVOCATION_STARTED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next(sequence),
            event_id=f"{invocation_id}:started",
            model=self.model,
            payload={
                "prompt_digest": digest_jsonable({"prompt": prompt}),
                "permission_mode": "bypassPermissions",
                "tools": "claude_code",
                "max_turns": self.max_turns,
                "resume_present": self.resume is not None,
                "output_schema_present": self.output_schema is not None,
                "sdk_home_redirected": True,
            },
        )
        if python is None:
            message = f"python executable not found on PATH: {self.python_path}"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model,
                payload={
                    "error_type": "ClaudeApiProviderError",
                    **redacted_text_payload(message, field="error"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))

        scratch = Path(execution.working_path) / self._SCRATCH
        worker_path = scratch / "claude_agent_sdk_worker.py"
        payload_path = scratch / "payload.json"
        for sub in ("home", "config", "tmp"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        worker_path.write_text(_claude_agent_sdk_worker_source(), encoding="utf-8")
        payload_path.write_text(
            json.dumps(self._worker_payload(execution.working_path, prompt), sort_keys=True),
            encoding="utf-8",
        )
        try:
            proc = execution.launch_confined(self.command_argv(worker_path, payload_path, python), confinement)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        if scratch.exists():
            # Fail closed on scrub residue (§4.7 parity): the SDK worker scratch
            # holds the seeded credentials and the worker payload.
            message = scratch_residue_message(self._SCRATCH)
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model,
                payload={
                    "returncode": proc.returncode,
                    "error_type": SCRATCH_RESIDUE_ERROR_TYPE,
                    "failure_classification": "scrub_residue",
                    **redacted_text_payload(message, field="error"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))

        if proc.returncode != 0:
            signal = ((proc.stderr or "") + (proc.stdout or "")).strip()
            message = f"Claude API worker failed (rc={proc.returncode}): {signal[-300:]}"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model,
                payload={
                    "returncode": proc.returncode,
                    "error_type": "ClaudeApiProviderError",
                    **redacted_text_payload(message, field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))

        sequence_start = next(sequence)
        try:
            parsed = parse_provider_worker_output(
                proc.stdout or "",
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                model=self.model,
                sequence_start=sequence_start,
                stderr=proc.stderr or "",
            )
        except Exception as exc:
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=sequence_start,
                event_id=f"{invocation_id}:failed",
                model=self.model,
                payload={
                    "error_type": type(exc).__name__,
                    **redacted_text_payload(str(exc), field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                },
            )
            raise ProviderInvocationError(str(exc), provider_events=(started, failed)) from exc

        completed = ProviderEvent(
            kind=PROVIDER_INVOCATION_COMPLETED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=sequence_start + len(parsed.events),
            event_id=f"{invocation_id}:completed",
            model=str(parsed.result.metadata.get("model") or self.model or "claude-api"),
            payload={
                "returncode": proc.returncode,
                "session_id": parsed.result.session_id or "",
                **parsed.diagnostics,
            },
        )
        result = ProviderInvocationResult(
            output_text=parsed.result.output_text,
            structured_output=parsed.result.structured_output,
            session_id=parsed.result.session_id,
            usage=parsed.result.usage,
            events=(started, *parsed.events, completed),
            metadata=parsed.result.metadata,
        )
        return ExecutionProviderResult(
            outcome=provider_invocation_outcome(
                result,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
            ),
            provider_events=result.events,
        )

    def _worker_payload(self, working_path: Path | str, prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "cwd": str(Path(working_path)),
            "tools": {"type": "preset", "preset": "claude_code"},
            "permissionMode": "bypassPermissions",
            "maxTurns": self.max_turns,
        }
        if self.model is not None:
            payload["model"] = self.model
        if self.resume is not None:
            payload["resume"] = self.resume
        if self.output_schema is not None:
            payload["outputSchema"] = dict(self.output_schema)
        return payload
