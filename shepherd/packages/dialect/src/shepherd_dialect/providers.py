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
import tempfile
import time
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


@dataclass(frozen=True)
class _HostLoginLookup:
    """The result of looking for a host ``claude`` login, with a non-secret trail.

    ``blob`` is the credential bytes (or ``None``); ``attempts`` records each source
    tried as ``(source_class, status)`` — never a path, never credential bytes — so
    a failed resolution can say *which* source class failed and roughly how
    (missing vs unreadable vs keychain-denied/timeout) without leaking secrets.
    """

    blob: bytes | None
    attempts: tuple[tuple[str, str], ...] = ()

    @property
    def source(self) -> str | None:
        """The source class the credential was found in, if any."""
        for source, status in self.attempts:
            if status.endswith("_found"):
                return source
        return None


@dataclass(frozen=True)
class _ClaudeAuthResolution:
    """How the jailed Claude lane would authenticate, with a diagnostic trail.

    Extends the ``(mode, blob)`` pair the run path consumes with a non-secret
    ``status`` and source ``attempts`` so ``doctor``/``probe`` can explain a
    ``mode is None`` verdict (seeding disabled? keychain denied? nothing found?)
    instead of a flat "no credentials".
    """

    mode: str | None
    blob: bytes | None = None
    source: str | None = None
    status: str = "unknown"
    seeding_disabled: bool = False
    attempts: tuple[tuple[str, str], ...] = ()


def _resolve_claude_auth() -> tuple[str | None, bytes | None]:
    """Return ``(auth_mode, login_blob)``; the blob is set only when seeding applies."""
    resolution = _resolve_claude_auth_diagnostic()
    return resolution.mode, resolution.blob


