"""Claude CLI output: stream-json parsing, event projection, failure diagnosis.

Shared claude-family infrastructure — consumed by the headless provider, the
legacy CLI-direct provider, and the auth probe; not owned by any one of them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from shepherd_dialect.provider_capabilities import canonical_tool_payload
from shepherd_dialect.provider_runtime import (
    MODEL_CALL,
    MODEL_TURN,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
    ProviderEvent,
    ProviderInvocationResult,
    digest_jsonable,
    redacted_text_payload,
)


class ClaudeProviderOutputError(RuntimeError):
    """Raised when Claude CLI output cannot be converted to provider events."""


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


_ACCESS_DENIED_SIGNALS = (
    "disabled claude subscription access",
    "disabled subscription access",
    "access denied",
    "not authorized",
    "does not have access",
    "permission_error",
)


def _diagnose_claude_cli_failure(returncode: int, stdout: str | None, stderr: str | None) -> _ClaudeCliFailureDiagnosis:
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
    # Present when the argv demanded ``--json-schema``: the CLI validates the
    # object against the schema before emitting it, so lift it as-is.
    raw_structured = payload.get("structured_output")
    structured_output = dict(raw_structured) if isinstance(raw_structured, Mapping) else {}
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
        structured_output=structured_output,
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
    # Synthetic ids for id-less starts, oldest first: an id-less result pairs
    # with the oldest open one instead of minting a fresh (never-matching)
    # fallback (§4.7 parity with the hermes queue). Claude's stream-json carries
    # ids in practice, so this only governs the degraded/id-less path.
    pending_fallback: list[str] = []
    fallback_index = 0
    sequence = sequence_start
    for stream_event in stream_events:
        for block in _content_blocks(stream_event):
            block_type = block.get("type")
            if block_type == "tool_use":
                raw_id = block.get("id")
                if raw_id:
                    tool_call_id = str(raw_id)
                else:
                    fallback_index += 1
                    tool_call_id = f"claude-tool-{fallback_index}"
                    pending_fallback.append(tool_call_id)
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
                raw_id = block.get("tool_use_id") or block.get("id")
                if raw_id:
                    tool_call_id = str(raw_id)
                elif pending_fallback:
                    tool_call_id = pending_fallback.pop(0)
                else:
                    fallback_index += 1
                    tool_call_id = f"claude-tool-{fallback_index}"
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
