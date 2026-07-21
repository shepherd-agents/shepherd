"""Publishable headless Codex provider backed by the pinned Python app-server."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect.provider_capabilities import (
    BASH,
    EDIT_FILE,
    READ_FILE,
    SEARCH_CONTENT,
    SEARCH_FILES,
    WRITE_FILE,
    AgentProviderCapabilities,
    canonical_tool_payload,
)
from shepherd_dialect.provider_effects import (
    completed_file_change_paths,
    reconcile_provider_file_claims,
    snapshot_workspace_files,
)
from shepherd_dialect.provider_runtime import (
    MODEL_CALL,
    MODEL_TURN,
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED,
    PROVIDER_INVOCATION_STARTED,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_REJECTED,
    TOOL_CALL_STARTED,
    ExecutionProviderResult,
    ProviderEvent,
    ProviderInvocationError,
    ProviderInvocationResult,
    digest_jsonable,
    provider_invocation_outcome,
    redacted_text_payload,
)
from shepherd_dialect.provider_stream import (
    ProviderProcessRequest,
    ProviderStreamError,
    supervise_provider_process,
)
from shepherd_dialect.providers._common import _invocation_id, _provider_prompt
from shepherd_dialect.providers.codex_profile import (
    CODEX_TESTED_VERSION,
    ResolvedCodexProfile,
    codex_profile_lock,
    resolve_codex_profile,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vcs_core.runtime_substrate import HandlerStack
    from vcs_core.spi import DriverContext

    from shepherd_dialect.provider_activity import ProviderActivity, ProviderActivityManifest


class CodexProviderError(RuntimeError):
    """Raised when the headless Codex provider cannot complete safely."""


@dataclass(frozen=True)
class CodexAgentProvider:
    """Python Codex app-server provider with ChatGPT subscription auth.

    The short-lived broker and authenticated app-server are trusted control
    plane.  They run outside the outer workspace jail so Codex can establish
    its own Bubblewrap/Seatbelt permission profile for model-selected tools.
    The carrier remains authoritative for the final workspace tree.
    """

    provider_id: str = "codex"
    prompt: str = ""
    model: str = "gpt-5.4"
    profile_id: str = "default"
    auth_mode: str = "chatgpt"
    output_schema: Mapping[str, Any] | None = None
    budget_seconds: int = 300
    _test_app_server_argv: tuple[str, ...] | None = field(default=None, repr=False)
    _test_app_server_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    _allow_fake_runtime: bool = field(default=False, repr=False)

    @property
    def capabilities(self) -> AgentProviderCapabilities:
        return AgentProviderCapabilities(
            provider_id=self.provider_id,
            transport="app_server_broker",
            confined=True,
            network_required=True,
            structured_output=True,
            session_resume=False,
            workspace_tools=frozenset({READ_FILE, WRITE_FILE, EDIT_FILE, SEARCH_FILES, SEARCH_CONTENT, BASH}),
            custom_tools=False,
            mcp=True,
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
        del stack, context
        if execution is None or confinement is None:
            raise ExecutionAuthorityRequired("Codex requires per-run execution authority and a lowered tool policy")
        workspace = Path(execution.working_path).resolve()
        prompt = _provider_prompt(self.prompt, task_body, args, "CodexAgentProvider")
        invocation_id = _invocation_id(self.provider_id, execution)
        started = ProviderEvent(
            kind=PROVIDER_INVOCATION_STARTED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=0,
            event_id=f"{invocation_id}:started",
            model=self.model,
            payload={
                "prompt_digest": digest_jsonable({"prompt": prompt}),
                "transport": "app_server_broker",
                "sdk_version": CODEX_TESTED_VERSION,
                "auth_mode": self.auth_mode,
                "profile_id_digest": _digest_text(self.profile_id),
                "approval_policy": "never",
                "provider_tool_sandbox": True,
                "output_schema_present": self.output_schema is not None,
            },
        )
        activities: tuple[ProviderActivity, ...] = ()
        manifest: ProviderActivityManifest | None = None
        before_tree: Mapping[str, str] | None = None
        try:
            profile = resolve_codex_profile(self.profile_id)
            _assert_profile_disjoint_from_workspace(profile, workspace=workspace)
            if profile.mode != self.auth_mode:
                raise CodexProviderError(
                    f"Codex profile mode is {profile.mode!r}, but the invocation requested {self.auth_mode!r}"
                )
            writable_roots = _canonical_writable_roots(confinement, workspace=workspace)
            network_mode, allowed_hosts = _network_policy(confinement)
            payload: dict[str, object] = {
                "credential_home": str(profile.credential_home),
                "credential_auth": str(profile.auth_path),
                "profile_root": str(profile.profile_root),
                "profile_id": profile.profile_id,
                "auth_mode": profile.mode,
                "model": self.model,
                "prompt": prompt,
                "output_schema": dict(self.output_schema) if self.output_schema is not None else None,
                "deadline_seconds": self.budget_seconds,
                "writable_roots": [str(path) for path in writable_roots],
                "network_mode": network_mode,
                "allowed_hosts": list(allowed_hosts),
                "allow_fake_runtime": self._allow_fake_runtime,
            }
            if self._test_app_server_argv is not None:
                payload["app_server_argv"] = list(self._test_app_server_argv)
                payload["app_server_env"] = dict(self._test_app_server_env)
            before_tree = snapshot_workspace_files(workspace)
            with codex_profile_lock(self.profile_id):
                process_result = supervise_provider_process(
                    ProviderProcessRequest(
                        adapter_id="codex-python",
                        provider_id=self.provider_id,
                        invocation_id=invocation_id,
                        working_directory=workspace,
                        payload=payload,
                        deadline_seconds=float(self.budget_seconds + 20),
                    )
                )
            activities = process_result.activities
            manifest = process_result.manifest
            assert before_tree is not None
            reconciliation = reconcile_provider_file_claims(
                before=before_tree,
                after=snapshot_workspace_files(workspace),
                provider_paths=completed_file_change_paths(activities),
            )
            raw = process_result.result
            output_text = _mapping_string(raw, "output_text") or ""
            structured = raw.get("structured_output")
            usage = raw.get("usage")
            cost = raw.get("cost")
            rate_limits = raw.get("rate_limits")
            projected = _provider_events_from_activities(
                activities,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                model=self.model,
                sequence_start=2,
            )
            model_call = ProviderEvent(
                kind=MODEL_CALL,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=1,
                event_id=f"{invocation_id}:model-call",
                model=self.model,
                caused_by_event_ids=(started.event_id,),
                payload={
                    "transport": "app_server_broker",
                    "thread_id_present": isinstance(raw.get("thread_id"), str),
                    "usage": dict(usage) if isinstance(usage, Mapping) else {},
                    **redacted_text_payload(output_text, field="output_text"),
                },
            )
            turn_event = ProviderEvent(
                kind=MODEL_TURN,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=2 + len(projected),
                event_id=f"{invocation_id}:model-turn",
                model=self.model,
                caused_by_event_ids=(model_call.event_id,),
                payload={
                    "terminal": str(raw.get("terminal") or "completed"),
                    "usage": dict(usage) if isinstance(usage, Mapping) else {},
                    "cost": dict(cost) if isinstance(cost, Mapping) else {},
                    **redacted_text_payload(output_text, field="text"),
                },
            )
            completed = ProviderEvent(
                kind=PROVIDER_INVOCATION_COMPLETED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=turn_event.sequence + 1,
                event_id=f"{invocation_id}:completed",
                model=self.model,
                caused_by_event_ids=(turn_event.event_id,),
                payload={
                    "returncode": process_result.returncode,
                    "activity_count": manifest.activity_count,
                    "ingress_count": manifest.ingress_count,
                    "activity_manifest_digest": manifest.last_record_digest,
                    "sdk_version": str(raw.get("sdk_version") or CODEX_TESTED_VERSION),
                    "runtime_version": str(raw.get("runtime_version") or "unknown"),
                    "auth_mode": profile.mode,
                    "approval_policy": "never",
                    "provider_tool_sandbox": dict(raw.get("sandbox_evidence"))
                    if isinstance(raw.get("sandbox_evidence"), Mapping)
                    else {},
                    "file_effect_reconciliation_digest": digest_jsonable(reconciliation),
                    "file_effect_classification_counts": reconciliation["classification_counts"],
                },
            )
            events = (started, model_call, *projected, turn_event, completed)
            result = ProviderInvocationResult(
                output_text=output_text,
                structured_output=dict(structured) if isinstance(structured, Mapping) else {},
                session_id=_mapping_string(raw, "thread_id"),
                usage=dict(usage) if isinstance(usage, Mapping) else {},
                events=events,
                metadata={
                    "cost": dict(cost) if isinstance(cost, Mapping) else {},
                    "rate_limits": dict(rate_limits) if isinstance(rate_limits, Mapping) else {},
                    "activity_manifest": manifest.as_wire_record(),
                    "auth_mode": profile.mode,
                    "profile_id_digest": _digest_text(self.profile_id),
                    "sdk_version": str(raw.get("sdk_version") or CODEX_TESTED_VERSION),
                    "runtime_version": str(raw.get("runtime_version") or "unknown"),
                    "permission_profile_digest": raw.get("permission_profile_digest"),
                    "sandbox_evidence": dict(raw.get("sandbox_evidence"))
                    if isinstance(raw.get("sandbox_evidence"), Mapping)
                    else {},
                    "file_effect_reconciliation": reconciliation,
                },
            )
            return ExecutionProviderResult(
                outcome=provider_invocation_outcome(
                    result,
                    provider_id=self.provider_id,
                    invocation_id=invocation_id,
                ),
                provider_events=events,
                provider_activities=activities,
                activity_manifest=manifest,
            )
        except ProviderInvocationError:
            raise
        except Exception as exc:
            if isinstance(exc, ProviderStreamError):
                activities = exc.activities
                manifest = exc.manifest
            failure_outcome: dict[str, object] = {}
            if before_tree is not None:
                try:
                    failure_outcome["file_effect_reconciliation"] = reconcile_provider_file_claims(
                        before=before_tree,
                        after=snapshot_workspace_files(workspace),
                        provider_paths=completed_file_change_paths(activities),
                    )
                except Exception as reconciliation_exc:  # noqa: BLE001 - retain the primary provider failure
                    failure_outcome["file_effect_reconciliation"] = {
                        "basis": "provider_claim_vs_carrier_tree",
                        "complete": False,
                        "error_type": type(reconciliation_exc).__name__,
                        "error_digest": _digest_text(str(reconciliation_exc)),
                    }
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=1,
                event_id=f"{invocation_id}:failed",
                model=self.model,
                caused_by_event_ids=(started.event_id,),
                payload={
                    "error_type": type(exc).__name__,
                    "error_digest": _digest_text(str(exc)),
                    "error_length": len(str(exc)),
                    "activity_count": len(activities),
                    "activity_manifest_present": manifest is not None,
                },
            )
            raise ProviderInvocationError(
                f"Codex provider failed ({type(exc).__name__})",
                provider_events=(started, failed),
                provider_activities=activities,
                activity_manifest=manifest,
                outcome=failure_outcome,
            ) from exc


def _provider_events_from_activities(
    activities: tuple[ProviderActivity, ...],
    *,
    provider_id: str,
    invocation_id: str,
    model: str,
    sequence_start: int,
) -> tuple[ProviderEvent, ...]:
    events: list[ProviderEvent] = []
    starts: dict[tuple[str | None, str | None, str], str] = {}
    for activity in activities:
        item_type = activity.payload.get("item_type")
        if item_type not in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
            continue
        if activity.item_id is None:
            continue
        key = (activity.thread_id, activity.turn_id, activity.item_id)
        native_name = _tool_name(activity)
        if activity.method == "item/started":
            event_id = f"{invocation_id}:activity:{activity.sequence}"
            starts[key] = event_id
            events.append(
                ProviderEvent(
                    kind=TOOL_CALL_STARTED,
                    provider_id=provider_id,
                    invocation_id=invocation_id,
                    sequence=sequence_start + len(events),
                    event_id=event_id,
                    model=model,
                    tool_call_id=activity.item_id,
                    payload={
                        **canonical_tool_payload(native_name),
                        "activity_event_id": activity.event_id,
                        "evidence_source": activity.source,
                    },
                )
            )
            continue
        if activity.method != "item/completed" or key not in starts:
            continue
        status = activity.payload.get("provider_status")
        status_text = status if isinstance(status, str) else "unknown"
        events.append(
            ProviderEvent(
                kind=TOOL_CALL_REJECTED if status_text == "declined" else TOOL_CALL_COMPLETED,
                provider_id=provider_id,
                invocation_id=invocation_id,
                sequence=sequence_start + len(events),
                event_id=f"{invocation_id}:activity:{activity.sequence}",
                model=model,
                tool_call_id=activity.item_id,
                caused_by_event_ids=(starts[key],),
                payload={
                    **canonical_tool_payload(native_name),
                    "activity_event_id": activity.event_id,
                    "evidence_source": activity.source,
                    "success": status_text in {"completed", "success"},
                    "provider_status": status_text,
                },
            )
        )
    return tuple(events)


def _tool_name(activity: ProviderActivity) -> str:
    item_type = activity.payload.get("item_type")
    if item_type == "commandExecution":
        return "Bash"
    if item_type == "fileChange":
        return "Edit"
    if item_type == "webSearch":
        return "WebSearch"
    if item_type == "mcpToolCall":
        server = activity.payload.get("mcp_server")
        tool = activity.payload.get("mcp_tool")
        return f"mcp__{server if isinstance(server, str) else 'mcp'}__{tool if isinstance(tool, str) else 'tool'}"
    return "CodexItem"


def _canonical_writable_roots(confinement: Any, *, workspace: Path) -> tuple[Path, ...]:
    raw_roots = getattr(confinement, "writable_roots", ())
    roots = tuple(Path(str(root)).resolve() for root in raw_roots)
    for root in roots:
        try:
            root.relative_to(workspace)
        except ValueError as exc:
            raise CodexProviderError("Codex writable grants must be inside the run workspace") from exc
    return roots


def _network_policy(confinement: Any) -> tuple[str, tuple[str, ...]]:
    network = getattr(confinement, "network", None)
    mode = getattr(getattr(network, "mode", None), "value", None)
    if mode == "deny_all":
        return "deny_all", ()
    if mode == "allow_all":
        return "allow_all", ()
    if mode == "broker":
        hosts = getattr(network, "allowed_hosts", ())
        return "broker", tuple(str(host) for host in hosts)
    raise CodexProviderError("Codex cannot lower the requested network policy")


def _assert_profile_disjoint_from_workspace(profile: ResolvedCodexProfile, *, workspace: Path) -> None:
    protected_paths = (profile.profile_root.resolve(), profile.credential_home.resolve(), profile.auth_path.resolve())
    for protected in protected_paths:
        if protected == workspace or protected.is_relative_to(workspace) or workspace.is_relative_to(protected):
            raise CodexProviderError("Codex authentication state must not overlap the run workspace")


def _mapping_string(value: Mapping[str, object], key: str) -> str | None:
    raw = value.get(key)
    return raw if isinstance(raw, str) else None


def _digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()}"


__all__ = ["CodexAgentProvider", "CodexProviderError"]
