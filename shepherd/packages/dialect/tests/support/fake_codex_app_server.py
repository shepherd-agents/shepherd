"""Deterministic JSON-RPC app-server used by the Codex Python spike.

It deliberately speaks the wire protocol rather than replacing SDK classes with
mocks.  That lets the spike exercise the SDK reader/router, generated models,
early-notification buffering, server requests, and nested-process confinement
without credentials or a live model.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

THREAD_ID = "thread-spike"
TURN_ID = "turn-spike"
_rate_limit_reads = 0


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, sort_keys=True) + "\n")
    sys.stdout.flush()


def _response(request_id: object, result: dict[str, Any]) -> None:
    _send({"id": request_id, "result": result})


def _notification(method: str, params: dict[str, Any]) -> None:
    _send({"method": method, "params": params})


def _thread() -> dict[str, Any]:
    return {
        "cliVersion": "fake-1.0",
        "createdAt": 1,
        "cwd": str(Path.cwd()),
        "ephemeral": True,
        "id": THREAD_ID,
        "modelProvider": "openai",
        "preview": "",
        "sessionId": "session-spike",
        "source": "appServer",
        "status": {"type": "idle"},
        "turns": [],
        "updatedAt": 1,
    }


def _thread_start_result() -> dict[str, Any]:
    return {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "auto_review",
        "cwd": str(Path.cwd()),
        "instructionSources": [],
        "model": "gpt-5.4",
        "modelProvider": "openai",
        "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
        "thread": _thread(),
    }


def _turn(status: str, *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"id": TURN_ID, "status": status, "items": items or []}


def _command(status: str, *, output: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "commandExecution",
        "id": "cmd-1",
        "command": "printf 'do not persist sk-spike-secret'",
        "commandActions": [],
        "cwd": str(Path.cwd()),
        "status": status,
    }
    if output is not None:
        item.update({"aggregatedOutput": output, "exitCode": 0, "durationMs": 7})
    return item


def _file_change(status: str) -> dict[str, Any]:
    return {
        "type": "fileChange",
        "id": "file-1",
        "status": status,
        "changes": [
            {
                "path": "result.txt",
                "diff": "+captured sk-spike-secret",
                "kind": {"type": "update"},
            }
        ],
    }


def _mcp(status: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "mcpToolCall",
        "id": "mcp-1",
        "arguments": {"credential": "sk-spike-secret", "path": "result.txt"},
        "server": "fixture",
        "tool": "write",
        "status": status,
    }
    if status != "inProgress":
        item["result"] = {"content": [{"type": "text", "text": "done sk-spike-secret"}]}
        item["durationMs"] = 4
    return item


def _web() -> dict[str, Any]:
    return {
        "type": "webSearch",
        "id": "web-1",
        "query": "sk-spike-secret never persist this query",
        "action": {"type": "search", "query": "example"},
    }


def _item_started(item: dict[str, Any], *, timestamp: int) -> None:
    _notification(
        "item/started",
        {"threadId": THREAD_ID, "turnId": TURN_ID, "startedAtMs": timestamp, "item": item},
    )


def _item_completed(item: dict[str, Any], *, timestamp: int) -> None:
    _notification(
        "item/completed",
        {"threadId": THREAD_ID, "turnId": TURN_ID, "completedAtMs": timestamp, "item": item},
    )


def _turn_completed(status: str) -> None:
    _notification("turn/completed", {"threadId": THREAD_ID, "turn": _turn(status)})


def _write_canaries() -> None:
    inside = os.environ.get("SHEPHERD_CODEX_SPIKE_CANARY_INSIDE")
    outside = os.environ.get("SHEPHERD_CODEX_SPIKE_CANARY_OUTSIDE")
    if inside:
        Path(inside).write_text("nested app-server child wrote inside\n", encoding="utf-8")
    if outside:
        with suppress(OSError):
            Path(outside).write_text("nested app-server escaped\n", encoding="utf-8")
    if os.environ.get("SHEPHERD_CODEX_SPIKE_WRITE_PROFILE_MARKER"):
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home:
            Path(codex_home, "refresh-marker").write_text("managed profile write\n", encoding="utf-8")
    report_path = os.environ.get("SHEPHERD_CODEX_SPIKE_ENV_REPORT")
    if report_path:
        report = {
            "codex_home": os.environ.get("CODEX_HOME"),
            "home": os.environ.get("HOME"),
            "codex_auth_exists": bool(
                os.environ.get("CODEX_HOME") and Path(os.environ["CODEX_HOME"]).joinpath("auth.json").exists()
            ),
        }
        Path(report_path).write_text(json.dumps(report, sort_keys=True), encoding="utf-8")


def _record_api_key_login(params: dict[str, Any]) -> None:
    """Record only non-secret evidence that an API-key login reached app-server."""
    report_path = os.environ.get("SHEPHERD_CODEX_SPIKE_AUTH_REPORT")
    if not report_path:
        report_path = None
    raw_key = params.get("apiKey")
    key = raw_key if isinstance(raw_key, str) else ""
    report = {
        "auth_kind": params.get("type"),
        "key_present": bool(key),
        "key_length": len(key),
        "key_digest": hashlib.sha256(key.encode("utf-8")).hexdigest() if key else None,
    }
    if report_path:
        Path(report_path).write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        Path(codex_home, "auth.json").write_text('{"fixture":"api-key-auth"}', encoding="utf-8")


def _maybe_delay() -> None:
    raw = os.environ.get("SHEPHERD_CODEX_SPIKE_EVENT_DELAY_MS")
    if raw:
        time.sleep(float(raw) / 1000)


def _emit_all_events(*, production: bool = False) -> None:
    # The beta SDK registers the turn queue after it receives ``turn/start``.
    # Give ordinary fixture scenarios enough scheduling room to do that, while
    # the explicit no-delay race test documents the current SDK behaviour.
    _maybe_delay()
    _item_started(_command("inProgress"), timestamp=100)
    _maybe_delay()
    _notification(
        "item/commandExecution/outputDelta",
        {"threadId": THREAD_ID, "turnId": TURN_ID, "itemId": "cmd-1", "delta": "stdout sk-spike-secret"},
    )
    _item_completed(_command("completed", output="stdout sk-spike-secret"), timestamp=101)

    _item_started(_file_change("inProgress"), timestamp=102)
    write_path = os.environ.get("SHEPHERD_CODEX_SPIKE_WRITE_PATH")
    if write_path:
        Path(write_path).write_text("captured file effect\n", encoding="utf-8")
    carrier_only_path = os.environ.get("SHEPHERD_CODEX_SPIKE_CARRIER_ONLY_PATH")
    if carrier_only_path:
        Path(carrier_only_path).write_text("unreported carrier effect\n", encoding="utf-8")
    _item_completed(_file_change("completed"), timestamp=103)

    _item_started(_mcp("inProgress"), timestamp=104)
    _notification(
        "item/mcpToolCall/progress",
        {"threadId": THREAD_ID, "turnId": TURN_ID, "itemId": "mcp-1", "message": "working sk-spike-secret"},
    )
    _item_completed(_mcp("completed"), timestamp=105)

    _item_started(_web(), timestamp=106)
    _item_completed(_web(), timestamp=107)
    _notification(
        "turn/diff/updated",
        {"threadId": THREAD_ID, "turnId": TURN_ID, "diff": "diff --git sk-spike-secret"},
    )
    _notification(
        "experimental/futureEvent",
        {"threadId": THREAD_ID, "turnId": TURN_ID, "apiKey": "sk-spike-secret", "value": "future"},
    )
    if production:
        _notification(
            "item/agentMessage/delta",
            {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "itemId": "message-1",
                "delta": "completed safely sk-spike-secret",
            },
        )
        _item_completed(
            {
                "type": "agentMessage",
                "id": "message-1",
                "text": "completed safely sk-spike-secret",
                "phase": "final_answer",
            },
            timestamp=108,
        )
        usage = {
            "inputTokens": 120,
            "cachedInputTokens": 20,
            "outputTokens": 30,
            "reasoningOutputTokens": 10,
            "totalTokens": 150,
        }
        _notification(
            "thread/tokenUsage/updated",
            {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "tokenUsage": {"last": usage, "total": usage, "modelContextWindow": 200000},
            },
        )
    _turn_completed("completed")


def _account() -> dict[str, Any]:
    if os.environ.get("SHEPHERD_CODEX_SPIKE_SCENARIO") == "api-key-production":
        return {
            "account": {"type": "apiKey"},
            "requiresOpenaiAuth": True,
        }
    return {
        "account": {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "plus"},
        "requiresOpenaiAuth": True,
    }


def _rate_limits() -> dict[str, Any]:
    global _rate_limit_reads
    balance = "10.0" if _rate_limit_reads == 0 else "9.5"
    _rate_limit_reads += 1
    return {
        "rateLimits": {
            "planType": "plus",
            "primary": {"usedPercent": 5, "resetsAt": 9999999999, "windowDurationMins": 300},
            "secondary": None,
            "credits": {"hasCredits": True, "unlimited": False, "balance": balance},
        },
        "rateLimitsByLimitId": None,
        "rateLimitResetCredits": None,
    }


def _command_exec(params: dict[str, Any]) -> dict[str, Any]:
    command = params.get("command")
    argv = command if isinstance(command, list) else []
    command_text = " ".join(str(item) for item in argv)
    if "/proc/$PPID/environ" in command_text:
        return {"exitCode": 0, "stdout": "", "stderr": ""}
    target = Path(str(argv[-1])) if argv else None
    cwd = Path(str(params.get("cwd") or Path.cwd())).resolve()
    if target is not None and "printf" in command_text:
        try:
            target.resolve().relative_to(cwd)
        except ValueError:
            return {"exitCode": 1, "stdout": "", "stderr": "denied"}
        target.write_text("ok\n", encoding="utf-8")
    return {"exitCode": 0, "stdout": "", "stderr": ""}


def _emit_approval_request() -> None:
    _maybe_delay()
    _item_started(_command("inProgress"), timestamp=200)
    _send(
        {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "itemId": "cmd-1",
                "command": "echo sk-spike-secret",
                "reason": "fixture approval",
            },
        }
    )


def _finish_approval(response: dict[str, Any]) -> None:
    result_path = os.environ.get("SHEPHERD_CODEX_SPIKE_APPROVAL_RESULT")
    if result_path:
        Path(result_path).write_text(json.dumps(response, sort_keys=True), encoding="utf-8")
    _notification("serverRequest/resolved", {"threadId": THREAD_ID, "requestId": "approval-1"})
    _item_completed(_command("declined"), timestamp=201)
    _turn_completed("completed")


def _emit_interrupt_start() -> None:
    _maybe_delay()
    _item_started(_command("inProgress"), timestamp=300)


def _emit_unknown_server_request() -> None:
    _send(
        {
            "id": "unknown-request-1",
            "method": "experimental/requestApproval",
            "params": {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "itemId": "future-1",
                "secret": "sk-spike-secret",
            },
        }
    )


def _emit_turn_error() -> None:
    _maybe_delay()
    _notification(
        "error",
        {
            "error": {"message": "fixture turn failure sk-spike-secret"},
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "willRetry": False,
        },
    )
    _turn_completed("failed")


def main() -> None:
    scenario = os.environ.get("SHEPHERD_CODEX_SPIKE_SCENARIO", "all-events")
    awaiting_approval = False
    awaiting_interrupt = False
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _write_canaries()
            _response(
                request_id,
                {
                    "serverInfo": {"name": "fake-codex-app-server", "version": "1.0"},
                    "userAgent": "fake-codex-app-server/1.0",
                },
            )
            continue
        if method == "initialized":
            continue
        if method == "account/login/start":
            params = message.get("params")
            login_params = params if isinstance(params, dict) else {}
            if login_params.get("type") == "apiKey":
                _record_api_key_login(login_params)
                _response(request_id, {"type": "apiKey"})
            else:
                _send({"id": request_id, "error": {"code": -32602, "message": "unsupported fake login type"}})
            continue
        if method == "account/read":
            _response(request_id, _account())
            continue
        if method == "account/rateLimits/read":
            _response(request_id, _rate_limits())
            continue
        if method == "permissionProfile/list":
            _response(
                request_id,
                {"data": [{"id": "shepherd_run", "description": "fixture", "allowed": True}], "nextCursor": None},
            )
            continue
        if method == "command/exec":
            params = message.get("params")
            _response(request_id, _command_exec(params if isinstance(params, dict) else {}))
            continue
        if method == "thread/start":
            _response(request_id, _thread_start_result())
            continue
        if method == "turn/start":
            _response(request_id, {"turn": _turn("inProgress")})
            if scenario == "all-events":
                _emit_all_events()
            elif scenario in {"production-all-events", "api-key-production"}:
                _emit_all_events(production=True)
            elif scenario == "approval":
                awaiting_approval = True
                _emit_approval_request()
            elif scenario == "interrupt":
                awaiting_interrupt = True
                _emit_interrupt_start()
            elif scenario == "unknown-request":
                _emit_unknown_server_request()
            elif scenario == "turn-error":
                _emit_turn_error()
            elif scenario == "malformed":
                sys.stdout.write("not-json\n")
                sys.stdout.flush()
            else:
                _turn_completed("completed")
            continue
        if awaiting_approval and request_id == "approval-1":
            awaiting_approval = False
            _finish_approval(message.get("result") if isinstance(message.get("result"), dict) else {})
            continue
        if awaiting_interrupt and method == "turn/interrupt":
            awaiting_interrupt = False
            _response(request_id, {})
            _turn_completed("interrupted")
            continue


if __name__ == "__main__":
    main()
