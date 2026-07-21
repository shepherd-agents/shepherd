"""The legacy Claude CLI-direct execution provider.

Status: legacy — kept only for the existing W1 compatibility contract. New
work should use ``ClaudeHeadlessProvider`` or ``ClaudeApiProvider``.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping  # noqa: TC003 — annotations must resolve at runtime (typing.get_type_hints)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect.providers._common import hard_stop_prefix, scratch_residue_message
from shepherd_dialect.providers.claude_cli import (
    _budget_exhausted_message,
    _diagnose_claude_cli_failure,
    _signals_max_turns_exhaustion,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext


@dataclass(frozen=True)
class ClaudeAgentProvider:
    """Legacy Claude CLI provider kept for the existing W1 compatibility contract.

    New launch notebooks should use ``ClaudeHeadlessProvider`` or
    ``ClaudeApiProvider``. This class preserves the older keyless argv shape and
    plain mapping return type used by current tests and external callers.
    """

    provider_id: str = "claude-agent-sdk"
    prompt: str = ""
    allowed_tools: tuple[str, ...] = ("Write", "Edit", "Read")
    max_turns: int = 4
    budget_seconds: int = 120

    _SCRATCH = ".claude-scratch"

    def command_argv(self, working_path: Path | str, cli: str) -> list[str]:
        """The full jailed argv for the legacy CLI-direct provider."""
        scratch = Path(working_path) / self._SCRATCH
        return [
            *hard_stop_prefix(self.budget_seconds),
            "/usr/bin/env",
            f"HOME={scratch / 'home'}",
            f"CLAUDE_CONFIG_DIR={scratch / 'config'}",
            f"TMPDIR={scratch / 'tmp'}",
            "DISABLE_AUTOUPDATER=1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            cli,
            "-p",
            self.prompt,
            "--allowed-tools",
            ",".join(self.allowed_tools),
            "--max-turns",
            str(self.max_turns),
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
    ) -> Mapping[str, Any]:
        del task_body, stack, context, args
        if execution is None or confinement is None:
            raise ExecutionAuthorityRequired(
                "the Claude Agent SDK provider runs only jailed: it needs the per-run "
                "ExecutionCapability and a lowered ConfinementSpec."
            )
        if not self.prompt:
            raise ValueError("ClaudeAgentProvider needs a prompt — the prompt is the body")
        cli = shutil.which("claude")
        if cli is None:
            raise RuntimeError("claude CLI not found on PATH — see the package README runbook note")
        scratch = Path(execution.working_path) / self._SCRATCH
        for sub in ("home", "config", "tmp"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        try:
            proc = execution.launch_confined(self.command_argv(execution.working_path, cli), confinement)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            # Fail closed on scrub residue (§4.7 parity); the legacy lane records
            # no events, so it refuses in its RuntimeError idiom.
            raise RuntimeError(scratch_residue_message(self._SCRATCH))
        if proc.returncode != 0:
            signal = ((proc.stderr or "") + (proc.stdout or "")).strip()
            if _signals_max_turns_exhaustion(signal):
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(f"max turns reached ({self.max_turns})")
            if proc.returncode == -14:
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(_budget_exhausted_message(self.budget_seconds, proc.stdout, proc.stderr))
            # Same actionable diagnosis as the headless lane: surface the CLI's own
            # reason (e.g. not-logged-in) instead of a blind tail-slice that drops it.
            diagnosis = _diagnose_claude_cli_failure(proc.returncode, proc.stdout, proc.stderr)
            message = f"confined body refused (rc={proc.returncode}): {diagnosis.summary}"
            if diagnosis.remedy:
                message += f"\n  → {diagnosis.remedy}"
            raise RuntimeError(message)
        return {
            "status": "ok",
            "provider": self.provider_id,
            "reply": (proc.stdout or "").strip()[-400:],
        }
