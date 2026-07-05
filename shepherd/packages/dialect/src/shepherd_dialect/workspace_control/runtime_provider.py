"""Workspace-control runtime-provider planning and static provider lane."""

from __future__ import annotations

import base64
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from shepherd_dialect.provider_runtime import (
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED,
    PROVIDER_INVOCATION_STARTED,
    ExecutionProviderResult,
    ProviderEvent,
    ProviderInvocationError,
    digest_jsonable,
    redacted_text_payload,
)
from shepherd_dialect.providers import ClaudeHeadlessProvider
from shepherd_dialect.runtime_options import RuntimeOptions, RuntimeOptionsError, parse_runtime_options

if TYPE_CHECKING:
    from shepherd_dialect.workspace_control.schemas import TaskArtifactLock

JsonObject = dict[str, object]
WorkspaceRuntimeProviderKind = Literal["static", "claude"]
CLAUDE_WORKSPACE_INPUT_DIR = ".shepherd-inputs"
_CLAUDE_PRIVATE_RUNTIME_DIRS = (CLAUDE_WORKSPACE_INPUT_DIR, ".claude-scratch", ".claude-sdk-scratch")


class WorkspaceRuntimePlanError(ValueError):
    """Raised when a workspace-control runtime envelope cannot be planned."""


class _ClaudePrivateRuntimeCleanupError(RuntimeError):
    """Raised when private Claude runtime paths remain after cleanup."""

    def __init__(self, details: tuple[str, ...]) -> None:
        self.details = details
        suffix = "; ".join(details)
        super().__init__(f"Claude private runtime cleanup failed; refusing retained publication: {suffix}")


@dataclass(frozen=True)
class RuntimeProviderTaskExecutorDescriptor:
    """Run-ledger descriptor for a workspace-control runtime provider."""

    executor_kind: Literal["in_process", "confined_process"]
    executor_id: str = "shepherd.workspace_control.executor.runtime_provider.v0"
    executor_policy: str = "provider_runtime"


@dataclass(frozen=True)
class WorkspaceRunRuntimePlan:
    """Validated public runtime envelope for a workspace-control run."""

    requested: RuntimeOptions
    supplied: bool
    provider_kind: WorkspaceRuntimeProviderKind | None = None
    provider_id: str | None = None
    model_name: str | None = None

    @property
    def uses_execution_provider(self) -> bool:
        return self.provider_kind is not None

    def policy_payload(self) -> JsonObject | None:
        if not self.supplied:
            return None
        resolved: JsonObject = {}
        if self.provider_id is not None:
            resolved["provider"] = self.provider_id
        if self.model_name is not None:
            resolved["model"] = self.model_name
        return {
            "requested": self.requested.to_payload(),
            "resolved": resolved,
        }


@dataclass(frozen=True)
class WorkspaceRuntimeInputArtifact:
    """One explicit retained-output artifact materialized for a live provider."""

    source_run_ref: str
    source_output_id: str
    source_output_name: str
    source_binding: str
    source_path: str
    materialized_path: str
    content: bytes
    label: str | None = None
    content_digest: str | None = None

    def manifest_entry(self) -> JsonObject:
        payload: JsonObject = {
            "source_run_ref": self.source_run_ref,
            "source_output_id": self.source_output_id,
            "source_output_name": self.source_output_name,
            "source_binding": self.source_binding,
            "source_path": self.source_path,
            "materialized_path": self.materialized_path,
            "byte_length": len(self.content),
        }
        if self.label is not None:
            payload["label"] = self.label
        if self.content_digest is not None:
            payload["content_digest"] = self.content_digest
        return payload


@dataclass(frozen=True)
class _WorkspaceClaudeInvocation:
    """Structured private invocation envelope for the built-in Claude lane."""

    provider_id: str
    prompt: str
    model_name: str | None
    task_lock: TaskArtifactLock
    kwargs: Mapping[str, Any]
    input_artifacts: tuple[WorkspaceRuntimeInputArtifact, ...]


