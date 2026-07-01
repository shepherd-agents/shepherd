"""D5 provider-boundary payload schemas.

Seven frozen dataclasses with JSON-compatible leaves per
DECISIONS D17 (frozen-dataclass-only at cross-plan boundaries):

- ``ProviderMessage``  role-tagged content block
- ``ToolSpec``         tool advertisement (name, description, schema)
- ``ProviderSettings`` model + provider-specific knobs
- ``ToolCallRecord``   per-tool invocation record
- ``Usage``            token / cost accounting
- ``ModelRequest``     embedded as ``EffectDeclaration("model.call")``
                       payload
- ``ModelResponse``    embedded as ``EffectCapture`` action_payload

``ModelResponse.__post_init__`` enforces mutual exclusion (exactly one
of ``text`` / ``structured_output`` / non-empty ``tool_calls``);
adapters are responsible for normalizing ``text=""`` to ``text=None``
before construction per DECISIONS D15.

Pinned by `docs/design/proposed/260505-plans/CONTRACTS.md` D5 +
DECISIONS D15, D17.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "ModelRequest",
    "ModelResponse",
    "ProviderMessage",
    "ProviderSettings",
    "ToolCallRecord",
    "ToolSpec",
    "Usage",
]


@dataclass(frozen=True)
class ProviderMessage:
    """One message block in a provider conversation.

    ``role`` is one of ``"system" | "user" | "assistant" | "tool"``;
    ``content`` is either a string (text) or a tuple of structured
    blocks (provider-specific shape).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ToolSpec:
    """Tool advertisement attached to a ``ModelRequest``.

    ``name`` follows the B5 effect-kind name rule
    (``[a-z][a-z0-9_]*``); validated at registration time, not at
    dataclass construction.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ProviderSettings:
    """Per-call provider settings.

    ``extra`` is JSON-serializable provider-specific knobs that the
    adapter round-trips through its SDK without interpretation.
    """

    model: str
    max_tokens: int | None = None
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallRecord:
    """Per-tool invocation record on a ``ModelResponse``.

    ``call_id`` is the SDK-side identifier; ``name`` follows B5.
    """

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Token / cost accounting for a single provider call.

    Cache and cost fields are optional; adapters that don't track them
    leave them at the zero / ``None`` defaults.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class ModelRequest:
    """Provider request payload (embedded in ``EffectDeclaration``)."""

    messages: tuple[ProviderMessage, ...]
    tools: tuple[ToolSpec, ...]
    settings: ProviderSettings


@dataclass(frozen=True)
class ModelResponse:
    """Provider response payload (embedded in ``EffectCapture``).

    Mutual exclusion enforced at construction: exactly one of
    ``text``, ``structured_output``, or non-empty ``tool_calls``.
    Adapters normalize ``text=""`` to ``text=None`` per DECISIONS D15
    before constructing.
    """

    text: str | None = None
    structured_output: dict[str, Any] | None = None
    tool_calls: tuple[ToolCallRecord, ...] = ()
    usage: Usage = field(default_factory=Usage)
    session_id: str | None = None
    finish_reason: Literal[
        "end_turn", "max_tokens", "stop_sequence", "tool_use"
    ] = "end_turn"

    def __post_init__(self) -> None:
        active = sum(
            [
                self.text is not None,
                self.structured_output is not None,
                len(self.tool_calls) > 0,
            ]
        )
        if active != 1:
            raise ValueError(
                "ModelResponse must carry exactly one of text, "
                "structured_output, or non-empty tool_calls; "
                f"got {active} populated"
            )
