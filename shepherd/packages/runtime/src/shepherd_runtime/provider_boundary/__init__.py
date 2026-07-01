"""Provider-boundary protocols and Phase 1 bypass bridge.

CONTRACTS D1-D6: protocols, frozen payload dataclasses, in-memory test
implementations, and ``BypassInterposition`` for the offline handled
``"model.call"`` path. Live SDK adapters, transcript capture, durable ref
minting, tool dispatch, and full provider sextet validation remain owner-track
work.

Pinned by `docs/design/proposed/260505-plans/CONTRACTS.md` Group D.
"""

from __future__ import annotations

from shepherd_runtime.provider_boundary.interposition import (
    BypassInterposition,
    ProviderInterposition,
    ResponderFn,
)
from shepherd_runtime.provider_boundary.payloads import (
    ModelRequest,
    ModelResponse,
    ProviderMessage,
    ProviderSettings,
    ToolCallRecord,
    ToolSpec,
    Usage,
)
from shepherd_runtime.provider_boundary.recorder import (
    InterpositionRecorder,
    RecorderLifecycleError,
    StubRecorder,
    ToolHandlerNotFoundError,
)
from shepherd_runtime.provider_boundary.runtime import (
    ProviderRuntime,
    StubProviderRuntime,
    StubTraceWriter,
    TraceWriter,
)
from shepherd_runtime.provider_boundary.summaries import (
    summarize_model_failure,
    summarize_model_request,
    summarize_model_response,
)
from shepherd_runtime.provider_boundary.tools import (
    StubToolHandler,
    ToolHandler,
    ToolHandlerEntry,
)

__all__ = [
    "BypassInterposition",
    # D6: InterpositionRecorder
    "InterpositionRecorder",
    "ModelRequest",
    "ModelResponse",
    # D1: ProviderInterposition Protocol + D4: BypassInterposition
    "ProviderInterposition",
    # D5: payload schemas
    "ProviderMessage",
    # D2: ProviderRuntime + TraceWriter
    "ProviderRuntime",
    "ProviderSettings",
    "RecorderLifecycleError",
    "ResponderFn",
    "StubProviderRuntime",
    "StubRecorder",
    "StubToolHandler",
    "StubTraceWriter",
    "ToolCallRecord",
    # D3: ToolHandler
    "ToolHandler",
    "ToolHandlerEntry",
    "ToolHandlerNotFoundError",
    "ToolSpec",
    "TraceWriter",
    "Usage",
    "summarize_model_failure",
    "summarize_model_request",
    "summarize_model_response",
]