@dataclass(frozen=True)
class _WorkspaceRuntimeProviderTransports:
    """Private built-in transport seam.

    This is deliberately not a public provider plugin ABI. It lets
    workspace-control own the release contract while tests inject deterministic
    transports at the internal Claude boundary.
    """

    claude: Callable[[_WorkspaceClaudeInvocation], Any]


def _default_claude_transport(invocation: _WorkspaceClaudeInvocation) -> Any:
    return ClaudeHeadlessProvider(
        provider_id=invocation.provider_id,
        prompt=invocation.prompt,
        model=invocation.model_name,
    )


_WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS = _WorkspaceRuntimeProviderTransports(
    claude=_default_claude_transport,
)


@dataclass(frozen=True)
class StaticWorkspaceRuntimeProvider:
    """Deterministic static provider for the public workspace-control lane.

    This provider creates a retained workspace artifact itself. It uses
    ``launch_confined`` when the run placement resolved to a jail and writes
    directly only for advisory placement.
    """

    task_lock: TaskArtifactLock
    kwargs: Mapping[str, Any]
    model_name: str | None
    enforce_with_launch_confined: bool
    launch_metadata: dict[str, object] | None = None
    provider_id: str = "static"

    def execute(
        self,
        task_body: Callable[..., Any] | None,
        stack: Any,
        context: Any,
        args: Mapping[str, Any],
        *,
        execution: Any = None,
        confinement: Any = None,
    ) -> ExecutionProviderResult:
        del task_body, stack, context, args
        if execution is None:
            from vcs_core.spi import ExecutionAuthorityRequired

            raise ExecutionAuthorityRequired("static workspace runtime provider requires execution authority")
        path, content = _static_runtime_artifact(self.task_lock, self.kwargs)
        invocation_id = _static_runtime_invocation_id(self.provider_id, execution)
        started = ProviderEvent(
            kind=PROVIDER_INVOCATION_STARTED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=0,
            event_id=f"{invocation_id}:started",
            model=self.model_name,
            payload={
                "task_id": self.task_lock.task_id,
                "task_version": self.task_lock.version,
                "artifact_path": path,
                "args_digest": digest_jsonable(dict(self.kwargs)),
            },
        )
        try:
            if self.enforce_with_launch_confined:
                if confinement is None:
                    from vcs_core.spi import ExecutionAuthorityRequired

                    raise ExecutionAuthorityRequired("static workspace runtime provider requires confinement")
                if self.launch_metadata is not None:
                    self.launch_metadata["launch_confined_attempted"] = True
                proc = execution.launch_confined(
                    _static_runtime_write_command(path, content),
                    confinement,
                )
                if proc.returncode != 0:
                    message = (
                        f"static provider confined write refused (rc={proc.returncode}): "
                        f"{(proc.stderr or proc.stdout or '').strip()[-300:]}"
                    )
                    failed = ProviderEvent(
                        kind=PROVIDER_INVOCATION_FAILED,
                        provider_id=self.provider_id,
                        invocation_id=invocation_id,
                        sequence=1,
                        event_id=f"{invocation_id}:failed",
                        model=self.model_name,
                        payload={
                            "returncode": proc.returncode,
                            "error_type": "StaticProviderWriteRefused",
                            **redacted_text_payload(message, field="error"),
                            **redacted_text_payload(proc.stdout or "", field="stdout"),
                            **redacted_text_payload(proc.stderr or "", field="stderr"),
                        },
                    )
                    _record_provider_events(self.launch_metadata, (started, failed))
                    raise ProviderInvocationError(message, provider_events=(started, failed))
            else:
                _write_static_runtime_artifact(Path(execution.working_path), path, content)
        except ProviderInvocationError:
            raise
        except Exception as exc:
            failed = ProviderEvent(
                kind=PROVIDER_INVOCATION_FAILED,
                provider_id=self.provider_id,
                invocation_id=invocation_id,
                sequence=1,
                event_id=f"{invocation_id}:failed",
                model=self.model_name,
                payload={
                    "error_type": type(exc).__name__,
                    **redacted_text_payload(str(exc), field="error"),
                },
            )
            _record_provider_events(self.launch_metadata, (started, failed))
            raise ProviderInvocationError(str(exc), provider_events=(started, failed)) from exc

        completed = ProviderEvent(
            kind=PROVIDER_INVOCATION_COMPLETED,
            provider_id=self.provider_id,
            invocation_id=invocation_id,
            sequence=1,
            event_id=f"{invocation_id}:completed",
            model=self.model_name,
            payload={
                "artifact_path": path,
                "content_digest": digest_jsonable({"path": path, "content": content.decode("utf-8", "replace")}),
                "launched_confined": self.enforce_with_launch_confined,
            },
        )
        _record_provider_events(self.launch_metadata, (started, completed))
        return ExecutionProviderResult(
            outcome={
                "schema": "shepherd.workspace_control.static_provider_outcome.v1",
                "status": "ok",
                "provider_id": self.provider_id,
                "model": self.model_name,
                "artifact_path": path,
                "artifact_bytes": len(content),
            },
            provider_events=(started, completed),
        )


