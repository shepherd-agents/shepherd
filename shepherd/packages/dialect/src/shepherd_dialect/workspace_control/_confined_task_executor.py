"""Confined subprocess adapter for retained workspace-control task execution."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal


class ConfinedTaskExecutionError(RuntimeError):
    """Structured confined-task failure surfaced to run terminalization."""

    phase: Literal["prelaunch_refused", "monitor_refused", "body_refused"]
    refusal_type: str
    detail: str
    monitor_established: bool

    def __init__(
        self,
        *,
        phase: Literal["prelaunch_refused", "monitor_refused", "body_refused"],
        refusal_type: str,
        detail: str,
        monitor_established: bool,
    ) -> None:
        self.phase = phase
        self.refusal_type = refusal_type
        self.detail = detail
        self.monitor_established = monitor_established
        super().__init__(detail)

    @classmethod
    def prelaunch(cls, exc: BaseException) -> ConfinedTaskExecutionError:
        return cls(
            phase="prelaunch_refused",
            refusal_type=type(exc).__name__,
            detail=str(exc),
            monitor_established=False,
        )

    @classmethod
    def monitor(cls, exc: BaseException) -> ConfinedTaskExecutionError:
        return cls(
            phase="monitor_refused",
            refusal_type=_monitor_refusal_type(exc),
            detail=str(exc),
            monitor_established=False,
        )

    @classmethod
    def body(cls, *, refusal_type: str, detail: str) -> ConfinedTaskExecutionError:
        return cls(
            phase="body_refused",
            refusal_type=refusal_type,
            detail=detail,
            monitor_established=True,
        )

    def evidence(self) -> dict[str, str]:
        return {"type": self.refusal_type, "message": self.detail}


@dataclass(frozen=True)
class ConfinedProcessTaskExecutorDescriptor:
    """Run-ledger descriptor for the confined subprocess workspace runner."""

    executor_kind: Literal["confined_process"] = "confined_process"
    executor_id: str = "shepherd.workspace_control.executor.confined_process.v0"
    executor_policy: str = "artifact_subprocess_syscall_jail"


@dataclass(frozen=True)
class ConfinedBindingAuthority:
    """One named binding's confined-run authority (Lane C LC-3e).

    ``param`` names the task parameter the in-body handle is injected as; ``binding`` is the
    bound name; ``authority`` is the clamped per-binding authority (``"readonly"``/``"readwrite"``
    — never a run-wide scalar, per the S2 tripwire); ``root`` is the bound subtree's
    **working-path-relative** POSIX path (``""``/``"."`` for the whole working path). The caller
    (LC-3f wiring) relativizes the workspace-absolute bound root against the run working path.
    """

    param: str
    binding: str
    authority: str
    root: str


@dataclass(frozen=True)
class ConfinedRootTaskProvider:
    """Execution-bound provider that runs one root task artifact in a jailed subprocess.

    Exactly one of ``repo_authority`` (the v0.1 single-binding form: one ``repo`` handle at the
    working path) or ``binding_authorities`` (Lane C LC-3e: one handle per named binding, each
    with its own clamped authority and sub-root) must be provided; ``_stage_request`` fails
    closed otherwise.
    """

    artifact_payload: Mapping[str, object]
    kwargs: Mapping[str, Any]
    repo_authority: str | None = None
    binding_authorities: tuple[ConfinedBindingAuthority, ...] | None = None
    launch_metadata: dict[str, object] | None = None
    provider_id: str = "workspace-control-confined-task"

    def execute(
        self,
        task_body: Callable[..., Any] | None,
        stack: Any,
        context: Any,
        args: Mapping[str, Any],
        *,
        execution: Any = None,
        confinement: Any = None,
    ) -> Mapping[str, Any]:
        del task_body, stack, context, args
        if execution is None or confinement is None:
            from vcs_core.spi import ExecutionAuthorityRequired

            raise ExecutionAuthorityRequired("confined workspace task execution requires execution authority")
        with tempfile.TemporaryDirectory(prefix="shepherd-confined-task-") as root:
            root_path = Path(root)
            try:
                request_path = self._stage_request(root_path)
            except ConfinedTaskExecutionError:
                raise
            except Exception as exc:
                raise ConfinedTaskExecutionError.prelaunch(exc) from exc

            # Lane C sub-root materialization: the jail's fail-closed pre-flight probe writes a
            # canary beneath every writable root, so each bound sub-root directory must already
            # exist in the run's clone upper. The clone mirrors the workspace, so bound roots that
            # exist in the workspace normally exist here — but ensure it fail-closed, dialect-side.
            self._materialize_binding_subroots(execution)
            try:
                if self.launch_metadata is not None:
                    self.launch_metadata["confined_worker_entrypoint"] = str(_confined_task_runner_entrypoint_path())
                    self.launch_metadata["launch_confined_attempted"] = True
                proc = execution.launch_confined(
                    [
                        sys.executable,
                        "-B",
                        str(_confined_task_runner_entrypoint_path()),
                        str(request_path),
                    ],
                    confinement,
                )
            except Exception as exc:
                raise ConfinedTaskExecutionError.monitor(exc) from exc
            if proc.returncode != 0:
                error = confined_task_error(proc.stderr)
                raise ConfinedTaskExecutionError.body(
                    refusal_type=error["type"],
                    detail=f"confined workspace task refused ({error['type']}): {error['message']}",
                )
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise ConfinedTaskExecutionError.body(
                    refusal_type=type(exc).__name__,
                    detail="confined workspace task returned invalid JSON",
                ) from exc
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema") != "shepherd.workspace_control.confined_task_result.v1"
            ):
                raise ConfinedTaskExecutionError.body(
                    refusal_type="TypeError",
                    detail="confined workspace task returned an unsupported result payload",
                )
            return {"status": "ok", "provider": self.provider_id, "result": payload.get("result")}

    def _materialize_binding_subroots(self, execution: Any) -> None:
        """Create each bound sub-root directory in the run clone before the jail probe (Lane C).

        This best-effort ``mkdir`` is not itself the fail-closed layer: the jail's per-root
        pre-flight probe writes a canary beneath every writable root and raises
        ``JailNotEstablished`` if any bound root is missing or denied, so a sub-root that failed to
        materialize here is caught before the body runs.
        """
        if self.binding_authorities is None:
            return
        working_path = getattr(execution, "working_path", None)
        if working_path is None:
            return
        working = Path(working_path)
        for entry in self.binding_authorities:
            if entry.root in {"", ".", os.curdir}:
                continue
            (working / PurePosixPath(entry.root)).mkdir(parents=True, exist_ok=True)

    def _stage_request(self, root_path: Path) -> Path:
        source_root = root_path / "src"
        source_root.mkdir()
        for raw_file in _artifact_files(self.artifact_payload):
            path = _required_artifact_file_str(raw_file, "path")
            content = _required_artifact_file_str(raw_file, "content")
            if _required_artifact_file_str(raw_file, "content_encoding") != "utf-8":
                raise RuntimeError("only utf-8 task artifact files are supported")
            destination = source_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

        entrypoint = self.artifact_payload.get("entrypoint")
        if not isinstance(entrypoint, Mapping):
            raise TypeError("task artifact entrypoint must be an object")
        if (self.repo_authority is None) == (self.binding_authorities is None):
            raise RuntimeError(
                "confined task provider requires exactly one of repo_authority (single-binding) "
                "or binding_authorities (per-binding) — refusing an ambiguous authority shape"
            )
        request: dict[str, object] = {
            "schema": "shepherd.workspace_control.confined_task_request.v1",
            "source_root": str(source_root),
            "entrypoint": dict(entrypoint),
            "kwargs": dict(self.kwargs),
        }
        if self.repo_authority is not None:
            # v0.1 single-binding shape — byte-identical to the pre-LC-3e request.
            request["repo"] = {
                "binding": "workspace",
                "authority": self.repo_authority,
            }
        else:
            assert self.binding_authorities is not None
            if not self.binding_authorities:
                raise RuntimeError("binding_authorities must name at least one binding")
            request["bindings"] = [
                {
                    "param": entry.param,
                    "binding": entry.binding,
                    "authority": entry.authority,
                    "root": entry.root,
                }
                for entry in self.binding_authorities
            ]
        request_path = root_path / "request.json"
        request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        return request_path


def confined_task_error(stderr: str) -> dict[str, str]:
    try:
        payload = json.loads(stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"type": "RuntimeError", "message": (stderr or "confined task failed").strip()[-300:]}
    if not isinstance(payload, Mapping):
        return {"type": "RuntimeError", "message": (stderr or "confined task failed").strip()[-300:]}
    return {
        "type": str(payload.get("type") or "RuntimeError"),
        "message": str(payload.get("message") or "confined task failed"),
    }


def _artifact_files(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, list | tuple):
        raise TypeError("task artifact files must be a list")
    files: list[Mapping[str, object]] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, Mapping):
            raise TypeError("task artifact file entries must be objects")
        files.append(raw_file)
    return tuple(files)


def _required_artifact_file_str(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"task artifact file {field_name} must be a non-empty string")
    if field_name == "path":
        _validate_artifact_relative_path(raw)
    return raw


def _validate_artifact_relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if path in {"", ".", ".."} or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise RuntimeError("task artifact file paths must be relative POSIX paths")


def _confined_task_runner_entrypoint_path() -> Path:
    return Path(__file__).with_name("_confined_task_runner.py").resolve()


def _monitor_refusal_type(exc: BaseException) -> str:
    cause_type = type(exc).__name__
    if cause_type == "JailNotEstablished" or "no jail-capable" not in str(exc):
        return cause_type
    return "JailNotEstablished"
