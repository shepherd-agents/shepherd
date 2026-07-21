"""The Claude headless CLI execution provider and its network auth probe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping  # noqa: TC003 — annotations must resolve at runtime (typing.get_type_hints)
from dataclasses import dataclass
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
from shepherd_dialect.providers._common import (
    SCRATCH_RESIDUE_ERROR_TYPE,
    _invocation_id,
    _provider_prompt,
    hard_stop_prefix,
    scratch_residue_message,
)
from shepherd_dialect.providers.claude_auth import (
    _claude_preflight_refusal,
    _keyless_detail,
    _resolve_claude_auth_diagnostic,
)
from shepherd_dialect.providers.claude_cli import (
    _budget_exhausted_message,
    _diagnose_claude_cli_failure,
    _parse_claude_cli_output,
    _provider_result_from_claude_stdout,
    _signals_max_turns_exhaustion,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext


@dataclass(frozen=True)
class ClaudeHeadlessProvider:
    """Claude headless CLI executor for the VcsCore-native run path.

    This provider is the headless CLI transport backed by ``claude -p``. It
    runs **inside the jail** via ``launch_confined``; its Write/Edit tools create
    real files in the carrier's working copy, and VcsCore captures the delta at
    merge. Nondeterministic and auth-needing, so it never gates CI — the runbook
    (package README) is its home.

    The argv composes three S1-proven blocks (`spikes/260610-real-sdk-jail-probe`,
    5/5), outermost first:

    - **the hard stop** — perl ``alarm``+``exec`` (the timer survives ``execve``;
      SIGALRM kills the body at ``budget_seconds``). Mandatory, not optional: under
      ``may=ReadOnly`` the CLI *hangs* on the denied network rather than failing
      fast, and ``launch_confined`` has no timeout parameter — the stop must ride
      the argv. The kill surfaces as rc -14 → the ordinary refusal path.
    - **the env redirect** — ``HOME``/``CLAUDE_CONFIG_DIR``/``TMPDIR`` pointed into
      ``<working_path>/.claude-scratch`` (the v0 lowering admits exactly one
      writable root, so housekeeping writes redirect in; the scratch is scrubbed
      before return so the captured delta stays the agent's intended writes only),
      auto-updater off (it writes to the install dir: denied).
    - **the body** — ``claude -p <prompt> --permission-mode bypassPermissions
      --tools default --max-turns …``. Provider-local permission prompts and
      tool narrowing are disabled because the VcsCore jail is the boundary
      (execution-boundary.md §7). When ``json_schema`` is set, ``--json-schema``
      rides the body too, and the terminal result envelope carries the
      schema-validated object in its ``structured_output`` field.
    """

    provider_id: str = "claude-headless"
    prompt: str = ""
    # ``max_turns=None`` runs the agent uncapped: the ``budget_seconds`` alarm
    # (below) is the always-on guardrail that bounds runaway cost/loops, so a
    # turn cap is an optional refinement rather than the primary stop. When set,
    # ``--max-turns`` is passed through to the CLI.
    max_turns: int | None = None
    budget_seconds: int = 240
    model: str | None = None  # passed to `claude --model`; None keeps the account default
    # A JSON Schema for the run's typed result. When set, ``--json-schema`` is
    # passed through and the validated object is lifted from the CLI's result
    # envelope into ``ProviderInvocationResult.structured_output``; a success
    # envelope without it then fails loudly. ``None`` keeps the flag off the
    # argv entirely, so CLIs that predate ``--json-schema`` never see it.
    json_schema: Mapping[str, Any] | None = None

    _SCRATCH = ".claude-scratch"

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="headless_cli",
            confined=True,
            network_required=True,
            # The executable claim is per-instance: an invocation produces a
            # typed result only when it was constructed with a schema to demand.
            structured_output=self.json_schema is not None,
            session_resume=False,
            workspace_tools=CANONICAL_WORKSPACE_TOOL_NAMES,
            custom_tools=False,
            mcp=False,
        )

    def command_argv(self, working_path: Path | str, cli: str, prompt: str | None = None) -> list[str]:
        """The full jailed argv — pure, so the shape is pinned by a keyless test."""
        prompt = self.prompt if prompt is None else prompt
        scratch = Path(working_path) / self._SCRATCH
        argv = [
            *hard_stop_prefix(self.budget_seconds),
            "/usr/bin/env",
            f"HOME={scratch / 'home'}",
            f"CLAUDE_CONFIG_DIR={scratch / 'config'}",
            f"TMPDIR={scratch / 'tmp'}",
            "DISABLE_AUTOUPDATER=1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            cli,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--tools",
            "default",
        ]
        # Omit ``--max-turns`` entirely when uncapped so the run is bounded only
        # by the ``budget_seconds`` alarm; pass it through when a cap is set.
        if self.max_turns is not None:
            argv += ["--max-turns", str(self.max_turns)]
        if self.model is not None:
            argv += ["--model", self.model]
        if self.json_schema is not None:
            argv += ["--json-schema", json.dumps(self.json_schema, sort_keys=True)]
        return argv

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
                "the Claude headless provider runs only jailed: it needs the per-run "
                "ExecutionCapability and a lowered ConfinementSpec."
            )
        prompt = _provider_prompt(self.prompt, task_body, args, "ClaudeHeadlessProvider")
        cli = shutil.which("claude")
        if cli is None:
            raise RuntimeError("claude CLI not found on PATH — see the package README runbook note")
        invocation_id = _invocation_id(self.provider_id, execution)
        sequence = count()
        resolution = _resolve_claude_auth_diagnostic()
        auth_mode, login_blob = resolution.mode, resolution.blob
        auth_payload = {
            "auth_mode": auth_mode or "none",
            "auth_source": resolution.source,
            "auth_status": resolution.status,
        }
        started = ProviderEvent(
            kind=PROVIDER_INVOCATION_STARTED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next(sequence),
            event_id=f"{invocation_id}:started",
            model=self.model or "claude-code-cli",
            payload={
                "prompt_digest": digest_jsonable({"prompt": prompt}),
                "permission_mode": "bypassPermissions",
                "tools": "default",
                "max_turns": self.max_turns,
                **auth_payload,
            },
        )
        # Pre-launch: a jailed body with no seedable/valid login authenticates
        # against nothing (HOME/CLAUDE_CONFIG_DIR are redirected into an empty
        # scratch), so refuse the known-doomed run *before* spending a confined
        # launch — unless SHEPHERD_ALLOW_KEYLESS_CLAUDE opts a wrapper in. The
        # refusal is a preflight, not a jail denial: `launch_attempted` is False
        # and `launch_confined` is never called.
        preflight = _claude_preflight_refusal(resolution)
        if preflight is not None and not os.environ.get("SHEPHERD_ALLOW_KEYLESS_CLAUDE"):
            classification, error_type, message = preflight
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model or "claude-code-cli",
                payload={
                    "error_type": error_type,
                    "failure_classification": classification,
                    "launch_attempted": False,
                    **auth_payload,
                    **redacted_text_payload(message, field="error"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))
        scratch = Path(execution.working_path) / self._SCRATCH
        for sub in ("home", "config", "tmp"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        if login_blob is not None:
            # Re-seed the host CLI's sign-in into the redirected config so a
            # subscription login authenticates the jailed body. The scratch is
            # scrubbed below before the delta is captured, so the credential
            # never enters a retained output.
            cred_path = scratch / "config" / ".credentials.json"
            cred_path.write_bytes(login_blob)
            cred_path.chmod(0o600)
        try:
            proc = execution.launch_confined(self.command_argv(execution.working_path, cli, prompt), confinement)
        finally:
            # D3 scrub: before prepare_bound's supervised after-scan and before the
            # wrap merges — the captured delta is the agent's writes, not housekeeping.
            shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            # Fail closed on scrub residue: the claude scratch holds the seeded
            # `.credentials.json`, so surviving residue in the captured delta is
            # worse than the hermes case (§4.7 parity with the loud post-scrub).
            message = scratch_residue_message(self._SCRATCH)
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model or "claude-code-cli",
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
            # Two positively identified budget stops map to the trace-preserving
            # Exhausted outcome rather than an ambiguous refusal: the CLI's own
            # turn-exhaustion report (prose + structured terminal_reason), and the
            # ``budget_seconds`` alarm kill (SIGALRM → rc -14; the perl ``alarm`` in
            # the argv is the only SIGALRM source on this path). The started
            # bookend rides the exception's events channel (§4.7) so an exhausted
            # run keeps its evidence instead of discarding it with the budget.
            if _signals_max_turns_exhaustion(signal):
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(f"max turns reached ({self.max_turns})", provider_events=(started,))
            if proc.returncode == -14:
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(
                    _budget_exhausted_message(self.budget_seconds, proc.stdout, proc.stderr),
                    provider_events=(started,),
                )
            # Otherwise: parse the CLI's own result envelope so the raised error and
            # the recorded trace name the cause (e.g. not-logged-in, org 403) instead
            # of a blind tail-slice that drops it. The full stdout is kept in the event.
            diagnosis = _diagnose_claude_cli_failure(proc.returncode, proc.stdout, proc.stderr)
            message = f"confined body refused (rc={proc.returncode}): {diagnosis.summary}"
            if diagnosis.remedy:
                message += f"\n  → {diagnosis.remedy}"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model or "claude-code-cli",
                payload={
                    "returncode": proc.returncode,
                    "error_type": "ConfinedProcessRefused",
                    "failure_classification": diagnosis.classification,
                    "cli_is_error": diagnosis.cli_is_error,
                    "cli_api_error_status": diagnosis.cli_api_error_status,
                    "cli_terminal_reason": diagnosis.cli_terminal_reason,
                    "cli_assistant_error": diagnosis.cli_assistant_error,
                    **redacted_text_payload(message, field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                    **redacted_text_payload(diagnosis.cli_result or "", field="cli_result"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))
        sequence_start = next(sequence)
        try:
            parsed = _provider_result_from_claude_stdout(
                proc.stdout or "",
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                model=self.model or "claude-code-cli",
                sequence_start=sequence_start,
            )
        except Exception as exc:
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=sequence_start,
                event_id=f"{invocation_id}:failed",
                model=self.model or "claude-code-cli",
                payload={
                    "error_type": type(exc).__name__,
                    **redacted_text_payload(str(exc), field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                },
            )
            raise ProviderInvocationError(str(exc), provider_events=(started, failed)) from exc
        next_sequence = sequence_start + len(parsed.events)
        if self.json_schema is not None and not parsed.structured_output:
            # The schema was demanded but the success envelope carried no
            # validated object — surface the refusal instead of handing the
            # caller an empty typed result it would misread as "no output".
            message = "claude CLI returned no structured_output for the demanded --json-schema"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next_sequence,
                event_id=f"{invocation_id}:failed",
                model=self.model or "claude-code-cli",
                payload={
                    "returncode": proc.returncode,
                    "error_type": "StructuredOutputMissing",
                    **redacted_text_payload(message, field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))
        completed = ProviderEvent(
            kind=PROVIDER_INVOCATION_COMPLETED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next_sequence,
            event_id=f"{invocation_id}:completed",
            model=str(parsed.metadata.get("model") or self.model or "claude-code-cli"),
            payload={
                "returncode": proc.returncode,
                "session_id": parsed.session_id or "",
                **redacted_text_payload(proc.stdout or "", field="stdout"),
                **redacted_text_payload(proc.stderr or "", field="stderr"),
            },
        )
        result = ProviderInvocationResult(
            output_text=parsed.output_text,
            structured_output=parsed.structured_output,
            session_id=parsed.session_id,
            usage=parsed.usage,
            events=(started, *parsed.events, completed),
            metadata=parsed.metadata,
        )
        return ExecutionProviderResult(
            outcome=provider_invocation_outcome(
                result,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
            ),
            provider_events=result.events,
        )


def probe_claude_auth(*, budget_seconds: int = 30) -> tuple[bool, str]:
    """Check Claude CLI auth under Shepherd's scrubbed-config conditions; return ``(ok, detail)``.

    Runs a minimal ``claude -p`` in the **parent** (not through the jail — no
    ``launch_confined``) under the provider's scrubbed-config + seeded-credential
    conditions (the auth-relevant half of a real run) and classifies the outcome
    with the same envelope parser the run path uses — the authoritative counterpart
    to the offline ``claude_auth_status``. Because it does not run under the jail, a
    pass does not rule out jail-only failure modes (e.g. a token that expires between
    probe and run, whose Seatbelt-blocked keychain refresh only bites the confined
    body). Reaches the network and may briefly call the model. Never raises.
    """
    cli = shutil.which("claude")
    if cli is None:
        return False, "`claude` not found on PATH"
    resolution = _resolve_claude_auth_diagnostic()
    auth_mode, login_blob = resolution.mode, resolution.blob
    if auth_mode is None:
        return False, _keyless_detail(resolution)
    provider = ClaudeHeadlessProvider(
        prompt="Reply with the single word: ok", max_turns=1, budget_seconds=budget_seconds
    )
    with tempfile.TemporaryDirectory(prefix="shepherd-claude-probe-") as tmp:
        working = Path(tmp)
        scratch = working / ClaudeHeadlessProvider._SCRATCH
        for sub in ("home", "config", "tmp"):
            (scratch / sub).mkdir(parents=True, exist_ok=True)
        if login_blob is not None:
            cred = scratch / "config" / ".credentials.json"
            cred.write_bytes(login_blob)
            cred.chmod(0o600)
        try:
            proc = subprocess.run(
                provider.command_argv(working, cli),
                capture_output=True,
                text=True,
                timeout=budget_seconds + 15,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"probe timed out after ~{budget_seconds}s"
        except Exception as exc:  # noqa: BLE001 — a probe that cannot launch is a failed probe, not a crash
            return False, f"probe could not launch: {exc}"
    if proc.returncode == 0:
        try:
            result_event, _events = _parse_claude_cli_output(proc.stdout or "")
        except Exception:  # noqa: BLE001 — unparseable-but-zero-exit is treated as authenticated
            return True, f"authenticated ({auth_mode})"
        if result_event.get("is_error"):
            diagnosis = _diagnose_claude_cli_failure(proc.returncode, proc.stdout, proc.stderr)
            return False, diagnosis.summary
        return True, f"authenticated ({auth_mode})"
    diagnosis = _diagnose_claude_cli_failure(proc.returncode, proc.stdout, proc.stderr)
    return False, diagnosis.summary or f"claude exited rc={proc.returncode}"