@dataclass(frozen=True)
class ClaudeWorkspaceRuntimeProvider:
    """Bounded built-in Claude lane for workspace-control retained runs.

    The public surface is ``runtime={"provider": "claude"}``; this adapter owns
    workspace-control prompt construction, cited-input staging, metadata
    projection, and fail-closed launch evidence while delegating the actual
    local CLI invocation/parsing to the existing headless Claude provider.
    """

    task_lock: TaskArtifactLock
    artifact_payload: Mapping[str, Any]
    kwargs: Mapping[str, Any]
    model_name: str | None
    input_artifacts: tuple[WorkspaceRuntimeInputArtifact, ...] = ()
    launch_metadata: dict[str, object] | None = None
    provider_id: str = "claude"

    def execute(
        self,
        task_body: Callable[..., Any] | None,
        stack: Any,
        context: Any,
        args: Mapping[str, Any],
        *,
        execution: Any = None,
        confinement: Any = None,
    ) -> ExecutionProviderResult:
        del task_body, args
        if execution is None or confinement is None:
            from vcs_core.spi import ExecutionAuthorityRequired

            raise ExecutionAuthorityRequired("Claude workspace runtime provider requires execution authority")
        root = Path(execution.working_path)
        prompt = _claude_runtime_prompt(
            task_lock=self.task_lock,
            artifact_payload=self.artifact_payload,
            kwargs=self.kwargs,
            input_artifacts=self.input_artifacts,
        )
        _stage_claude_input_artifacts(root, self.input_artifacts)
        invocation = _WorkspaceClaudeInvocation(
            provider_id=self.provider_id,
            prompt=prompt,
            model_name=self.model_name,
            task_lock=self.task_lock,
            kwargs=self.kwargs,
            input_artifacts=self.input_artifacts,
        )
        _record_claude_invocation_metadata(self.launch_metadata, invocation)
        proxied_execution = _LaunchMetadataExecutionProxy(execution, self.launch_metadata)
        try:
            provider = _WORKSPACE_RUNTIME_PROVIDER_TRANSPORTS.claude(invocation)
            result = provider.execute(None, stack, context, {}, execution=proxied_execution, confinement=confinement)
        except ProviderInvocationError as exc:
            _scrub_claude_private_runtime_dirs_best_effort(root)
            _record_provider_events(self.launch_metadata, exc.provider_events)
            raise
        except Exception as exc:
            _scrub_claude_private_runtime_dirs_best_effort(root)
            events = _claude_runtime_failure_events(
                provider_id=self.provider_id,
                model_name=self.model_name,
                execution=execution,
                prompt=prompt,
                exc=exc,
            )
            _record_provider_events(self.launch_metadata, events)
            raise ProviderInvocationError(str(exc), provider_events=events) from exc

        try:
            _scrub_claude_private_runtime_dirs(root)
        except _ClaudePrivateRuntimeCleanupError as exc:
            events = _claude_runtime_failure_events(
                provider_id=self.provider_id,
                model_name=self.model_name,
                execution=execution,
                prompt=prompt,
                exc=exc,
            )
            _record_provider_events(self.launch_metadata, events)
            raise ProviderInvocationError(str(exc), provider_events=events) from exc

        provider_events = tuple(getattr(result, "provider_events", ()))
        _record_provider_events(self.launch_metadata, provider_events)
        return result


