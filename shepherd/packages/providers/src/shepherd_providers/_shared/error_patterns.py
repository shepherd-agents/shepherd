"""Provider-local error pattern matching and suggestion generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorPattern:
    """Pattern definition for error matching and suggestions."""

    name: str
    exception_types: tuple[type[Exception], ...] = ()
    string_patterns: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    priority: int = 0


ERROR_PATTERNS: tuple[ErrorPattern, ...] = (
    ErrorPattern(
        name="auth_invalid_key",
        string_patterns=(
            "invalid api key",
            "invalid x-api-key",
            "authentication failed",
            "unauthorized",
            "401",
        ),
        suggestions=(
            "Check ${API_KEY_VAR} environment variable is set correctly",
            "Verify API key has not expired or been revoked",
            "Ensure key has appropriate permissions for this operation",
        ),
        priority=100,
    ),
    ErrorPattern(
        name="auth_missing_key",
        string_patterns=(
            "api key required",
            "missing api key",
            "no api key",
        ),
        suggestions=(
            "Set ${API_KEY_VAR} environment variable",
            "Pass api_key parameter when creating the provider",
        ),
        priority=100,
    ),
    ErrorPattern(
        name="rate_limit",
        string_patterns=(
            "rate limit",
            "rate_limit",
            "too many requests",
            "429",
            "quota exceeded",
        ),
        suggestions=(
            "Wait and retry with exponential backoff",
            "Reduce request frequency or batch operations",
            "Consider upgrading API tier for higher limits",
        ),
        priority=90,
    ),
    ErrorPattern(
        name="content_policy",
        string_patterns=(
            "content policy",
            "content_policy",
            "safety",
            "blocked",
            "refused",
            "harmful",
        ),
        suggestions=(
            "Review prompt content for policy violations",
            "Rephrase request to avoid triggering content filters",
            "Check if system prompt sets appropriate boundaries",
        ),
        priority=85,
    ),
    ErrorPattern(
        name="buffer_overflow",
        string_patterns=(
            "buffer size",
            "maximum buffer",
            "context length",
            "token limit",
            "too long",
            "max_tokens",
        ),
        suggestions=(
            "Reduce prompt size or conversation history",
            "Use session.fork() to start fresh with summarized context",
            "Check for large tool outputs that should be truncated",
            "Call stream.debug_summary() to see transcript size",
        ),
        priority=80,
    ),
    ErrorPattern(
        name="subprocess_failed",
        string_patterns=(
            "exit code 1",
            "exit code",
            "command failed",
            "subprocess",
            "process exited",
            "non-zero exit",
        ),
        suggestions=(
            "Check stderr in the error details for the actual failure",
            "Try again in a fresh owner-path runtime scope",
            "Verify tool ${TOOL_NAME} has correct permissions/dependencies",
            "Run the command manually to reproduce the error",
        ),
        priority=70,
    ),
    ErrorPattern(
        name="timeout",
        string_patterns=(
            "timeout",
            "timed out",
            "deadline exceeded",
            "took too long",
        ),
        suggestions=(
            "Increase timeout configuration if operation is expected to be slow",
            "Check network connectivity",
            "Consider breaking operation into smaller steps",
        ),
        priority=60,
    ),
    ErrorPattern(
        name="connection",
        string_patterns=(
            "connection refused",
            "connection reset",
            "connection error",
            "network",
            "unreachable",
            "dns",
            "econnrefused",
            "enotfound",
        ),
        suggestions=(
            "Check network connectivity",
            "Verify API endpoint is correct and accessible",
            "Check if firewall/proxy is blocking the connection",
            "Retry after a brief delay",
        ),
        priority=50,
    ),
    ErrorPattern(
        name="session_invalid",
        string_patterns=(
            "session not found",
            "invalid session",
            "session expired",
            "session_id",
        ),
        suggestions=(
            "Session may have expired - create a new session",
            "Open a fresh runtime scope or workspace before retrying",
            "Check session_id: ${SESSION_ID}",
        ),
        priority=40,
    ),
    ErrorPattern(
        name="sdk_generic",
        string_patterns=(),
        suggestions=(
            "Call stream.debug_summary() for execution timeline",
            "Check the full traceback for root cause",
            "Verify SDK version compatibility",
        ),
        priority=0,
    ),
)


def _substitute_template(
    template: str,
    *,
    session_id: str | None,
    last_tool: Any | None,
    provider: str | None,
) -> str:
    """Substitute template variables in suggestion text."""
    result = template

    api_key_vars = {
        "claude": "ANTHROPIC_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "orcarouter": "ORCAROUTER_API_KEY",
    }
    api_key_var = api_key_vars.get(provider or "", "API_KEY")
    result = result.replace("${API_KEY_VAR}", api_key_var)

    if session_id:
        result = result.replace("${SESSION_ID}", session_id)
    elif "${SESSION_ID}" in result:
        return ""

    tool_name = getattr(last_tool, "name", None) if last_tool else None
    return result.replace("${TOOL_NAME}", tool_name) if tool_name else result.replace("${TOOL_NAME}", "the tool")


def suggest_fixes(
    error: Exception | str,
    *,
    session_id: str | None = None,
    last_tool: Any | None = None,
    provider: str | None = None,
) -> list[str]:
    """Generate actionable suggestions based on provider-local error patterns."""
    error_str = str(error).lower()
    error_type = type(error) if isinstance(error, Exception) else None

    matched_patterns: list[ErrorPattern] = []

    for pattern in ERROR_PATTERNS:
        if error_type and pattern.exception_types and isinstance(error, pattern.exception_types):
            matched_patterns.append(pattern)
            continue

        if pattern.string_patterns:
            for substr in pattern.string_patterns:
                if substr in error_str:
                    matched_patterns.append(pattern)
                    break
        elif pattern.priority == 0:
            matched_patterns.append(pattern)

    matched_patterns.sort(key=lambda p: p.priority, reverse=True)

    suggestions: list[str] = []
    seen: set[str] = set()

    for pattern in matched_patterns[:2]:
        for suggestion in pattern.suggestions:
            substituted = _substitute_template(
                suggestion,
                session_id=session_id,
                last_tool=last_tool,
                provider=provider,
            )
            if substituted and substituted not in seen:
                suggestions.append(substituted)
                seen.add(substituted)

    return suggestions


__all__ = [
    "ERROR_PATTERNS",
    "ErrorPattern",
    "suggest_fixes",
]
