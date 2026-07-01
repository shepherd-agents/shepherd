"""Execution providers the dialect ships.

Every provider in this module is an executor behind ``runtime.run``: the task
prompt/configuration is sent to a confined provider process, and VcsCore captures
workspace changes from the run working path after that process exits. Provider
events are semantic evidence only, not workspace authority.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect.provider_capabilities import (
    BASH,
    CANONICAL_WORKSPACE_TOOL_NAMES,
    AgentProviderCapabilities,
    canonical_tool_payload,
)
from shepherd_dialect.provider_runtime import (
    MODEL_CALL,
    MODEL_TURN,
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED,
    PROVIDER_INVOCATION_STARTED,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
    ExecutionProviderResult,
    ProviderEvent,
    ProviderInvocationError,
    ProviderInvocationResult,
    digest_jsonable,
    provider_invocation_outcome,
    redacted_text_payload,
)
from shepherd_dialect.provider_worker import parse_provider_worker_output

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext

__all__ = [
    "ClaudeAgentProvider",
    "ClaudeApiProvider",
    "ClaudeHeadlessProvider",
    "CodexAgentProvider",
    "DeterministicFakeProvider",
]


class ClaudeProviderOutputError(RuntimeError):
    """Raised when Claude CLI output cannot be converted to provider events."""


class CodexProviderError(RuntimeError):
    """Raised when Codex SDK output cannot be used by this integration path."""


_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def claude_auth_mode() -> str | None:
    """Return how the jailed Claude lane can authenticate on this host, or ``None``.

    - ``"api_key"``: ``ANTHROPIC_API_KEY`` is set (passes through the jail's env block).
    - ``"oauth_token"``: ``CLAUDE_CODE_OAUTH_TOKEN`` is set (same passthrough).
    - ``"subscription_login"``: the host's ``claude`` CLI is signed in and credential
      seeding is enabled; the provider copies the login into the scrubbed scratch
      config at launch. Set ``SHEPHERD_NO_CREDENTIAL_SEEDING=1`` to disable.
    """
    mode, _ = _resolve_claude_auth()
    return mode


def _resolve_claude_auth() -> tuple[str | None, bytes | None]:
    """Return ``(auth_mode, login_blob)``; the blob is set only when seeding applies."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key", None
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "oauth_token", None
    if os.environ.get("SHEPHERD_NO_CREDENTIAL_SEEDING"):
        return None, None
    blob = _read_host_claude_login()
    if blob is not None:
        return "subscription_login", blob
    return None, None


def _read_host_claude_login() -> bytes | None:
    """Return the host ``claude`` CLI's login credentials, or ``None``. Never raises.

    The jail redirects ``CLAUDE_CONFIG_DIR`` into an empty scratch, which strips the
    CLI's sign-in state; these credentials are re-seeded into that scratch so a
    subscription login works exactly like an env-carried key. Locations are Claude
    Code internals and may shift across CLI versions — every path here fails soft
    (the run then proceeds keyless and the CLI reports not-logged-in).
    """
    try:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        candidates = [Path(config_dir) / ".credentials.json"] if config_dir else []
        candidates.append(Path.home() / ".claude" / ".credentials.json")
        for candidate in candidates:
            if candidate.is_file():
                return candidate.read_bytes()
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return bytes(proc.stdout.strip())
    except Exception:  # noqa: BLE001 — fail soft: no login state means env auth or a clean CLI error
        return None
    return None


def _signals_max_turns_exhaustion(signal: str) -> bool:
    """True when a nonzero-exit ``signal`` is the CLI's turn-limit stop.

    The headless CLI reports turn exhaustion two ways in its combined
    stdout+stderr: the human-readable ``Reached maximum number of turns (N)``
    and the structured stream-json ``"terminal_reason":"max_turns"``. Match
    either so the stop maps to a semantic ``BudgetExhausted`` (→ ``Exhausted``
    outcome, trace and retained artifacts preserved) rather than an ambiguous
    ``Failed`` refusal. (The earlier ``"Reached max turns"`` probe matched
    neither real form, so turn exhaustion was silently misclassified.)
    """
    return "maximum number of turns" in signal or '"terminal_reason":"max_turns"' in signal


def _codex_runner_source() -> str:
    """Return the jailed Codex SDK worker source copied into run scratch."""
    return resources.files("shepherd_dialect.workers").joinpath("codex_runner.mjs").read_text(encoding="utf-8")


def _claude_agent_sdk_worker_source() -> str:
    """Return the jailed Claude Agent SDK worker source copied into run scratch."""
    return (
        resources.files("shepherd_dialect.workers")
        .joinpath("claude_agent_sdk_worker.py")
        .read_text(encoding="utf-8")
    )


def _provider_prompt(
    provider_prompt: str,
    task_body: Callable[..., Any] | None,
    args: Mapping[str, Any],
    provider_name: str,
) -> str:
    if provider_prompt:
        return provider_prompt
    if task_body is None:
        raise ValueError(f"{provider_name} needs a prompt or task body")
    from shepherd_dialect.task_meta import task_prompt

    prompt_body = getattr(task_body, "__shepherd_prompt_body__", task_body)
    prompt_args = getattr(task_body, "__shepherd_prompt_args__", args)
    return task_prompt(prompt_body, dict(prompt_args))


@dataclass(frozen=True)
class CodexAgentProvider:
    """Codex SDK provider facade for the current integration path.

    The provider never installs SDK dependencies. It runs a tiny Node runner
    through ``ExecutionCapability.launch_confined(command, confinement)`` and
    expects ``sdk_module`` to be resolvable in that environment. Auth and Codex
    config are inherited from the environment/Codex home; secrets are not
    serialized into the scratch payload or provider events.
    """

    provider_id: str = "codex-sdk"
    prompt: str = ""
    model: str = "gpt-5.4"
    sdk_module: str = "@openai/codex-sdk"
    codex_path: str | None = None
    node_path: str = "node"
    sandbox_mode: str = "danger-full-access"
    approval_policy: str = "never"
    model_reasoning_effort: str = "medium"
    network_access_enabled: bool = True
    web_search_mode: str = "live"
    thread_id: str | None = None
    output_schema: Mapping[str, Any] | None = None
    base_url: str | None = None
    budget_seconds: int = 300
    auto_install_sdk: bool = False

    _SCRATCH = ".codex-scratch"

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="agent_sdk_worker",
            confined=True,
            network_required=True,
            structured_output=True,
            session_resume=True,
            workspace_tools=frozenset({BASH}),
            custom_tools=False,
            mcp=False,
        )

    def command_argv(self, runner_path: Path | str, payload_path: Path | str, node: str) -> list[str]:
        """The full jailed argv. Dependency resolution happens inside Node."""
        return [
            "/usr/bin/perl",
            "-e",
            "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}",
            str(self.budget_seconds),
            node,
            str(runner_path),
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
                "the Codex SDK provider runs only jailed: it needs the per-run "
                "ExecutionCapability and a lowered ConfinementSpec."
            )
        prompt = _provider_prompt(self.prompt, task_body, args, "CodexAgentProvider")
        node = shutil.which(self.node_path) if Path(self.node_path).name == self.node_path else self.node_path
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
                "sdk_module": self.sdk_module if not Path(self.sdk_module).is_absolute() else Path(self.sdk_module).name,
                "sandbox_mode": self.sandbox_mode,
                "approval_policy": self.approval_policy,
                "reasoning_effort": self.model_reasoning_effort,
                "network_access_enabled": self.network_access_enabled,
                "web_search_mode": self.web_search_mode,
                "thread_id_present": self.thread_id is not None,
                "output_schema_present": self.output_schema is not None,
                "auto_install_sdk": self.auto_install_sdk,
                "codex_home_redirected": True,
            },
        )
        if node is None:
            message = f"node executable not found on PATH: {self.node_path}"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model,
                payload={
                    "error_type": "CodexProviderError",
                    **redacted_text_payload(message, field="error"),
                },
            )
            raise ProviderInvocationError(message, provider_events=(started, failed))

        scratch = Path(execution.working_path) / self._SCRATCH
        runner_path = scratch / "runner.mjs"
        payload_path = scratch / "payload.json"
        scratch.mkdir(parents=True, exist_ok=True)
        runner_path.write_text(_codex_runner_source(), encoding="utf-8")
        payload_path.write_text(
            json.dumps(self._runner_payload(execution.working_path, prompt), sort_keys=True),
            encoding="utf-8",
        )
        try:
            proc = execution.launch_confined(
                self.command_argv(runner_path, payload_path, node),
                confinement,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        if proc.returncode != 0:
            signal = ((proc.stderr or "") + (proc.stdout or "")).strip()
            message = f"Codex SDK execution failed (rc={proc.returncode}): {signal[-300:]}"
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=next(sequence),
                event_id=f"{invocation_id}:failed",
                model=self.model,
                payload={
                    "returncode": proc.returncode,
                    "error_type": "CodexProviderError",
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

        next_sequence = sequence_start + len(parsed.events)
        completed = ProviderEvent(
            kind=PROVIDER_INVOCATION_COMPLETED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=next_sequence,
            event_id=f"{invocation_id}:completed",
            model=self.model,
            payload={
                "returncode": proc.returncode,
                "thread_id": parsed.result.session_id or "",
                "sandbox_mode": str(parsed.result.metadata.get("sandbox_mode") or self.sandbox_mode),
                "network_access_enabled": bool(
                    parsed.result.metadata.get("network_access_enabled", self.network_access_enabled)
                ),
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

    def _runner_payload(self, working_path: Path | str, prompt: str) -> dict[str, Any]:
        return {
            "sdkModule": self.sdk_module,
            "codexPath": self.codex_path,
            "baseUrl": self.base_url,
            "model": self.model,
            "workingDirectory": str(Path(working_path)),
            "codexHome": str(Path(working_path) / self._SCRATCH / "codex-home"),
            "sandboxMode": self.sandbox_mode,
            "approvalPolicy": self.approval_policy,
            "reasoningEffort": self.model_reasoning_effort,
            "networkAccessEnabled": self.network_access_enabled,
            "webSearchMode": self.web_search_mode,
            "outputSchema": dict(self.output_schema) if self.output_schema else None,
            "prompt": prompt,
            "threadId": self.thread_id,
        }


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
        script = (
            "import pathlib\n"
            f"pathlib.Path({self.artifact!r}).write_text({self.content!r})\n"
        )
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
                f"confined body refused (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[-300:]}"
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
            "/usr/bin/perl",
            "-e",
            "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}",
            str(self.budget_seconds),
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
        if proc.returncode != 0:
            signal = ((proc.stderr or "") + (proc.stdout or "")).strip()
            if _signals_max_turns_exhaustion(signal):
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(f"max turns reached ({self.max_turns})")
            raise RuntimeError(f"confined body refused (rc={proc.returncode}): {signal[-300:]}")
        return {
            "status": "ok",
            "provider": self.provider_id,
            "reply": (proc.stdout or "").strip()[-400:],
        }


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
      (execution-boundary.md §7).
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

    _SCRATCH = ".claude-scratch"

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="headless_cli",
            confined=True,
            network_required=True,
            structured_output=False,
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
            "/usr/bin/perl",
            "-e",
            "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}",
            str(self.budget_seconds),
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
        auth_mode, login_blob = _resolve_claude_auth()
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
                "auth_mode": auth_mode or "none",
            },
        )
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
        if proc.returncode != 0:
            signal = ((proc.stderr or "") + (proc.stdout or "")).strip()
            # The one positively identified budget stop: the CLI reports turn
            # exhaustion distinguishably (prose + structured terminal_reason) —
            # D3's Exhausted emitter. Ambiguous stops (incl. alarm kills, rc -14)
            # stay refusals.
            if _signals_max_turns_exhaustion(signal):
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(f"max turns reached ({self.max_turns})")
            message = f"confined body refused (rc={proc.returncode}): {signal[-300:]}"
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
                    **redacted_text_payload(message, field="error"),
                    **redacted_text_payload(proc.stdout or "", field="stdout"),
                    **redacted_text_payload(proc.stderr or "", field="stderr"),
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
            session_resume=True,
            workspace_tools=CANONICAL_WORKSPACE_TOOL_NAMES,
            custom_tools=False,
            mcp=False,
        )

    def command_argv(self, worker_path: Path | str, payload_path: Path | str, python: str) -> list[str]:
        return [
            "/usr/bin/perl",
            "-e",
            "alarm shift @ARGV; exec @ARGV or die qq{exec: $!}",
            str(self.budget_seconds),
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


def _invocation_id(provider_id: str, execution: Any) -> str:
    identity = getattr(execution, "identity", None)
    scope_instance_id = getattr(identity, "scope_instance_id", None)
    scope_name = getattr(identity, "scope_name", None)
    suffix = scope_instance_id or scope_name or "unknown"
    return f"{provider_id}:{suffix}"


def _provider_result_from_claude_stdout(
    stdout: str,
    *,
    provider_id: str,
    invocation_id: str,
    model: str,
    sequence_start: int,
) -> ProviderInvocationResult:
    payload, stream_events = _parse_claude_cli_output(stdout)
    output_text = str(payload.get("result") or payload.get("finalResponse") or payload.get("response") or "")
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    session_id = payload.get("session_id") or payload.get("sessionId")
    served_model = str(payload.get("model") or model)
    cost_usd = payload.get("total_cost_usd")
    metadata: dict[str, object] = {"model": served_model}
    if isinstance(cost_usd, (int, float)):
        metadata["cost_usd"] = float(cost_usd)
    events = _claude_stream_events_to_provider_events(
        stream_events,
        provider_id=provider_id,
        invocation_id=invocation_id,
        model=served_model,
        sequence_start=sequence_start,
    )
    final_sequence = sequence_start + len(events)
    if output_text or usage:
        events = (
            *events,
            ProviderEvent(
                kind=MODEL_CALL,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=final_sequence,
                event_id=f"{invocation_id}:model-call:{final_sequence}",
                model=served_model,
                payload={
                    "usage": dict(usage),
                    "duration_ms": _number(payload.get("duration_ms"), 0.0),
                    "duration_api_ms": _number(payload.get("duration_api_ms"), 0.0),
                    **redacted_text_payload(output_text, field="output_text"),
                },
            ),
        )
        final_sequence += 1
    if output_text:
        events = (
            *events,
            ProviderEvent(
                kind=MODEL_TURN,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=final_sequence,
                event_id=f"{invocation_id}:model-turn:{final_sequence}",
                model=served_model,
                payload=redacted_text_payload(output_text, field="text"),
            ),
        )
    return ProviderInvocationResult(
        output_text=output_text,
        session_id=session_id if isinstance(session_id, str) else None,
        usage=dict(usage),
        events=events,
        metadata=metadata,
    )


def _parse_claude_cli_output(stdout: str) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    events = _parse_json_events(stdout)
    if len(events) == 1:
        if events[0].get("type") not in (None, "result"):
            raise ClaudeProviderOutputError("claude CLI single JSON payload was not a result event")
        return events[0], ()
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if result is None:
        raise ClaudeProviderOutputError("claude CLI stream-json output did not include a result event")
    return result, tuple(events)


def _parse_json_events(stdout: str) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        raise ClaudeProviderOutputError("claude CLI returned empty stdout")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        events: list[dict[str, Any]] = []
        non_json_lines: list[str] = []
        for line in [line.strip() for line in text.splitlines() if line.strip()]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                non_json_lines.append(line)
                continue
            if not isinstance(event, dict):
                raise ClaudeProviderOutputError("claude CLI returned a non-object JSON stream event") from None
            events.append(event)
        if not events:
            raise ClaudeProviderOutputError(f"claude CLI returned non-JSON stdout: {text[:500]}") from None
        if non_json_lines and len(events) > 1:
            raise ClaudeProviderOutputError(f"claude CLI returned non-JSON stream line: {non_json_lines[0][:500]}") from None
        return events
    if not isinstance(parsed, dict):
        raise ClaudeProviderOutputError("claude CLI returned a non-object JSON payload")
    return [parsed]


def _claude_stream_events_to_provider_events(
    stream_events: tuple[dict[str, Any], ...],
    *,
    provider_id: str,
    invocation_id: str,
    model: str,
    sequence_start: int,
) -> tuple[ProviderEvent, ...]:
    events: list[ProviderEvent] = []
    started: dict[str, str] = {}
    fallback_index = 0
    sequence = sequence_start
    for stream_event in stream_events:
        for block in _content_blocks(stream_event):
            block_type = block.get("type")
            if block_type == "tool_use":
                fallback_index += 1
                tool_call_id = str(block.get("id") or f"claude-tool-{fallback_index}")
                tool_name = str(block.get("name") or "tool")
                params = _tool_params(block.get("input"))
                started[tool_call_id] = tool_name
                events.append(
                    ProviderEvent(
                        kind=TOOL_CALL_STARTED,
                        provider_id=provider_id,
                        invocation_id=invocation_id,
                        sequence=sequence,
                        event_id=f"{invocation_id}:tool-start:{sequence}",
                        model=model,
                        tool_call_id=tool_call_id,
                        payload={
                            **canonical_tool_payload(tool_name),
                            "params_digest": digest_jsonable(params),
                        },
                    )
                )
                sequence += 1
            elif block_type == "tool_result":
                fallback_index += 1
                tool_call_id = str(block.get("tool_use_id") or block.get("id") or f"claude-tool-{fallback_index}")
                tool_name = started.get(tool_call_id, "tool")
                output = _tool_output(block.get("content"))
                events.append(
                    ProviderEvent(
                        kind=TOOL_CALL_COMPLETED,
                        provider_id=provider_id,
                        invocation_id=invocation_id,
                        sequence=sequence,
                        event_id=f"{invocation_id}:tool-complete:{sequence}",
                        model=model,
                        tool_call_id=tool_call_id,
                        payload={
                            **canonical_tool_payload(tool_name),
                            "success": not bool(block.get("is_error", False)),
                            **redacted_text_payload(output, field="output"),
                        },
                    )
                )
                sequence += 1
    return tuple(events)


def _content_blocks(event: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    message = event.get("message")
    candidates: list[Any] = []
    if isinstance(message, Mapping):
        candidates.append(message.get("content"))
    candidates.append(event.get("content"))
    candidates.append(event.get("blocks"))

    blocks: list[dict[str, Any]] = []
    for candidate in candidates:
        if isinstance(candidate, list):
            blocks.extend(dict(block) for block in candidate if isinstance(block, Mapping))
        elif isinstance(candidate, Mapping):
            blocks.append(dict(candidate))
    return tuple(blocks)


def _tool_params(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {"input": value}


def _tool_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except TypeError:
        return repr(value)


def _number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def codex_provider_result_from_payload(
    raw: Mapping[str, Any],
    *,
    provider_id: str,
    invocation_id: str,
    model: str,
    sequence_start: int = 0,
) -> ProviderInvocationResult:
    """Convert a recorded Codex SDK payload to native provider events."""
    final_response = str(raw.get("finalResponse") or raw.get("final_response") or "")
    structured_output = raw.get("structuredOutput") or raw.get("structured_output") or {}
    structured_output = dict(structured_output) if isinstance(structured_output, Mapping) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    codex_items = [dict(item) for item in items if isinstance(item, Mapping)]
    events = _codex_items_to_provider_events(
        codex_items,
        provider_id=provider_id,
        invocation_id=invocation_id,
        model=model,
        sequence_start=sequence_start,
    )
    next_sequence = sequence_start + len(events)
    if final_response or usage:
        events = (
            *events,
            ProviderEvent(
                kind=MODEL_CALL,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=next_sequence,
                event_id=f"{invocation_id}:model-call:{next_sequence}",
                model=model,
                payload={
                    "usage": dict(usage),
                    "item_count": len(codex_items),
                    **redacted_text_payload(final_response, field="output_text"),
                },
            ),
        )
        next_sequence += 1
    if final_response:
        events = (
            *events,
            ProviderEvent(
                kind=MODEL_TURN,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=next_sequence,
                event_id=f"{invocation_id}:model-turn:{next_sequence}",
                model=model,
                payload=redacted_text_payload(final_response, field="text"),
            ),
        )
    return ProviderInvocationResult(
        output_text=final_response,
        structured_output=structured_output,
        session_id=str(raw.get("threadId") or raw.get("thread_id") or "") or None,
        usage=dict(usage),
        events=events,
        metadata={
            "model": model,
            "item_count": len(codex_items),
            "sandbox_mode": str(raw.get("sandboxMode") or raw.get("sandbox_mode") or ""),
            "network_access_enabled": bool(raw.get("networkAccessEnabled", False)),
        },
    )


def _codex_items_to_provider_events(
    items: list[dict[str, Any]],
    *,
    provider_id: str,
    invocation_id: str,
    model: str,
    sequence_start: int,
) -> tuple[ProviderEvent, ...]:
    events: list[ProviderEvent] = []
    sequence = sequence_start
    for index, item in enumerate(items, start=1):
        tool = _codex_tool_from_item(item, fallback_id=f"codex-item-{index}")
        if tool is None:
            continue
        tool_call_id, tool_name, params, success, output = tool
        events.append(
            ProviderEvent(
                kind=TOOL_CALL_STARTED,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=sequence,
                event_id=f"{invocation_id}:tool-start:{sequence}",
                model=model,
                tool_call_id=tool_call_id,
                payload={
                    **canonical_tool_payload(tool_name),
                    "params_digest": digest_jsonable(params),
                },
            )
        )
        sequence += 1
        events.append(
            ProviderEvent(
                kind=TOOL_CALL_COMPLETED,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=sequence,
                event_id=f"{invocation_id}:tool-complete:{sequence}",
                model=model,
                tool_call_id=tool_call_id,
                payload={
                    **canonical_tool_payload(tool_name),
                    "success": success,
                    **redacted_text_payload(output, field="output"),
                },
            )
        )
        sequence += 1
    return tuple(events)


def _codex_tool_from_item(
    item: Mapping[str, Any],
    *,
    fallback_id: str,
) -> tuple[str, str, dict[str, Any], bool, str] | None:
    item_type = item.get("type")
    tool_call_id = str(item.get("id") or fallback_id)
    if item_type == "command_execution":
        status = str(item.get("status") or "")
        exit_code = item.get("exit_code")
        return (
            tool_call_id,
            "Bash",
            {"command": str(item.get("command") or "")},
            status == "completed" and (exit_code is None or exit_code == 0),
            str(item.get("aggregated_output") or ""),
        )
    if item_type == "mcp_tool_call":
        server = str(item.get("server") or "mcp")
        tool = str(item.get("tool") or "tool")
        error = item.get("error")
        return (
            tool_call_id,
            f"mcp__{server}__{tool}",
            {"arguments": item.get("arguments")},
            item.get("status") == "completed" and error is None,
            _json_output(item.get("result") if error is None else error),
        )
    if item_type == "web_search":
        return (
            tool_call_id,
            "WebSearch",
            {"query": str(item.get("query") or "")},
            True,
            "",
        )
    if item_type == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        return (
            tool_call_id,
            "FileChange",
            {"changes": changes},
            item.get("status") == "completed",
            _json_output({"changes": changes, "status": item.get("status")}),
        )
    return None


def _json_output(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except TypeError:
        return repr(value)