def resolve_workspace_run_runtime_plan(value: Mapping[str, object] | RuntimeOptions | None) -> WorkspaceRunRuntimePlan:
    """Return the v0.1.1 runtime-provider plan for a workspace-control run."""
    supplied = value is not None
    try:
        requested = parse_runtime_options(value)
    except RuntimeOptionsError as exc:
        raise WorkspaceRuntimePlanError(f"invalid runtime: {exc}") from exc
    provider_id = requested.provider.id.strip() if requested.provider is not None else None
    model_name = requested.model.name.strip() if requested.model is not None else None
    if provider_id is None:
        if model_name is not None:
            raise WorkspaceRuntimePlanError("runtime.model requires runtime.provider")
        return WorkspaceRunRuntimePlan(requested=requested, supplied=supplied)
    normalized = provider_id.lower()
    if normalized == "static":
        return WorkspaceRunRuntimePlan(
            requested=requested,
            supplied=supplied,
            provider_kind="static",
            provider_id="static",
            model_name=model_name,
        )
    if normalized == "claude":
        return WorkspaceRunRuntimePlan(
            requested=requested,
            supplied=supplied,
            provider_kind="claude",
            provider_id="claude",
            model_name=model_name,
        )
    if normalized in {"codex", "codex-sdk"}:
        raise WorkspaceRuntimePlanError("runtime provider 'codex' is deferred for v0.1.1")
    if normalized in {"claude", "claude-headless", "claude-api"}:
        raise WorkspaceRuntimePlanError("runtime provider aliases for Claude are not public in v0.1.1; use 'claude'")
    raise WorkspaceRuntimePlanError(f"unsupported runtime provider: {provider_id!r}")


def _static_runtime_artifact(
    task_lock: TaskArtifactLock,
    kwargs: Mapping[str, Any],
) -> tuple[str, bytes]:
    raw_path = kwargs.get("output_path", kwargs.get("artifact_path", "index.html"))
    if not isinstance(raw_path, str):
        raise TypeError("static runtime output_path must be a string")
    path = _validate_static_runtime_artifact_path(raw_path)
    if "output_text" in kwargs:
        raw_content = kwargs["output_text"]
    elif "artifact_text" in kwargs:
        raw_content = kwargs["artifact_text"]
    elif "output_content" in kwargs:
        raw_content = kwargs["output_content"]
    else:
        raw_content = _default_static_runtime_artifact_text(task_lock, kwargs)
    return path, _static_runtime_content_bytes(raw_content)


def _validate_static_runtime_artifact_path(path: str) -> str:
    parsed = PurePosixPath(path)
    if path in {"", ".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("static runtime artifact path must be a relative POSIX path")
    return parsed.as_posix()


def _static_runtime_content_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, indent=2, sort_keys=True, default=str).encode("utf-8")


def _default_static_runtime_artifact_text(task_lock: TaskArtifactLock, kwargs: Mapping[str, Any]) -> str:
    payload = {
        "task_id": task_lock.task_id,
        "task_version": task_lock.version,
        "args": dict(kwargs),
    }
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return (
        "<!doctype html>\n"
        '<meta charset="utf-8">\n'
        "<title>Static Runtime Output</title>\n"
        "<pre>" + body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>\n"
    )


