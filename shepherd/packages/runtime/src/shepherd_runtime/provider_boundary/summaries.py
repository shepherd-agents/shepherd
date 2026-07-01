"""Structural transcript summaries for provider-boundary trace payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shepherd_runtime.provider_boundary.payloads import ModelRequest, ModelResponse

__all__ = [
    "summarize_model_failure",
    "summarize_model_request",
    "summarize_model_response",
]


def summarize_model_request(
    request: ModelRequest,
    *,
    provider_id: str | None = None,
    status: str = "requested",
) -> dict[str, object]:
    """Return a redaction-safe structural summary of a provider request."""
    return {
        "provider_id": provider_id,
        "model_id": request.settings.model,
        "model": request.settings.model,
        "status": status,
        "message_count": len(request.messages),
        "message_roles": tuple(message.role for message in request.messages),
        "tool_count": len(request.tools),
        "tool_call_count": 0,
    }


def summarize_model_response(
    response: ModelResponse,
    *,
    provider_id: str | None = None,
    model_id: str | None = None,
    status: str = "returned",
) -> dict[str, object]:
    """Return a redaction-safe structural summary of a provider response."""
    summary: dict[str, object] = {
        "provider_id": provider_id,
        "model_id": model_id,
        "status": status,
        "finish_reason": response.finish_reason,
        "session_present": response.session_id is not None,
        "tool_call_count": len(response.tool_calls),
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_present": response.usage.cost_usd is not None,
        },
    }
    if response.structured_output is not None:
        summary["response_shape"] = "structured_output"
        summary["structured_keys"] = sorted(response.structured_output)
    elif response.text is not None:
        summary["response_shape"] = "text"
        summary["text_length"] = len(response.text)
    else:
        summary["response_shape"] = "tool_calls"
    return summary


def summarize_model_failure(
    exc: BaseException,
    *,
    request: ModelRequest | None = None,
    provider_id: str | None = None,
    status: str = "raised",
) -> dict[str, object]:
    """Return a redaction-safe structural summary of a provider failure."""
    model_id = request.settings.model if request is not None else None
    tool_count = len(request.tools) if request is not None else 0
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "status": status,
        "failure_class": type(exc).__name__,
        "error_type": type(exc).__name__,
        "tool_count": tool_count,
        "tool_call_count": 0,
    }
