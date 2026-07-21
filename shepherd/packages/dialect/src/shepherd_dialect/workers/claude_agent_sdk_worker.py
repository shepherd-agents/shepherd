"""Confined Claude Agent SDK worker for `shepherd.provider_worker.v1`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "shepherd.provider_worker.v1"


def main() -> None:
    """Load the worker payload and run one Claude Agent SDK invocation."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: claude_agent_sdk_worker.py PAYLOAD.json")
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    asyncio.run(run(payload))


async def run(payload: Mapping[str, Any]) -> None:
    """Run the SDK query and emit provider worker records."""
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    options_dict = {
        "model": payload.get("model"),
        "cwd": payload.get("cwd"),
        "tools": payload.get("tools"),
        "permission_mode": payload.get("permissionMode"),
        "max_turns": payload.get("maxTurns"),
        "resume": payload.get("resume"),
    }
    if payload.get("outputSchema"):
        options_dict["output_format"] = payload["outputSchema"]
    options = ClaudeAgentOptions(**{key: value for key, value in options_dict.items() if value is not None})

    output_text = ""
    structured_output: Mapping[str, object] = {}
    session_id: str | None = None
    usage: Mapping[str, object] = {}
    metadata: dict[str, object] = {"model": str(payload.get("model") or "claude-api")}
    started_tools: dict[str, str] = {}

    async for message in query(prompt=str(payload["prompt"]), options=options):
        if isinstance(message, (AssistantMessage, UserMessage)):
            for block in _blocks(message):
                if isinstance(block, TextBlock):
                    output_text = block.text or output_text
                elif isinstance(block, ToolUseBlock):
                    tool_call_id = str(block.id)
                    tool_name = str(block.name)
                    started_tools[tool_call_id] = tool_name
                    _emit(
                        {
                            "record_type": "provider_event",
                            "kind": "tool.call.started",
                            "tool_call_id": tool_call_id,
                            "model": metadata["model"],
                            "payload": {
                                "tool_name": tool_name,
                                "params_digest": _digest_jsonable(block.input or {}),
                            },
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    tool_call_id = str(block.tool_use_id)
                    tool_name = started_tools.get(tool_call_id, "tool")
                    output = _stringify_tool_output(block.content)
                    _emit(
                        {
                            "record_type": "provider_event",
                            "kind": "tool.call.completed",
                            "tool_call_id": tool_call_id,
                            "model": metadata["model"],
                            "payload": {
                                "tool_name": tool_name,
                                "success": not bool(block.is_error),
                                **_redacted_text_payload(output, field="output"),
                            },
                        }
                    )
        elif isinstance(message, ResultMessage):
            session_id = message.session_id
            result_text = getattr(message, "result", None)
            if isinstance(result_text, str) and result_text:
                output_text = result_text
            structured = getattr(message, "structured_output", None)
            if isinstance(structured, Mapping):
                structured_output = dict(structured)
            raw_usage = getattr(message, "usage", None)
            if isinstance(raw_usage, Mapping):
                usage = dict(raw_usage)
            model_id = getattr(message, "model", None)
            if isinstance(model_id, str) and model_id:
                metadata["model"] = model_id
            for attr in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "is_error"):
                value = getattr(message, attr, None)
                if value is not None:
                    metadata[attr] = value

    if output_text or usage:
        _emit(
            {
                "record_type": "provider_event",
                "kind": "model.call",
                "model": metadata["model"],
                "payload": {
                    "usage": dict(usage),
                    **_redacted_text_payload(output_text, field="output_text"),
                },
            }
        )
    if output_text:
        _emit(
            {
                "record_type": "provider_event",
                "kind": "model.turn",
                "model": metadata["model"],
                "payload": _redacted_text_payload(output_text, field="text"),
            }
        )

    _emit(
        {
            "record_type": "provider_result",
            "output_text": output_text,
            "structured_output": dict(structured_output),
            "session_id": session_id,
            "usage": dict(usage),
            "metadata": metadata,
        }
    )


def _blocks(message: object) -> tuple[object, ...]:
    content = getattr(message, "content", ())
    return tuple(content) if isinstance(content, list | tuple) else ()


def _emit(record: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps({"schema_version": SCHEMA_VERSION, **dict(record)}, sort_keys=True) + "\n")
    sys.stdout.flush()


def _digest_jsonable(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()}"


def _redacted_text_payload(value: str, *, field: str, excerpt_limit: int = 10_000) -> dict[str, object]:
    return {
        f"{field}_digest": f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}",
        f"{field}_length": len(value),
        f"{field}_excerpt": value[-excerpt_limit:] if excerpt_limit else "",
    }


def _stringify_tool_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except TypeError:
        return repr(value)


if __name__ == "__main__":
    main()