def _resolve_claude_auth_diagnostic() -> _ClaudeAuthResolution:
    """Resolve Claude auth with a non-secret diagnostic trail (see ``_ClaudeAuthResolution``)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _ClaudeAuthResolution("api_key", None, "env_api_key", "env_api_key")
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return _ClaudeAuthResolution("oauth_token", None, "env_oauth_token", "env_oauth_token")
    if os.environ.get("SHEPHERD_NO_CREDENTIAL_SEEDING"):
        return _ClaudeAuthResolution(None, None, None, "seeding_disabled", seeding_disabled=True)
    lookup = _read_host_claude_login()
    if lookup.blob is not None:
        won = next((s for s in lookup.attempts if s[1].endswith("_found")), ("host_login", "found"))
        return _ClaudeAuthResolution("subscription_login", lookup.blob, won[0], won[1], attempts=lookup.attempts)
    status = lookup.attempts[-1][1] if lookup.attempts else "no_credentials"
    return _ClaudeAuthResolution(None, None, None, status, attempts=lookup.attempts)


def _read_host_claude_login() -> _HostLoginLookup:
    """Return the host ``claude`` CLI's login credentials + a source trail. Never raises.

    The jail redirects ``CLAUDE_CONFIG_DIR`` into an empty scratch, which strips the
    CLI's sign-in state; these credentials are re-seeded into that scratch so a
    subscription login works exactly like an env-carried key. Locations are Claude
    Code internals and may shift across CLI versions — every source here fails soft
    (a keyless resolution then makes the public headless provider refuse before
    launch, unless ``SHEPHERD_ALLOW_KEYLESS_CLAUDE`` is set), but each attempt is
    recorded (source class + coarse status, never a path or bytes) so a keyless
    verdict can name *why*. Ambiguous platform signals collapse to ``keychain_failed``
    rather than guessing ``security`` exit-code trivia.
    """
    attempts: list[tuple[str, str]] = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    file_sources: list[tuple[str, Path]] = []
    if config_dir:
        file_sources.append(("configured_config", Path(config_dir) / ".credentials.json"))
    file_sources.append(("default_config", Path.home() / ".claude" / ".credentials.json"))
    for source, candidate in file_sources:
        try:
            if candidate.is_file():
                blob = candidate.read_bytes()
                attempts.append((source, f"{source}_found"))
                return _HostLoginLookup(blob, tuple(attempts))
            attempts.append((source, f"{source}_missing"))
        except Exception:  # noqa: BLE001 — an unreadable source is a recorded miss, not a crash
            attempts.append((source, f"{source}_unreadable"))

    if sys.platform != "darwin":
        attempts.append(("macos_keychain", "unsupported_platform"))
        return _HostLoginLookup(None, tuple(attempts))
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        attempts.append(("macos_keychain", "keychain_timeout"))
        return _HostLoginLookup(None, tuple(attempts))
    except Exception:  # noqa: BLE001 — collapse ambiguous `security` failures, don't guess
        attempts.append(("macos_keychain", "keychain_failed"))
        return _HostLoginLookup(None, tuple(attempts))
    if proc.returncode == 0 and proc.stdout.strip():
        attempts.append(("macos_keychain", "keychain_found"))
        return _HostLoginLookup(bytes(proc.stdout.strip()), tuple(attempts))
    # `security` exits 44 for "not found"; any other nonzero is a denial/other
    # failure. We do not overfit the exact code — not-found vs failed is the
    # useful cut; anything ambiguous collapses to keychain_failed.
    status = "keychain_not_found" if proc.returncode == 44 else "keychain_failed"
    attempts.append(("macos_keychain", status))
    return _HostLoginLookup(None, tuple(attempts))


def _claude_blob_expiry(blob: bytes | None) -> bool | None:
    """Whether a subscription login blob's access token is expired.

    ``True`` = expired, ``False`` = still valid, ``None`` = not determinable
    (missing field / unrecognized shape). Never raises. The blob is Claude Code's
    ``.credentials.json`` / keychain payload: ``{"claudeAiOauth": {"expiresAt": <ms>}}``.
    """
    if not blob:
        return None
    try:
        data = json.loads(blob)
        oauth = data.get("claudeAiOauth") if isinstance(data, Mapping) else None
        expires_at = oauth.get("expiresAt") if isinstance(oauth, Mapping) else None
        if not isinstance(expires_at, (int, float)):
            return None
        return (expires_at / 1000.0) < time.time()
    except Exception:  # noqa: BLE001 — best-effort; an unreadable blob is "not determinable"
        return None


@dataclass(frozen=True)
class ClaudeAuthStatus:
    """The offline readiness verdict for the jailed Claude lane."""

    mode: str | None
    ok: bool
    detail: str


def claude_auth_status() -> ClaudeAuthStatus:
    """Offline verdict on whether the jailed Claude lane can authenticate.

    Cheap and network-free. Env credentials pass; an absent login fails; a
    subscription login is inspected for token expiry — an expired access token
    cannot be refreshed under the jail (keychain write-back is blocked), so it is
    a hard fail. A valid-looking login is reported ``ok`` but *unverified*:
    ``probe_claude_auth`` is the authoritative check. This is what makes a green
    ``doctor`` honest rather than merely "a blob is readable".
    """
    resolution = _resolve_claude_auth_diagnostic()
    mode, blob = resolution.mode, resolution.blob
    if mode == "api_key":
        return ClaudeAuthStatus(mode, True, "ANTHROPIC_API_KEY set")
    if mode == "oauth_token":
        return ClaudeAuthStatus(mode, True, "CLAUDE_CODE_OAUTH_TOKEN set")
    if mode is None:
        return ClaudeAuthStatus(None, False, _keyless_detail(resolution))
    expired = _claude_blob_expiry(blob)
    if expired is True:
        return ClaudeAuthStatus(
            mode,
            False,
            "signed-in `claude` CLI, but the access token is expired — a jailed run cannot "
            "refresh it; run `claude login` or set CLAUDE_CODE_OAUTH_TOKEN",
        )
    unverified = "run `shepherd doctor claude --probe` to authenticate"
    if expired is False:
        return ClaudeAuthStatus(mode, True, f"signed-in `claude` CLI (found, not verified — {unverified})")
    return ClaudeAuthStatus(mode, True, f"signed-in `claude` CLI (found, format unrecognized, not verified — {unverified})")


_AUTH_REMEDY = "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or ANTHROPIC_API_KEY, or sign in with `claude login`"


def _keyless_detail(resolution: _ClaudeAuthResolution) -> str:
    """A sharp offline message for a ``mode is None`` verdict, named by source status.

    Scans the *whole* attempt trail for the most actionable signal rather than only
    the last status: a source that was found-but-unreadable or a keychain that was
    denied/timed out is more useful to surface than a later "not found", which is
    the trail's normal terminal state.
    """
    if resolution.seeding_disabled:
        return f"credential seeding is disabled (SHEPHERD_NO_CREDENTIAL_SEEDING) and no env credential is set — {_AUTH_REMEDY}"
    statuses = {status for _source, status in resolution.attempts} or {resolution.status}
    if "keychain_timeout" in statuses:
        return f"the macOS keychain lookup timed out — {_AUTH_REMEDY}"
    if "keychain_failed" in statuses:
        return f"the macOS keychain lookup was denied or failed — {_AUTH_REMEDY}"
    if any(status.endswith("_unreadable") for status in statuses):
        return f"a `claude` credential file was found but unreadable — {_AUTH_REMEDY}"
    return f"no signed-in `claude` login found — {_AUTH_REMEDY}"


_KEYLESS_ESCAPE = (
    "If a `claude` wrapper authenticates outside Shepherd's known credential routes, "
    "set SHEPHERD_ALLOW_KEYLESS_CLAUDE=1 to launch anyway."
)


def _claude_preflight_refusal(resolution: _ClaudeAuthResolution) -> tuple[str, str, str] | None:
    """``(classification, error_type, message)`` if a jailed launch is known-doomed, else ``None``.

    The public headless provider redirects ``HOME``/``CLAUDE_CONFIG_DIR`` into an
    empty scratch, so a body with no env credential and no seedable host login
    authenticates against nothing — a guaranteed not-logged-in failure — and an
    expired subscription blob cannot be refreshed under the jail. Both are refused
    before launch (unless ``SHEPHERD_ALLOW_KEYLESS_CLAUDE`` is set) so a trace reader
    sees a preflight refusal, not a wasted confined run that reads like a jail denial.
    """
    if resolution.mode is None:
        message = f"Claude CLI auth is not available for a jailed run ({_keyless_detail(resolution)}). {_KEYLESS_ESCAPE}"
        return "auth_missing", "ClaudeAuthMissing", message
    if resolution.mode == "subscription_login" and _claude_blob_expiry(resolution.blob) is True:
        message = (
            "the seeded `claude` subscription login is expired and a jailed run cannot refresh it — "
            "run `claude login` or set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`). If your "
            "`claude` wrapper keeps its real auth outside the standard store, set "
            "SHEPHERD_NO_CREDENTIAL_SEEDING=1 and SHEPHERD_ALLOW_KEYLESS_CLAUDE=1 to skip seeding the "
            "stale blob and launch anyway."
        )
        return "auth_expired", "ClaudeAuthExpired", message
    return None


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
    provider = ClaudeHeadlessProvider(prompt="Reply with the single word: ok", max_turns=1, budget_seconds=budget_seconds)
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


@dataclass(frozen=True)
class _ClaudeCliFailureDiagnosis:
    """A parsed, classified ``claude`` CLI failure.

    Carries the cause, a remedy, and the safe scalar envelope fields worth
    preserving in the trace (never raw JSON).
    """

    classification: str
    summary: str
    remedy: str | None = None
    cli_result: str | None = None
    cli_is_error: bool | None = None
    cli_api_error_status: int | None = None
    cli_terminal_reason: str | None = None
    cli_assistant_error: str | None = None


#: Result-text fingerprints of an authorization/policy denial (an HTTP 403 class),
#: distinct from a not-logged-in auth failure: the credential is valid, the account
#: or organization is not permitted. Re-login does not help these.
_ACCESS_DENIED_SIGNALS = (
    "disabled claude subscription access",
    "disabled subscription access",
    "access denied",
    "not authorized",
    "does not have access",
    "permission_error",
)


def _diagnose_claude_cli_failure(
    returncode: int, stdout: str | None, stderr: str | None
) -> _ClaudeCliFailureDiagnosis:
    """Turn a nonzero ``claude`` CLI exit into an actionable cause + remedy.

    The headless CLI reports real errors *inside* a well-formed stream-json
    result envelope (e.g. ``result: "Not logged in · Please run /login"`` with an
    ``authentication_failed`` assistant message, or an ``api_error_status: 403``
    org-policy denial) and still exits nonzero. A blind tail-slice of that ~3 KB
    envelope surfaces only trailing bookkeeping fields and drops the cause, so
    this parses the ``result`` text and the safe scalar fields and classifies the
    common stops so the raised error and the recorded trace name what actually
    happened. Best-effort: it never raises.
    """
    signal_text = (stderr or "") + (stdout or "")
    lowered = signal_text.lower()
    cli_result: str | None = None
    is_error: bool | None = None
    api_error_status: int | None = None
    terminal_reason: str | None = None
    assistant_error: str | None = None
    try:
        result_event, events = _parse_claude_cli_output(stdout or "")
        raw = result_event.get("result")
        if isinstance(raw, str) and raw.strip():
            cli_result = raw.strip()
        if isinstance(result_event.get("is_error"), bool):
            is_error = result_event["is_error"]
        if isinstance(result_event.get("api_error_status"), int):
            api_error_status = result_event["api_error_status"]
        if isinstance(result_event.get("terminal_reason"), str):
            terminal_reason = result_event["terminal_reason"]
        assistant_error = _first_assistant_error(events or (result_event,))
    except Exception:  # noqa: BLE001 — diagnosis must never mask the original failure
        cli_result = None

    result_lowered = (cli_result or "").lower()
    if "cannot be used with root" in lowered:
        classification = "root_permission"
        remedy: str | None = (
            "the jailed `claude` CLI refuses bypass permissions when run as root; run "
            "as a non-root user, or set IS_SANDBOX=1 if you are intentionally sandboxed"
        )
    elif (
        api_error_status == 403
        or assistant_error == "permission_error"
        or "403" in result_lowered
        or "forbidden" in result_lowered
        or any(sig in result_lowered for sig in _ACCESS_DENIED_SIGNALS)
    ):
        classification = "access_denied"
        remedy = (
            "Claude refused with an authorization error (HTTP 403) — this is an account or "
            "organization policy limit, not a login problem. Use an API key your org permits "
            "(ANTHROPIC_API_KEY) or ask your org admin; re-login will not change it"
        )
    elif (
        "not logged in" in lowered
        or "authentication_failed" in lowered
        or "please run /login" in lowered
        or "invalid api key" in lowered
        or "oauth token has expired" in lowered
    ):
        classification = "auth_failure"
        remedy = (
            "the jailed `claude` CLI is not authenticated — a seeded subscription login "
            "may be missing or expired. Set CLAUDE_CODE_OAUTH_TOKEN (from `claude "
            "setup-token`) or ANTHROPIC_API_KEY, or sign in again with `claude login`"
        )
    else:
        classification = "unknown"
        remedy = None

    stripped = signal_text.strip()
    summary = cli_result or (stripped[-300:] if stripped else f"no output (rc={returncode})")
    return _ClaudeCliFailureDiagnosis(
        classification=classification,
        summary=summary,
        remedy=remedy,
        cli_result=cli_result,
        cli_is_error=is_error,
        cli_api_error_status=api_error_status,
        cli_terminal_reason=terminal_reason,
        cli_assistant_error=assistant_error,
    )


def _first_assistant_error(events: tuple[dict[str, Any], ...]) -> str | None:
    """The first scalar assistant-message ``error`` in a parsed stream, if any."""
    for event in events:
        message = event.get("message") if isinstance(event, Mapping) else None
        err = message.get("error") if isinstance(message, Mapping) else event.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
    return None


def _budget_exhausted_message(budget_seconds: int, stdout: str | None, stderr: str | None) -> str:
    """The ``BudgetExhausted`` message for an alarm kill, with a hung-body hint.

    A ``budget_seconds`` alarm kill (SIGALRM → rc -14) with **no** output at all
    is usually a body that hung before it ever produced a token — a stale ``claude``
    version or a blocked network — which reads misleadingly as "the model ran long".
    Name that case; otherwise the model genuinely ran out of budget.
    """
    produced_output = bool(((stdout or "") + (stderr or "")).strip())
    if produced_output:
        return f"budget exceeded ({budget_seconds}s)"
    return (
        f"budget exceeded ({budget_seconds}s): no output before the alarm — the CLI may have hung "
        "before starting (check for a stale `claude` version or a blocked network)"
    )


def _codex_runner_source() -> str:
    """Return the jailed Codex SDK worker source copied into run scratch."""
    return resources.files("shepherd_dialect.workers").joinpath("codex_runner.mjs").read_text(encoding="utf-8")


def _claude_agent_sdk_worker_source() -> str:
    """Return the jailed Claude Agent SDK worker source copied into run scratch."""
    return (
        resources.files("shepherd_dialect.workers").joinpath("claude_agent_sdk_worker.py").read_text(encoding="utf-8")
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
                "sdk_module": self.sdk_module
                if not Path(self.sdk_module).is_absolute()
                else Path(self.sdk_module).name,
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
        if proc.returncode != 0:
            signal = ((proc.stderr or "") + (proc.stdout or "")).strip()
            # Two positively identified budget stops map to the trace-preserving
            # Exhausted outcome rather than an ambiguous refusal: the CLI's own
            # turn-exhaustion report (prose + structured terminal_reason), and the
            # ``budget_seconds`` alarm kill (SIGALRM → rc -14; the perl ``alarm`` in
            # the argv is the only SIGALRM source on this path).
            if _signals_max_turns_exhaustion(signal):
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(f"max turns reached ({self.max_turns})")
            if proc.returncode == -14:
                from shepherd_dialect.nucleus import BudgetExhausted

                raise BudgetExhausted(_budget_exhausted_message(self.budget_seconds, proc.stdout, proc.stderr))
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
                    **redacted_text_payload(proc.stdout or "", field="stdout", excerpt_limit=4000),
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
            raise ClaudeProviderOutputError(
                f"claude CLI returned non-JSON stream line: {non_json_lines[0][:500]}"
            ) from None
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