def _write_static_runtime_artifact(root: Path, path: str, content: bytes) -> None:
    _validate_static_runtime_artifact_path(path)
    target = root / PurePosixPath(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _static_runtime_write_command(path: str, content: bytes) -> list[str]:
    _validate_static_runtime_artifact_path(path)
    encoded = base64.b64encode(content).decode("ascii")
    script = (
        "import base64, pathlib\n"
        f"path = pathlib.Path({path!r})\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        f"path.write_bytes(base64.b64decode({encoded!r}))\n"
    )
    return [sys.executable, "-B", "-c", script]


def _static_runtime_invocation_id(provider_id: str, execution: Any) -> str:
    identity = getattr(execution, "identity", None)
    scope_instance_id = getattr(identity, "scope_instance_id", None)
    scope_name = getattr(identity, "scope_name", None)
    return f"{provider_id}:{scope_instance_id or scope_name or 'unknown'}"


def _record_provider_events(
    metadata: dict[str, object] | None,
    events: tuple[ProviderEvent, ...],
) -> None:
    if metadata is None:
        return
    metadata["provider_events"] = [event.stable_payload() for event in events]


def _record_claude_invocation_metadata(
    metadata: dict[str, object] | None,
    invocation: _WorkspaceClaudeInvocation,
) -> None:
    if metadata is None:
        return
    metadata["provider_prompt_digest"] = digest_jsonable({"prompt": invocation.prompt})
    metadata["provider_input_manifest"] = [artifact.manifest_entry() for artifact in invocation.input_artifacts]
    metadata["provider_private_dirs"] = list(_CLAUDE_PRIVATE_RUNTIME_DIRS)


class _LaunchMetadataExecutionProxy:
    """Proxy an execution capability so metadata reflects actual jail launch."""

    def __init__(self, execution: Any, metadata: dict[str, object] | None) -> None:
        self._execution = execution
        self._metadata = metadata

    def __getattr__(self, name: str) -> Any:
        return getattr(self._execution, name)

    def launch_confined(self, command: list[str], confinement: object) -> object:
        if self._metadata is not None:
            self._metadata["launch_confined_attempted"] = True
        return self._execution.launch_confined(command, confinement)


def _claude_runtime_prompt(
    *,
    task_lock: TaskArtifactLock,
    artifact_payload: Mapping[str, Any],
    kwargs: Mapping[str, Any],
    input_artifacts: tuple[WorkspaceRuntimeInputArtifact, ...],
) -> str:
    entrypoint = artifact_payload.get("entrypoint")
    source_files = _claude_prompt_source_files(artifact_payload)
    args_json = json.dumps(_prompt_jsonable(dict(kwargs)), indent=2, sort_keys=True, default=str)
    manifest = [artifact.manifest_entry() for artifact in input_artifacts]
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
    source_block = "\n\n".join(f"### {path}\n```python\n{content}\n```" for path, content in source_files)
    if not source_block:
        source_block = "(no source text available)"
    return (
        "You are executing one Shepherd workspace-control task as a local Claude agent.\n"
        "Work only in the current working directory. Create or update only the artifacts requested by the task.\n"
        "Do not persist credentials, provider config, prompts, transcripts, or scratch files.\n"
        "The framework will retain files you write in the working directory after you exit.\n\n"
        f"Task id: {task_lock.task_id}\n"
        f"Task version: {task_lock.version}\n"
        f"Entrypoint: {json.dumps(entrypoint, sort_keys=True, default=str)}\n\n"
        "Task contract source:\n"
        f"{source_block}\n\n"
        "Durable task arguments:\n"
        f"```json\n{args_json}\n```\n\n"
        "Explicit retained-output input artifacts are materialized before launch under "
        f"`{CLAUDE_WORKSPACE_INPUT_DIR}/` and are listed here:\n"
        f"```json\n{manifest_json}\n```\n\n"
        "If the task asks for JSON, write a valid JSON object to the requested output path. "
        "If the task asks for HTML, write a complete self-contained HTML document to the requested output path. "
        "Return a short textual summary only after writing the requested file artifacts."
    )


def _claude_prompt_source_files(artifact_payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    files = artifact_payload.get("files")
    if not isinstance(files, list | tuple):
        return ()
    out: list[tuple[str, str]] = []
    for raw in files:
        if not isinstance(raw, Mapping):
            continue
        path = raw.get("path")
        content = raw.get("content")
        if isinstance(path, str) and isinstance(content, str):
            out.append((path, content))
    return tuple(out)


def _prompt_jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {
            "kind": "shepherd.workspace_control.redacted_python_argument.v1",
            "type": "bytes",
            "byte_length": len(value),
            "content_digest": digest_jsonable({"bytes_b64": base64.b64encode(value).decode("ascii")}),
        }
    if isinstance(value, Mapping):
        to_json = getattr(value, "to_json", None)
        if callable(to_json):
            raw = to_json()
            if isinstance(raw, Mapping):
                return _prompt_jsonable(raw)
        return {str(key): _prompt_jsonable(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_prompt_jsonable(child) for child in value]
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        raw = to_json()
        if isinstance(raw, Mapping):
            return _prompt_jsonable(raw)
    return repr(value)[:240]


def _stage_claude_input_artifacts(root: Path, artifacts: tuple[WorkspaceRuntimeInputArtifact, ...]) -> None:
    for artifact in artifacts:
        _validate_static_runtime_artifact_path(artifact.materialized_path)
        if not artifact.materialized_path.startswith(f"{CLAUDE_WORKSPACE_INPUT_DIR}/"):
            raise ValueError("Claude input artifact materialized path must live under the private input directory")
        target = root / PurePosixPath(artifact.materialized_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(artifact.content)


def _scrub_claude_private_runtime_dirs(root: Path) -> None:
    cleanup_errors: list[str] = []
    for dirname in _CLAUDE_PRIVATE_RUNTIME_DIRS:
        target = root / dirname
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(f"{dirname}: {type(exc).__name__}: {exc}")
    remaining = [
        dirname
        for dirname in _CLAUDE_PRIVATE_RUNTIME_DIRS
        if (root / dirname).exists() or (root / dirname).is_symlink()
    ]
    if cleanup_errors or remaining:
        remaining_errors = tuple(f"{dirname}: still exists after cleanup" for dirname in remaining)
        raise _ClaudePrivateRuntimeCleanupError((*cleanup_errors, *remaining_errors))


def _scrub_claude_private_runtime_dirs_best_effort(root: Path) -> None:
    with suppress(_ClaudePrivateRuntimeCleanupError):
        _scrub_claude_private_runtime_dirs(root)


def _claude_runtime_failure_events(
    *,
    provider_id: str,
    model_name: str | None,
    execution: Any,
    prompt: str,
    exc: BaseException,
) -> tuple[ProviderEvent, ProviderEvent]:
    invocation_id = _static_runtime_invocation_id(provider_id, execution)
    model = model_name or "claude-code-cli"
    started = ProviderEvent(
        kind=PROVIDER_INVOCATION_STARTED,
        provider_id=provider_id,
        invocation_id=invocation_id,
        sequence=0,
        event_id=f"{invocation_id}:started",
        model=model,
        payload={
            "prompt_digest": digest_jsonable({"prompt": prompt}),
            "transport": "headless_cli",
            "network_credential_posture": "advisory",
        },
    )
    failed = ProviderEvent(
        kind=PROVIDER_INVOCATION_FAILED,
        provider_id=provider_id,
        invocation_id=invocation_id,
        sequence=1,
        event_id=f"{invocation_id}:failed",
        model=model,
        payload={
            "error_type": type(exc).__name__,
            **redacted_text_payload(str(exc), field="error"),
        },
    )
    return (started, failed)


__all__ = [
    "CLAUDE_WORKSPACE_INPUT_DIR",
    "ClaudeWorkspaceRuntimeProvider",
    "RuntimeProviderTaskExecutorDescriptor",
    "StaticWorkspaceRuntimeProvider",
    "WorkspaceRunRuntimePlan",
    "WorkspaceRuntimeInputArtifact",
    "WorkspaceRuntimePlanError",
    "resolve_workspace_run_runtime_plan",
]
