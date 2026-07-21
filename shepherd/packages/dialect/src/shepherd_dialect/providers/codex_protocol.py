"""Pinned Codex app-server protocol, sandbox, and activity adapter."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from shepherd_dialect.provider_activity import ProviderActivityLedger
from shepherd_dialect.providers.codex_profile import CODEX_TESTED_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable

    from shepherd_dialect.provider_activity import ProviderActivity

CODEX_PERMISSION_PROFILE = "shepherd_run"
CODEX_PROTOCOL_ADAPTER_VERSION = "shepherd.codex_protocol.144_4.v1"

_TOOL_ITEM_TYPES = frozenset({"commandExecution", "fileChange", "mcpToolCall", "webSearch"})


class CodexProtocolError(RuntimeError):
    """Raised when the pinned app-server contract or tool sandbox refuses."""


class CodexTurnDeadlineError(CodexProtocolError):
    """Raised after a deadline-triggered interrupt has terminalized a turn."""

    def __init__(self, message: str, *, terminal: str) -> None:
        super().__init__(message)
        self.terminal = terminal


@dataclass(frozen=True)
class CodexTurnResult:
    """Safe terminal data returned by one completed Codex turn."""

    thread_id: str
    turn_id: str
    terminal: str
    output_text: str
    structured_output: Mapping[str, object]
    usage: Mapping[str, object]
    cost: Mapping[str, object]
    rate_limits: Mapping[str, object]
    sdk_version: str
    runtime_version: str
    sandbox_evidence: Mapping[str, object]


class CodexIngressProjector:
    """Derive safe summaries while retaining private output/terminal state."""

    def __init__(self, *, workspace_root: Path, request_method: Callable[[object], str | None]) -> None:
        self.workspace_root = workspace_root.resolve()
        self._request_method = request_method
        self._lock = threading.RLock()
        self._terminal_condition = threading.Condition(self._lock)
        self._turn_terminals: dict[str, str] = {}
        self._message_deltas: dict[str, list[str]] = {}
        self._completed_messages: dict[str, str] = {}
        self._usage: dict[str, object] = {}

    def __call__(self, message: Mapping[str, Any] | None, parse_state: str) -> Mapping[str, object]:
        if message is None:
            return {
                "category": "malformed",
                "kind": f"transport.{parse_state}",
                "parse_state": parse_state,
            }
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params")
            raw_params = params if isinstance(params, Mapping) else {}
            if "id" in message:
                return self._server_request(method, raw_params)
            return self._notification(method, raw_params)
        response_id = message.get("id")
        request_method = self._request_method(response_id)
        category = "error_response" if "error" in message else "response"
        return {
            "category": category,
            "kind": f"{category}.{_safe_method(request_method or 'unknown')}",
            "method": request_method,
            "response_state": "error" if "error" in message else "ok",
            "request_id_digest": _digest_text(str(response_id)),
            **_safe_error_summary(message.get("error")),
        }

    def wait_for_terminal(self, turn_id: str, *, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._terminal_condition:
            while turn_id not in self._turn_terminals:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexProtocolError("app-server did not emit turn/completed before the deadline")
                self._terminal_condition.wait(timeout=remaining)
            return self._turn_terminals[turn_id]

    def output_text(self, turn_id: str) -> str:
        with self._lock:
            completed = self._completed_messages.get(turn_id)
            if completed is not None:
                return completed
            return "".join(self._message_deltas.get(turn_id, ()))

    @property
    def usage(self) -> Mapping[str, object]:
        with self._lock:
            return dict(self._usage)

    def _server_request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, object]:
        return {
            "category": "server_request",
            "kind": f"server_request.{_safe_method(method)}",
            "method": method,
            "thread_id": _mapping_string(params, "threadId"),
            "turn_id": _mapping_string(params, "turnId"),
            "item_id": _mapping_string(params, "itemId"),
            "approval_kind": _approval_kind(method),
            "expected_decision": "decline",
            "param_keys": sorted(str(key) for key in params),
            "params_digest": _digest_json(params),
        }

    def _notification(self, method: str, params: Mapping[str, Any]) -> Mapping[str, object]:
        thread_id = _mapping_string(params, "threadId", "thread_id")
        turn_id = _mapping_string(params, "turnId", "turn_id")
        item_id = _mapping_string(params, "itemId", "item_id")
        base: dict[str, object] = {
            "category": "notification",
            "kind": f"notification.{_safe_method(method)}",
            "method": method,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "item_id": item_id,
        }
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            raw_item = item if isinstance(item, Mapping) else {}
            item_id = _mapping_string(raw_item, "id") or item_id
            base["item_id"] = item_id
            base.update(_safe_item_summary(raw_item, workspace_root=self.workspace_root))
            if method == "item/completed" and raw_item.get("type") == "agentMessage":
                text = raw_item.get("text")
                if isinstance(text, str) and turn_id:
                    with self._lock:
                        self._completed_messages[turn_id] = text
            timestamp_key = "startedAtMs" if method == "item/started" else "completedAtMs"
            timestamp = params.get(timestamp_key)
            if isinstance(timestamp, int) and not isinstance(timestamp, bool):
                base["server_timestamp_ms"] = timestamp
            return base
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and turn_id:
                with self._lock:
                    self._message_deltas.setdefault(turn_id, []).append(delta)
            base.update(_safe_blob(delta, prefix="message"))
            return base
        if method == "item/commandExecution/outputDelta":
            base.update(_safe_blob(params.get("delta"), prefix="native_output"))
            return base
        if method == "item/mcpToolCall/progress":
            base.update(_safe_blob(params.get("message"), prefix="progress"))
            return base
        if method == "turn/diff/updated":
            base.update(_safe_blob(params.get("diff"), prefix="workspace_patch"))
            return base
        if method == "thread/tokenUsage/updated":
            usage = _safe_token_usage(params.get("tokenUsage"))
            with self._lock:
                self._usage = dict(usage)
            base.update(usage)
            return base
        if method == "turn/completed":
            turn = params.get("turn")
            raw_turn = turn if isinstance(turn, Mapping) else {}
            completed_turn = _mapping_string(raw_turn, "id") or turn_id
            terminal = _enum_string(raw_turn.get("status")) or "unknown"
            base["turn_id"] = completed_turn
            base["terminal"] = terminal
            base.update(_safe_error_summary(raw_turn.get("error")))
            if completed_turn:
                with self._terminal_condition:
                    self._turn_terminals[completed_turn] = terminal
                    self._terminal_condition.notify_all()
            return base
        if method == "error":
            base.update(_safe_error_summary(params.get("error")))
            base["will_retry"] = params.get("willRetry") is True
            return base
        base["params_digest"] = _digest_json(params)
        base["param_keys"] = sorted(str(key) for key in params)
        return base


def make_capturing_client(
    *,
    config: Any,
    provider_id: str,
    invocation_id: str,
    workspace_root: Path,
    on_activity: Callable[[ProviderActivity], None] | None = None,
) -> tuple[Any, ProviderActivityLedger, CodexIngressProjector]:
    """Construct the pinned client with capture before JSON parsing/routing."""
    from openai_codex.client import CodexClient  # type: ignore[import-not-found]
    from openai_codex.errors import CodexError, TransportClosedError  # type: ignore[import-not-found]

    class CapturingCodexClient(CodexClient):
        def __init__(self) -> None:
            self._shepherd_request_methods: dict[object, str] = {}
            self._shepherd_request_lock = threading.RLock()
            self._shepherd_ledger: ProviderActivityLedger | None = None
            super().__init__(config=config, approval_handler=self._shepherd_decline_approval)

        def request_method(self, request_id: object) -> str | None:
            with self._shepherd_request_lock:
                return self._shepherd_request_methods.get(request_id)

        def record_control(self, *, kind: str, payload: Mapping[str, object]) -> None:
            if self._shepherd_ledger is None:
                raise CodexError("control decision occurred before Shepherd installed its activity ledger")
            self._shepherd_ledger.append_control(kind=kind, payload=payload)

        def _write_message(self, payload: Any) -> None:
            if isinstance(payload, Mapping) and isinstance(payload.get("method"), str) and "id" in payload:
                with self._shepherd_request_lock:
                    self._shepherd_request_methods[payload.get("id")] = str(payload["method"])
            super()._write_message(payload)

        def _read_message(self) -> dict[str, Any]:
            if self._proc is None or self._proc.stdout is None:
                raise TransportClosedError("Codex process is not running")
            line = self._proc.stdout.readline()
            if not line:
                raise TransportClosedError(f"Codex process closed stdout. stderr_length={len(self._stderr_tail())}")
            if self._shepherd_ledger is None:
                raise CodexError("Shepherd activity ledger was not installed before transport start")
            self._shepherd_ledger.append_ingress(line)
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodexError("Codex app-server emitted invalid JSON-RPC") from exc
            if not isinstance(message, dict):
                raise CodexError("Codex app-server emitted a non-object JSON-RPC payload")
            return message

        def _shepherd_decline_approval(self, method: str, params: Mapping[str, Any] | None) -> dict[str, object]:
            raw_params = params if isinstance(params, Mapping) else {}
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                if self._shepherd_ledger is None:
                    raise CodexError("approval request arrived before Shepherd installed its activity ledger")
                self._shepherd_ledger.append_control(
                    kind="approval.declined",
                    payload={
                        "method": method,
                        "thread_id": _mapping_string(raw_params, "threadId"),
                        "turn_id": _mapping_string(raw_params, "turnId"),
                        "item_id": _mapping_string(raw_params, "itemId"),
                        "decision": "decline",
                    },
                )
                return {"decision": "decline"}
            if self._shepherd_ledger is None:
                raise CodexError("unsupported server request arrived before Shepherd installed its activity ledger")
            self._shepherd_ledger.append_control(
                kind="server_request.refused",
                payload={
                    "method": method,
                    "thread_id": _mapping_string(raw_params, "threadId"),
                    "turn_id": _mapping_string(raw_params, "turnId"),
                    "item_id": _mapping_string(raw_params, "itemId"),
                    "reason": "unsupported_method",
                },
            )
            raise CodexError("unsupported Codex app-server request refused")

    client = CapturingCodexClient()
    projector = CodexIngressProjector(workspace_root=workspace_root, request_method=client.request_method)
    ledger = ProviderActivityLedger(
        provider_id=provider_id,
        invocation_id=invocation_id,
        source="codex.app_server",
        projector=projector,
        on_activity=on_activity,
    )
    client._shepherd_ledger = ledger
    return client, ledger, projector


def run_codex_turn(
    *,
    client: Any,
    projector: CodexIngressProjector,
    workspace: Path,
    model: str,
    prompt: str,
    output_schema: Mapping[str, object] | None,
    deadline_seconds: float,
    auth_mode: str,
    writable_roots: tuple[Path, ...],
    denied_paths: tuple[Path, ...],
    allow_fake_runtime: bool = False,
) -> CodexTurnResult:
    """Handshake, prove the profile, then run one turn without SDK queue loss."""
    from importlib import metadata

    from openai_codex.generated.v2_all import (  # type: ignore[import-not-found]
        CommandExecResponse,
        GetAccountRateLimitsResponse,
    )

    sdk_version = metadata.version("openai-codex")
    if sdk_version != CODEX_TESTED_VERSION:
        raise CodexProtocolError(f"openai-codex {sdk_version} differs from tested {CODEX_TESTED_VERSION}")
    runtime_distribution_version = metadata.version("openai-codex-cli-bin")
    if runtime_distribution_version != CODEX_TESTED_VERSION:
        raise CodexProtocolError(
            f"openai-codex-cli-bin {runtime_distribution_version} differs from tested {CODEX_TESTED_VERSION}"
        )
    client.start()
    initialized = client.initialize()
    server_info = getattr(initialized, "server_info", None) or getattr(initialized, "serverInfo", None)
    advertised_version = getattr(server_info, "version", None)
    if (
        not allow_fake_runtime
        and isinstance(advertised_version, str)
        and advertised_version
        and advertised_version != CODEX_TESTED_VERSION
    ):
        raise CodexProtocolError(f"Codex runtime {advertised_version} differs from tested {CODEX_TESTED_VERSION}")
    runtime_version = advertised_version or runtime_distribution_version
    account = client.account_read({"refreshToken": True})
    account_root = getattr(getattr(account, "account", None), "root", None)
    if account_root is None:
        raise CodexProtocolError("Codex app-server reports no authenticated ChatGPT account")
    account_type = _enum_string(getattr(account_root, "type", None))
    if auth_mode == "chatgpt" and account_type == "apiKey":
        raise CodexProtocolError("Codex provider requires ChatGPT subscription auth, not API-key auth")
    if auth_mode == "api_key" and account_type != "apiKey":
        raise CodexProtocolError("Codex provider profile is configured for API-key auth but app-server is not")
    if auth_mode not in {"chatgpt", "api_key"}:
        raise CodexProtocolError(f"unsupported Codex authentication mode: {auth_mode!r}")

    sandbox_evidence = prove_codex_sandbox(
        client,
        workspace=workspace,
        writable_roots=writable_roots,
        denied_paths=denied_paths,
        response_model=CommandExecResponse,
    )
    rate_before = _read_rate_limits(client, GetAccountRateLimitsResponse) if auth_mode == "chatgpt" else {}
    started = client.thread_start(
        {
            "model": model,
            "cwd": str(workspace),
            "approvalPolicy": "never",
            "permissions": CODEX_PERMISSION_PROFILE,
            "ephemeral": True,
            "developerInstructions": (
                "Shepherd has already resolved authority. Work only in the current workspace. "
                "Never access provider credentials or request interactive approval."
            ),
        }
    )
    thread_id = started.thread.id
    turn_params: dict[str, object] = {}
    if output_schema is not None:
        turn_params["outputSchema"] = dict(output_schema)
    turn = client.turn_start(thread_id, prompt, turn_params or None)
    try:
        terminal = projector.wait_for_terminal(turn.turn.id, timeout=deadline_seconds)
    except CodexProtocolError as exc:
        terminal = "deadline_without_terminal"
        reader = getattr(client, "_reader_thread", None)
        if reader is not None and reader.is_alive():
            client.record_control(
                kind="turn.interrupt.requested",
                payload={"thread_id": thread_id, "turn_id": turn.turn.id, "reason": "deadline"},
            )
            try:
                client.turn_interrupt(thread_id, turn.turn.id)
                terminal = projector.wait_for_terminal(turn.turn.id, timeout=min(5.0, deadline_seconds))
            except Exception:  # noqa: BLE001 - hard-stop cleanup still follows a failed graceful interrupt
                client.record_control(
                    kind="turn.interrupt.failed",
                    payload={"thread_id": thread_id, "turn_id": turn.turn.id, "reason": "deadline"},
                )
        raise CodexTurnDeadlineError(
            "Codex turn exceeded its deadline and was interrupted",
            terminal=terminal,
        ) from exc
    rate_after = _read_rate_limits(client, GetAccountRateLimitsResponse) if auth_mode == "chatgpt" else {}
    rate_summary, cost = _rate_and_cost_summary(rate_before, rate_after, auth_mode=auth_mode)
    output_text = projector.output_text(turn.turn.id)
    structured: Mapping[str, object] = {}
    if output_schema is not None and output_text:
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            structured = dict(parsed)
    return CodexTurnResult(
        thread_id=thread_id,
        turn_id=turn.turn.id,
        terminal=terminal,
        output_text=output_text,
        structured_output=structured,
        usage=projector.usage,
        cost=cost,
        rate_limits=rate_summary,
        sdk_version=sdk_version,
        runtime_version=runtime_version,
        sandbox_evidence=sandbox_evidence,
    )


def write_codex_permission_config(
    *,
    codex_home: Path,
    workspace: Path,
    credential_paths: tuple[Path, ...],
    writable_roots: tuple[Path, ...],
    network_mode: str,
    allowed_hosts: tuple[str, ...],
    readable_runtime_paths: tuple[Path, ...] = (),
) -> str:
    """Write the invocation-specific profile and return its stable digest."""
    if network_mode not in {"deny_all", "allow_all", "broker"}:
        raise CodexProtocolError(f"unsupported Codex network mode: {network_mode!r}")
    workspace = workspace.resolve()
    grants: list[str] = ['":minimal" = "read"']
    denied_candidates = {codex_home.resolve(), *(path.resolve() for path in credential_paths)}
    denied: list[Path] = []
    for path in sorted(denied_candidates, key=lambda item: (len(item.parts), str(item))):
        if any(path == parent or path.is_relative_to(parent) for parent in denied):
            continue
        denied.append(path)
    for path in denied:
        grants.append(f'{_toml_string(path)} = "deny"')
        if path.is_dir():
            # A more-specific nonexistent read anchor lets Bubblewrap create
            # mount-point parents below an otherwise read-only hierarchy. It
            # exposes no provider data and the broader directory remains deny.
            grants.append(f'{_toml_string(path / ".shepherd-sandbox-anchor")} = "read"')
    # Codex stages one Bubblewrap argument file below CODEX_HOME while building
    # the child sandbox. The child may read that single generated file, but the
    # surrounding ephemeral profile and credential link remain denied. Without
    # this narrow override Bubblewrap cannot finish constructing the namespace.
    grants.append(f'{_toml_string(codex_home.resolve() / "tmp" / "arg0")} = "read"')
    for path in sorted({path.resolve() for path in readable_runtime_paths}, key=str):
        grants.append(f'{_toml_string(path)} = "read"')
    resolved_writable_roots = {path.resolve() for path in writable_roots}
    workspace_access = "write" if workspace in resolved_writable_roots else "read"
    for root in sorted(resolved_writable_roots, key=str):
        try:
            relative = root.relative_to(workspace)
        except ValueError as exc:
            raise CodexProtocolError("Codex writable roots must be within the canonical workspace") from exc
        if relative == Path():
            continue
        grants.append(f'{_toml_string(root)} = "write"')
    network_enabled = network_mode != "deny_all"
    if network_mode == "broker" and not allowed_hosts:
        raise CodexProtocolError("broker network policy requires exact allowed hosts")
    domain_lines = ""
    if network_mode == "broker":
        checked = tuple(_validate_network_host(host) for host in allowed_hosts)
        domain_lines = "\n[permissions.shepherd_run.network.domains]\n" + "\n".join(
            f'"{host}" = "allow"' for host in checked
        )
    config = (
        f'default_permissions = "{CODEX_PERMISSION_PROFILE}"\n'
        'cli_auth_credentials_store = "file"\n\n'
        f"[permissions.{CODEX_PERMISSION_PROFILE}]\n"
        'description = "Invocation-specific Shepherd tool sandbox."\n\n'
        f"[permissions.{CODEX_PERMISSION_PROFILE}.filesystem]\n" + "\n".join(grants) + "\n\n"
        f'[permissions.{CODEX_PERMISSION_PROFILE}.filesystem.":workspace_roots"]\n'
        f'"." = "{workspace_access}"\n\n'
        f"[permissions.{CODEX_PERMISSION_PROFILE}.network]\n"
        f"enabled = {'true' if network_enabled else 'false'}\n" + domain_lines + "\n"
    )
    path = codex_home / "config.toml"
    path.write_text(config, encoding="utf-8")
    path.chmod(0o600)
    return _digest_text(config)


def prove_codex_sandbox(
    client: Any,
    *,
    workspace: Path,
    writable_roots: tuple[Path, ...],
    denied_paths: tuple[Path, ...],
    response_model: type[Any],
) -> Mapping[str, object]:
    """Run no-model positive and negative canaries before sending the prompt."""
    probe_name = f".shepherd-codex-probe-{uuid.uuid4().hex}"
    inside = (writable_roots[0] if writable_roots else workspace) / probe_name
    outside = workspace.parent / f"{probe_name}-outside"
    inside_result = _command_exec(
        client,
        command=["/bin/sh", "-c", 'printf "ok\\n" > "$1"', "probe", str(inside)],
        cwd=workspace,
        response_model=response_model,
    )
    inside_persisted = inside.is_file()
    inside.unlink(missing_ok=True)
    outside_result = _command_exec(
        client,
        command=["/bin/sh", "-c", 'printf "escape\\n" > "$1"', "probe", str(outside)],
        cwd=workspace,
        response_model=response_model,
    )
    outside_persisted = outside.exists()
    outside.unlink(missing_ok=True)
    proc_result = _command_exec(
        client,
        command=[
            "/bin/sh",
            "-c",
            (
                'test -r "/proc/$PPID/environ" || exit 0; '
                'tr "\\000" "\\n" < "/proc/$PPID/environ" | '
                "grep -Eiq '(^|_)(api_?key|access_?token|refresh_?token|authorization|cookie|password|secret)="
                "|://[^/@[:space:]]+:[^/@[:space:]]+@|bearer[[:space:]]' && exit 1; exit 0"
            ),
        ],
        cwd=workspace,
        response_model=response_model,
    )
    denied_results = [
        _command_exec(
            client,
            command=["/bin/sh", "-c", 'test ! -e "$1" || test ! -r "$1"', "probe", str(path)],
            cwd=workspace,
            response_model=response_model,
        )
        for path in denied_paths
    ]
    if writable_roots and (getattr(inside_result, "exit_code", 1) != 0 or not inside_persisted):
        raise CodexProtocolError("Codex permission profile denied an authorized workspace write probe")
    if not writable_roots and inside_persisted:
        raise CodexProtocolError("Codex read-only permission profile persisted a workspace write")
    if outside_persisted:
        raise CodexProtocolError("Codex permission profile persisted an outside-workspace write")
    if getattr(proc_result, "exit_code", 1) != 0:
        raise CodexProtocolError("Codex tool sandbox found credential material in its parent environment")
    if any(getattr(result, "exit_code", 1) != 0 for result in denied_results):
        raise CodexProtocolError("Codex tool sandbox could read provider profile or broker state")
    return {
        "adapter_version": CODEX_PROTOCOL_ADAPTER_VERSION,
        "permission_profile": CODEX_PERMISSION_PROFILE,
        "workspace_write_probe": "passed" if writable_roots else "read_only_passed",
        "outside_write_probe": "passed",
        "parent_environment_secret_probe": "passed",
        "provider_state_denial_probe": "passed",
        "provider_state_denied_path_count": len(denied_paths),
        "outside_command_exit_code": getattr(outside_result, "exit_code", None),
    }


def _command_exec(
    client: Any,
    *,
    command: list[str],
    cwd: Path,
    response_model: type[Any],
) -> Any:
    return client.request(
        "command/exec",
        {
            "command": command,
            "cwd": str(cwd),
            "permissionProfile": CODEX_PERMISSION_PROFILE,
            "timeoutMs": 15_000,
        },
        response_model=response_model,
    )


def _read_rate_limits(client: Any, response_model: type[Any]) -> Mapping[str, object]:
    try:
        response = client.request("account/rateLimits/read", {}, response_model=response_model)
    except Exception:  # noqa: BLE001 - optional account telemetry must not fail a turn
        return {}
    raw = response.model_dump(by_alias=True, mode="json")
    return raw if isinstance(raw, Mapping) else {}


def _rate_and_cost_summary(
    before: Mapping[str, object], after: Mapping[str, object], *, auth_mode: str
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    safe_before = _safe_rate_snapshot(before)
    safe_after = _safe_rate_snapshot(after)
    cost: dict[str, object] = {
        "basis": "chatgpt_subscription" if auth_mode == "chatgpt" else "api_billing",
        "currency": None,
        "amount": None,
        "reported": False,
    }
    before_balance = _credit_balance(before)
    after_balance = _credit_balance(after)
    if before_balance is not None and after_balance is not None:
        consumed = before_balance - after_balance
        cost.update(
            {
                "subscription_credit_balance_before": str(before_balance),
                "subscription_credit_balance_after": str(after_balance),
                "subscription_credits_consumed": str(consumed),
                "reported": True,
            }
        )
    return {"before": safe_before, "after": safe_after}, cost


def _safe_rate_snapshot(value: Mapping[str, object]) -> Mapping[str, object]:
    rate = value.get("rateLimits")
    raw = rate if isinstance(rate, Mapping) else {}
    result: dict[str, object] = {}
    for source, target in (
        ("planType", "plan_type"),
        ("rateLimitReachedType", "reached_type"),
        ("limitId", "limit_id"),
    ):
        item = raw.get(source)
        if isinstance(item, str):
            result[target] = item
    for source, target in (("primary", "primary"), ("secondary", "secondary")):
        window = raw.get(source)
        if isinstance(window, Mapping):
            result[target] = {
                key: child
                for key, child in {
                    "used_percent": window.get("usedPercent"),
                    "resets_at": window.get("resetsAt"),
                    "window_minutes": window.get("windowDurationMins"),
                }.items()
                if isinstance(child, int) and not isinstance(child, bool)
            }
    credit_snapshot = raw.get("credits")
    if isinstance(credit_snapshot, Mapping):
        result["credits"] = {
            "has_credits": credit_snapshot.get("hasCredits") is True,
            "unlimited": credit_snapshot.get("unlimited") is True,
            **({"balance": credit_snapshot["balance"]} if isinstance(credit_snapshot.get("balance"), str) else {}),
        }
    return result


def _credit_balance(value: Mapping[str, object]) -> Decimal | None:
    rate = value.get("rateLimits")
    credit_snapshot = rate.get("credits") if isinstance(rate, Mapping) else None
    balance = credit_snapshot.get("balance") if isinstance(credit_snapshot, Mapping) else None
    if not isinstance(balance, str):
        return None
    try:
        return Decimal(balance)
    except InvalidOperation:
        return None


def _safe_item_summary(item: Mapping[str, Any], *, workspace_root: Path) -> dict[str, object]:
    item_type = item.get("type") if isinstance(item.get("type"), str) else "unknown"
    status = _enum_string(item.get("status")) or "unknown"
    summary: dict[str, object] = {"item_type": item_type, "provider_status": status}
    if item_type == "commandExecution":
        summary.update(_safe_blob(item.get("command"), prefix="native_input"))
        cwd = item.get("cwd")
        if isinstance(cwd, str):
            relative = _relative_path(cwd, workspace_root=workspace_root)
            if relative is not None:
                summary["cwd_relative"] = relative
            else:
                summary.update(_safe_blob(cwd, prefix="cwd"))
        _copy_int(summary, item, "exitCode", "exit_code")
        _copy_int(summary, item, "durationMs", "duration_ms")
    elif item_type == "fileChange":
        paths: list[str] = []
        changes = item.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, Mapping) and isinstance(change.get("path"), str):
                    safe = _relative_path(str(change["path"]), workspace_root=workspace_root)
                    if safe is not None:
                        paths.append(safe)
        summary["change_count"] = len(changes) if isinstance(changes, list) else 0
        summary["paths"] = paths
    elif item_type == "mcpToolCall":
        summary.update(
            {
                "mcp_server": str(item.get("server") or "mcp"),
                "mcp_tool": str(item.get("tool") or "tool"),
                "native_input_digest": _digest_json(item.get("arguments")),
                "native_result_digest": _digest_json(item.get("result")),
                "native_error_digest": _digest_json(item.get("error")),
            }
        )
        _copy_int(summary, item, "durationMs", "duration_ms")
    elif item_type == "webSearch":
        summary.update(_safe_blob(item.get("query"), prefix="query"))
        summary["action_digest"] = _digest_json(item.get("action"))
    return summary


def _safe_token_usage(value: object) -> dict[str, object]:
    raw = value if isinstance(value, Mapping) else {}
    result: dict[str, object] = {}
    for source, target in (("last", "last"), ("total", "total")):
        breakdown = raw.get(source)
        if isinstance(breakdown, Mapping):
            result[target] = {
                target_key: child
                for source_key, target_key in (
                    ("inputTokens", "input_tokens"),
                    ("cachedInputTokens", "cached_input_tokens"),
                    ("outputTokens", "output_tokens"),
                    ("reasoningOutputTokens", "reasoning_output_tokens"),
                    ("totalTokens", "total_tokens"),
                )
                if isinstance((child := breakdown.get(source_key)), int) and not isinstance(child, bool)
            }
    context = raw.get("modelContextWindow")
    if isinstance(context, int) and not isinstance(context, bool):
        result["model_context_window"] = context
    return result


def _safe_error_summary(value: object) -> dict[str, object]:
    raw = value if isinstance(value, Mapping) else {}
    message = raw.get("message")
    payload = _safe_blob(message, prefix="error_message")
    if isinstance(message, str):
        payload["error_category"] = _error_category(message)
    code = raw.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        payload["error_code"] = code
    return payload


def _safe_blob(value: object, *, prefix: str) -> dict[str, object]:
    text = value if isinstance(value, str) else ""
    return {f"{prefix}_digest": _digest_text(text), f"{prefix}_length": len(text)}


def _error_category(message: str) -> str:
    lowered = message.lower()
    if any(term in lowered for term in ("rate limit", "usage limit", "quota", "credits")):
        return "usage_or_quota"
    if any(term in lowered for term in ("auth", "login", "subscription", "entitlement")):
        return "authentication_or_entitlement"
    if any(term in lowered for term in ("network", "connection", "timeout", "dns")):
        return "transport"
    if any(term in lowered for term in ("sandbox", "permission", "access denied")):
        return "sandbox_or_permission"
    return "unclassified"


def _relative_path(value: str, *, workspace_root: Path) -> str | None:
    try:
        return Path(value).resolve(strict=False).relative_to(workspace_root).as_posix()
    except ValueError:
        posix = PurePosixPath(value)
        if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
            return None
        return posix.as_posix()


def _validate_network_host(host: str) -> str:
    candidate = host.strip().lower().rstrip(".")
    if len(candidate) > 253:
        raise CodexProtocolError(f"invalid exact network host: {host!r}")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise CodexProtocolError(f"unsafe network host is not representable: {host!r}")
    labels = candidate.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise CodexProtocolError(f"invalid exact network host: {host!r}")
    if candidate == "localhost" or candidate.endswith(".localhost") or candidate == "metadata.google.internal":
        raise CodexProtocolError(f"unsafe network host is not representable: {host!r}")
    return candidate


def _approval_kind(method: str) -> str:
    return {
        "item/commandExecution/requestApproval": "command_execution",
        "item/fileChange/requestApproval": "file_change",
    }.get(method, "unsupported")


def _mapping_string(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


def _enum_string(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _copy_int(target: dict[str, object], source: Mapping[str, Any], key: str, target_key: str) -> None:
    value = source.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        target[target_key] = value


def _safe_method(method: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", ".", method).strip(".") or "unknown"


def _digest_json(value: object) -> str:
    return _digest_text(json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr))


def _digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()}"


def _toml_string(path: Path) -> str:
    return json.dumps(str(path))


__all__ = [
    "CODEX_PERMISSION_PROFILE",
    "CODEX_PROTOCOL_ADAPTER_VERSION",
    "CodexIngressProjector",
    "CodexProtocolError",
    "CodexTurnResult",
    "make_capturing_client",
    "prove_codex_sandbox",
    "run_codex_turn",
    "write_codex_permission_config",
]
