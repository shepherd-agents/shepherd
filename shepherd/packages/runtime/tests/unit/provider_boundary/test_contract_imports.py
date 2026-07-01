"""Contract-import tests for Plan 01 provider-boundary stubs.

Satisfies CONTRACTS Maintenance Rule 3 for D1, D2, D3, D4, D5, D6 by
importing each contract from the production module path and
exercising the documented surface.

Stub semantics: ``StubRecorder`` enforces local lifecycle ordering but does
not mint production content-addressed refs. ``BypassInterposition`` does not
emit kernel records. Production behavior — Claude Agent SDK adapter, durable
ref minting, and full SDK transcript capture — is target-track work.
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Imports — each contract resolves from shepherd_runtime.provider_boundary
# ---------------------------------------------------------------------------


def test_d1_provider_interposition_imports() -> None:
    from shepherd_runtime.provider_boundary import ProviderInterposition

    assert ProviderInterposition.__name__ == "ProviderInterposition"


def test_d2_provider_runtime_and_trace_writer_import() -> None:
    from shepherd_runtime.provider_boundary import (
        ProviderRuntime,
        StubProviderRuntime,
        StubTraceWriter,
        TraceWriter,
    )

    assert ProviderRuntime.__name__ == "ProviderRuntime"
    assert TraceWriter.__name__ == "TraceWriter"
    assert StubProviderRuntime.__name__ == "StubProviderRuntime"
    assert StubTraceWriter.__name__ == "StubTraceWriter"


def test_d3_tool_handler_imports() -> None:
    from shepherd_runtime.provider_boundary import (
        StubToolHandler,
        ToolHandler,
        ToolHandlerEntry,
    )

    assert ToolHandler.__name__ == "ToolHandler"
    assert ToolHandlerEntry.__name__ == "ToolHandlerEntry"
    assert StubToolHandler.__name__ == "StubToolHandler"


def test_d4_bypass_interposition_imports() -> None:
    from shepherd_runtime.provider_boundary import BypassInterposition, ResponderFn

    assert BypassInterposition.__name__ == "BypassInterposition"
    assert ResponderFn is not None


def test_d5_payloads_import_as_frozen_dataclasses() -> None:
    from dataclasses import is_dataclass

    from shepherd_runtime.provider_boundary import (
        ModelRequest,
        ModelResponse,
        ProviderMessage,
        ProviderSettings,
        ToolCallRecord,
        ToolSpec,
        Usage,
    )

    for cls in (
        ProviderMessage,
        ProviderSettings,
        ToolSpec,
        ToolCallRecord,
        Usage,
        ModelRequest,
        ModelResponse,
    ):
        assert is_dataclass(cls), f"{cls.__name__} must be a dataclass"


def test_d6_interposition_recorder_imports() -> None:
    from shepherd_runtime.provider_boundary import (
        InterpositionRecorder,
        RecorderLifecycleError,
        StubRecorder,
        ToolHandlerNotFoundError,
    )

    assert InterpositionRecorder.__name__ == "InterpositionRecorder"
    assert StubRecorder.__name__ == "StubRecorder"
    assert issubclass(RecorderLifecycleError, RuntimeError)
    assert issubclass(ToolHandlerNotFoundError, LookupError)


def test_transcript_summary_helpers_import() -> None:
    from shepherd_runtime.provider_boundary import (
        summarize_model_failure,
        summarize_model_request,
        summarize_model_response,
    )

    assert summarize_model_request.__name__ == "summarize_model_request"
    assert summarize_model_response.__name__ == "summarize_model_response"
    assert summarize_model_failure.__name__ == "summarize_model_failure"


# ---------------------------------------------------------------------------
# D5: ModelResponse mutual exclusion
# ---------------------------------------------------------------------------


def test_model_response_text_only_succeeds() -> None:
    from shepherd_runtime.provider_boundary import ModelResponse

    r = ModelResponse(text="hello")
    assert r.text == "hello"
    assert r.structured_output is None
    assert r.tool_calls == ()
    assert r.finish_reason == "end_turn"


def test_model_response_structured_only_succeeds() -> None:
    from shepherd_runtime.provider_boundary import ModelResponse

    r = ModelResponse(structured_output={"result": {"value": 42}})
    assert r.structured_output == {"result": {"value": 42}}


def test_model_response_tool_call_only_succeeds() -> None:
    from shepherd_runtime.provider_boundary import (
        ModelResponse,
        ToolCallRecord,
    )

    call = ToolCallRecord(call_id="c1", name="read_file", arguments={"path": "x"})
    r = ModelResponse(tool_calls=(call,), finish_reason="tool_use")
    assert r.tool_calls == (call,)


def test_model_response_rejects_zero_populated() -> None:
    from shepherd_runtime.provider_boundary import ModelResponse

    with pytest.raises(ValueError, match="exactly one"):
        ModelResponse()


def test_model_response_rejects_two_populated() -> None:
    from shepherd_runtime.provider_boundary import ModelResponse

    with pytest.raises(ValueError, match="exactly one"):
        ModelResponse(text="hi", structured_output={"result": "x"})


def test_model_response_accepts_empty_string_text() -> None:
    """``text=""`` is accepted by the dataclass validator.

    The mutual-exclusion check only counts ``is not None``; empty
    string is "populated". Per DECISIONS D15, adapters (Plan 01) are
    responsible for normalizing ``text=""`` to ``text=None`` *before*
    construction so empty provider responses content-address
    identically. The contract surface here is the validator's ``is
    not None`` rule; the adapter normalization is policy.
    """
    from shepherd_runtime.provider_boundary import ModelResponse

    r = ModelResponse(text="")
    assert r.text == ""


# ---------------------------------------------------------------------------
# D5: ModelRequest construction
# ---------------------------------------------------------------------------


def test_model_request_construction() -> None:
    from shepherd_runtime.provider_boundary import (
        ModelRequest,
        ProviderMessage,
        ProviderSettings,
        ToolSpec,
    )

    req = ModelRequest(
        messages=(
            ProviderMessage(role="system", content="you are helpful"),
            ProviderMessage(role="user", content="hi"),
        ),
        tools=(
            ToolSpec(
                name="read_file",
                description="read a file",
                input_schema={"type": "object"},
            ),
        ),
        settings=ProviderSettings(model="claude-sonnet"),
    )
    assert len(req.messages) == 2
    assert len(req.tools) == 1


def test_transcript_summaries_are_structural_and_redaction_safe() -> None:
    from shepherd_runtime.provider_boundary import (
        ModelRequest,
        ModelResponse,
        ProviderMessage,
        ProviderSettings,
        ToolCallRecord,
        ToolSpec,
        summarize_model_failure,
        summarize_model_request,
        summarize_model_response,
    )

    request = ModelRequest(
        messages=(ProviderMessage(role="user", content="secret prompt"),),
        tools=(ToolSpec(name="lookup", description="lookup", input_schema={"type": "object"}),),
        settings=ProviderSettings(model="model-1"),
    )
    response = ModelResponse(
        tool_calls=(ToolCallRecord(call_id="call-1", name="lookup", arguments={"query": "secret"}),),
        finish_reason="tool_use",
    )

    request_summary = summarize_model_request(request, provider_id="provider.test")
    response_summary = summarize_model_response(response, provider_id="provider.test", model_id="model-1")
    failure_summary = summarize_model_failure(RuntimeError("secret failure"), request=request, provider_id="provider.test")

    assert request_summary == {
        "provider_id": "provider.test",
        "model_id": "model-1",
        "model": "model-1",
        "status": "requested",
        "message_count": 1,
        "message_roles": ("user",),
        "tool_count": 1,
        "tool_call_count": 0,
    }
    assert response_summary["provider_id"] == "provider.test"
    assert response_summary["model_id"] == "model-1"
    assert response_summary["status"] == "returned"
    assert response_summary["tool_call_count"] == 1
    assert response_summary["response_shape"] == "tool_calls"
    assert failure_summary["failure_class"] == "RuntimeError"
    assert failure_summary["status"] == "raised"

    combined = repr((request_summary, response_summary, failure_summary))
    assert "secret prompt" not in combined
    assert "secret failure" not in combined
    assert "secret" not in combined


# ---------------------------------------------------------------------------
# D2: TraceWriter / ProviderRuntime structural shape
# ---------------------------------------------------------------------------


def test_stub_trace_writer_mints_sequential_refs() -> None:
    from shepherd_runtime.provider_boundary import StubTraceWriter

    writer = StubTraceWriter()
    r1 = writer.append_kernel("record-a")  # type: ignore[arg-type]
    r2 = writer.append_surface("record-b")  # type: ignore[arg-type]
    assert r1 == "ref:1"
    assert r2 == "ref:2"
    assert writer.kernel_records == [("ref:1", "record-a")]
    assert writer.surface_records == [("ref:2", "record-b")]


def test_stub_provider_runtime_satisfies_protocol() -> None:
    from shepherd_runtime.provider_boundary import (
        ProviderRuntime,
        StubProviderRuntime,
    )

    runtime = StubProviderRuntime()
    # Protocol membership via runtime_checkable
    assert isinstance(runtime, ProviderRuntime)
    assert runtime.execution_context.binding_env_ref == "env:root"
    assert runtime.run_ref.id == "run-stub"
    assert runtime.task_name is None


# ---------------------------------------------------------------------------
# D3: ToolHandler lookup
# ---------------------------------------------------------------------------


def test_stub_tool_handler_install_and_lookup() -> None:
    from shepherd_runtime.provider_boundary import StubToolHandler

    handler = StubToolHandler()
    handler.install("tool.read_file", lambda payload: {"contents": "hi"})

    entry = handler.lookup("tool.read_file")
    assert entry is not None
    assert entry.handler_id == "local.read_file.v1"
    assert entry.handler_frame_ref == "frame:tool.read_file"


def test_stub_tool_handler_lookup_returns_none_on_miss() -> None:
    from shepherd_runtime.provider_boundary import StubToolHandler

    handler = StubToolHandler()
    assert handler.lookup("tool.nonexistent") is None


def test_stub_tool_handler_invoke_runs_callable() -> None:
    from shepherd_runtime.provider_boundary import StubToolHandler

    handler = StubToolHandler()
    handler.install("tool.echo", lambda payload: {"echo": payload["msg"]})

    async def _go() -> dict:
        entry = handler.lookup("tool.echo")
        assert entry is not None
        return await entry.invoke({"msg": "hi"})

    assert asyncio.run(_go()) == {"echo": "hi"}


# ---------------------------------------------------------------------------
# D4: BypassInterposition
# ---------------------------------------------------------------------------


def test_bypass_returns_responder_result() -> None:
    from shepherd_runtime.provider_boundary import (
        BypassInterposition,
        ModelRequest,
        ModelResponse,
        ProviderSettings,
        StubProviderRuntime,
        StubToolHandler,
    )

    expected = ModelResponse(text="bypass response")

    def responder(req: ModelRequest) -> ModelResponse:
        return expected

    adapter = BypassInterposition(responder)
    runtime = StubProviderRuntime()
    tools = StubToolHandler()
    request = ModelRequest(
        messages=(),
        tools=(),
        settings=ProviderSettings(model="claude-sonnet"),
    )

    result = asyncio.run(
        adapter.perform_model_call(request, runtime, tools)
    )
    assert result is expected


def test_bypass_supports_async_responder() -> None:
    from shepherd_runtime.provider_boundary import (
        BypassInterposition,
        ModelRequest,
        ModelResponse,
        ProviderSettings,
        StubProviderRuntime,
        StubToolHandler,
    )

    async def responder(req: ModelRequest) -> ModelResponse:
        return ModelResponse(text="async response")

    adapter = BypassInterposition(responder)
    request = ModelRequest(
        messages=(),
        tools=(),
        settings=ProviderSettings(model="claude-sonnet"),
    )
    result = asyncio.run(
        adapter.perform_model_call(
            request, StubProviderRuntime(), StubToolHandler()
        )
    )
    assert result.text == "async response"


def test_bypass_rejects_non_modelresponse_responder_return() -> None:
    from shepherd_runtime.provider_boundary import (
        BypassInterposition,
        ModelRequest,
        ProviderSettings,
        StubProviderRuntime,
        StubToolHandler,
    )

    adapter = BypassInterposition(lambda req: "not a ModelResponse")  # type: ignore[arg-type]
    request = ModelRequest(
        messages=(),
        tools=(),
        settings=ProviderSettings(model="claude-sonnet"),
    )
    with pytest.raises(TypeError, match="ModelResponse"):
        asyncio.run(
            adapter.perform_model_call(
                request, StubProviderRuntime(), StubToolHandler()
            )
        )


# ---------------------------------------------------------------------------
# D6: StubRecorder records calls and mints counter-based refs
# ---------------------------------------------------------------------------


def test_stub_recorder_records_full_model_turn_sextet() -> None:
    from shepherd_runtime.provider_boundary import (
        ModelRequest,
        ProviderSettings,
        StubRecorder,
    )

    recorder = StubRecorder()
    request = ModelRequest(
        messages=(),
        tools=(),
        settings=ProviderSettings(model="claude-sonnet"),
    )
    decl = recorder.start_model_call(request)
    sel = recorder.select_provider_handler(decl, "provider.claude.v1")
    handle = recorder.mint_resumption(decl, sel)
    res = recorder.resume(handle, {"text": "ok"})
    rret = recorder.resume_return(res, {"text": "ok"})
    cap = recorder.capture(sel, "return", {"text": "ok"})

    assert all(r.startswith(("decl", "sel", "handle", "resume", "rret", "cap")) for r in [decl, sel, handle, res, rret, cap])
    assert len(recorder.calls) == 6
    assert recorder.calls[0][0] == "start_model_call"
    assert recorder.calls[-1][0] == "capture"


def test_stub_recorder_supports_nested_tool_call() -> None:
    from shepherd_runtime.provider_boundary import (
        ModelRequest,
        ProviderSettings,
        StubRecorder,
    )

    recorder = StubRecorder()
    request = ModelRequest(
        messages=(),
        tools=(),
        settings=ProviderSettings(model="claude-sonnet"),
    )
    model_decl = recorder.start_model_call(request)
    tool_decl = recorder.open_tool_call(
        model_decl, "read_file", {"path": "x"}
    )
    tool_sel = recorder.select_tool_handler(tool_decl, "local.read_file.v1")
    assert tool_decl != model_decl
    assert ("open_tool_call", model_decl, "read_file", {"path": "x"}, tool_decl) in recorder.calls


def test_stub_recorder_rejects_out_of_order_lifecycle_calls() -> None:
    from shepherd_runtime.provider_boundary import (
        ModelRequest,
        ProviderSettings,
        RecorderLifecycleError,
        StubRecorder,
    )

    recorder = StubRecorder()
    request = ModelRequest(
        messages=(),
        tools=(),
        settings=ProviderSettings(model="claude-sonnet"),
    )

    with pytest.raises(RecorderLifecycleError, match="unknown declaration"):
        recorder.select_provider_handler("decl:missing", "provider.claude.v1")

    decl = recorder.start_model_call(request)
    sel = recorder.select_provider_handler(decl, "provider.claude.v1")
    handle = recorder.mint_resumption(decl, sel)
    resume = recorder.resume(handle, {"text": "ok"})

    with pytest.raises(RecorderLifecycleError, match="before resume_return"):
        recorder.capture(sel, "return", {"text": "ok"})

    recorder.resume_return(resume, {"text": "ok"})
    recorder.capture(sel, "return", {"text": "ok"})

    with pytest.raises(RecorderLifecycleError, match="already captured"):
        recorder.capture(sel, "return", {"text": "again"})
