"""Internal hook runtime DTOs and helpers for session-level capture."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vcs_core._fs_capture import ensure_fs_capture_shim

if TYPE_CHECKING:
    from vcs_core.types import BoundSubstrate, EffectRecord, ScopeInfo
    from vcs_core.vcscore import VcsCore


HookKind = Literal["path_wrapper", "ld_preload", "http_proxy"]
HookPhase = Literal["start", "finish", "point"]
HookOutcome = Literal[
    "persisted",
    "ignored_no_effect",
    "ignored_stale_scope",
    "ignored_unsupported",
    "malformed",
    "failed",
]
HOOK_OUTCOMES: tuple[HookOutcome, ...] = (
    "persisted",
    "ignored_no_effect",
    "ignored_stale_scope",
    "ignored_unsupported",
    "malformed",
    "failed",
)


@dataclass(frozen=True)
class HookIgnored:
    """Explicit non-persisted hook translation outcome."""

    outcome: Literal["ignored_no_effect", "ignored_stale_scope", "ignored_unsupported"] = "ignored_no_effect"
    reason: str | None = None


@dataclass(frozen=True)
class HookDispatchResult:
    """Structured result of routing one hook event through the daemon."""

    outcome: HookOutcome
    reason: str | None = None

    @classmethod
    def persisted(cls) -> HookDispatchResult:
        return cls(outcome="persisted")

    @classmethod
    def ignored(
        cls,
        outcome: Literal["ignored_no_effect", "ignored_stale_scope", "ignored_unsupported"],
        reason: str | None = None,
    ) -> HookDispatchResult:
        return cls(outcome=outcome, reason=reason)


@dataclass(frozen=True)
class SystemHook:
    """Internal system-level interception declaration."""

    hook_id: str
    kind: HookKind
    config: Mapping[str, Any]
    translator: Callable[[HookEvent], HookAction | HookIgnored | None]
    # Empty capabilities means this hook is part of the baseline session runtime.
    # Non-empty capabilities are optional and activated only when explicitly requested.
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HookEvent:
    """Normalized session hook event emitted by wrapper/shim backends."""

    binding_name: str
    hook_id: str
    kind: HookKind
    phase: HookPhase
    scope: str
    scope_instance_id: str
    pid: int
    proc_seq: int
    timestamp_ns: int
    payload: Mapping[str, Any]
    ppid: int | None = None
    cwd: str | None = None
    exe: str | None = None
    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    signal: int | None = None
    command_operation_id: str | None = None
    capture_epoch: str | None = None


@dataclass(frozen=True)
class HookEffects:
    """Translated hook event emitted directly as EffectRecords."""

    effects: tuple[EffectRecord, ...]
    value: object | None = None


@dataclass(frozen=True)
class HookCaptureEvent:
    """Translated raw capture event that belongs to a command envelope."""

    event: Any
    seq: int
    capture_mechanism: str = "preload"


@dataclass(frozen=True)
class HookCaptureDiagnostic:
    """Translated raw capture event that cannot be admitted authoritatively."""

    event: Any
    seq: int
    reason: str
    capture_mechanism: str = "preload"


@dataclass(frozen=True)
class HookCaptureProcessStart:
    """Lifecycle marker for one instrumented capture process."""


@dataclass(frozen=True)
class HookCaptureProcessFinish:
    """Lifecycle marker carrying one process's capture high-water sequence."""

    last_proc_seq: int


@dataclass(frozen=True)
class HookCaptureShellCommandFinish:
    """Barrier marker for one shell command in a persistent shell process."""

    seq: int


HookAction = (
    HookEffects
    | HookCaptureEvent
    | HookCaptureDiagnostic
    | HookCaptureProcessStart
    | HookCaptureProcessFinish
    | HookCaptureShellCommandFinish
)


@dataclass(frozen=True)
class HookActivation:
    """Requested optional hook capabilities for one shell launch.

    Baseline hooks remain active even when this set is empty.
    """

    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HookRuntimeEnv:
    """Shell environment fragments synthesized by hook installers."""

    env: Mapping[str, str]
    prepend_path: tuple[str, ...] = ()
    prepend_env: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    sockets: Mapping[str, str] = field(default_factory=dict)


EMPTY_HOOK_ENV = HookRuntimeEnv(env={}, prepend_path=(), prepend_env={}, sockets={})


@dataclass(frozen=True)
class HookBinding:
    """One bound substrate hook routed by binding + hook id."""

    binding_name: str
    substrate_type: str
    substrate: object
    hook: SystemHook


def _hook_operation_id(event: HookEvent) -> str:
    return f"hook-{event.binding_name}-{event.hook_id}-{event.pid}-{event.proc_seq}".replace("/", "-")


def _hook_operation_kind(event: HookEvent) -> str:
    return f"hook.{event.binding_name}.{event.hook_id}"


def parse_hook_event_line(raw: str | bytes) -> HookEvent:
    """Parse one JSON-lines hook payload into a HookEvent."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid hook JSON: {exc}") from exc
    return parse_hook_event(payload)


def parse_hook_event(payload: Mapping[str, Any]) -> HookEvent:
    """Validate and normalize a hook event payload."""
    kind = _require_literal(payload, "kind", {"path_wrapper", "ld_preload", "http_proxy"})
    phase = _require_literal(payload, "phase", {"start", "finish", "point"})
    argv_value = payload.get("argv")
    if argv_value in (None, ""):
        argv: tuple[str, ...] = ()
    elif isinstance(argv_value, list) and all(isinstance(item, str) for item in argv_value):
        argv = tuple(argv_value)
    else:
        raise ValueError("hook event field 'argv' must be a list[str] when present.")
    payload_value = payload.get("payload", {})
    if not isinstance(payload_value, Mapping):
        raise ValueError("hook event field 'payload' must be an object.")  # noqa: TRY004

    return HookEvent(
        binding_name=_require_str(payload, "binding_name"),
        hook_id=_require_str(payload, "hook_id"),
        kind=kind,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        scope=_require_str(payload, "scope"),
        scope_instance_id=_require_str(payload, "scope_instance_id"),
        pid=_require_int(payload, "pid"),
        proc_seq=_require_int(payload, "proc_seq"),
        timestamp_ns=_require_int(payload, "timestamp_ns"),
        payload=payload_value,
        ppid=_optional_int(payload, "ppid"),
        cwd=_optional_str(payload, "cwd"),
        exe=_optional_str(payload, "exe"),
        argv=argv,
        exit_code=_optional_int(payload, "exit_code"),
        signal=_optional_int(payload, "signal"),
        command_operation_id=_optional_str(payload, "command_operation_id"),
        capture_epoch=_optional_str(payload, "capture_epoch"),
    )


class HookManager:
    """Daemon-owned system hook installation and dispatch."""

    def __init__(self, mg: VcsCore, *, workspace: Path, repo_path: Path, socket_path: str) -> None:
        self._mg = mg
        self._workspace = workspace.resolve()
        self._repo_path = repo_path.resolve()
        self._socket_path = socket_path
        self._bindings: dict[tuple[str, str], HookBinding] = {}
        self._known_capabilities: set[str] = set()
        self._installers: list[Any] = []
        self._static_env = HookRuntimeEnv(
            env={"VCS_CORE_HOOK_SOCKET": socket_path}, prepend_path=(), sockets={"hook": socket_path}
        )

    def install_bindings(self, bindings: Sequence[BoundSubstrate]) -> None:
        grouped: dict[HookKind, list[HookBinding]] = {}
        for binding in bindings:
            provider = getattr(binding.instance, "system_hooks", None)
            if provider is None:
                continue
            hooks = provider()
            for hook in hooks:
                hook_binding = HookBinding(
                    binding_name=binding.binding_name,
                    substrate_type=binding.substrate_type,
                    substrate=binding.instance,
                    hook=hook,
                )
                binding_key = (binding.binding_name, hook.hook_id)
                if binding_key in self._bindings:
                    raise ValueError(f"Duplicate hook binding: {binding_key!r}")
                self._bindings[binding_key] = hook_binding
                self._known_capabilities.update(hook.capabilities)
                grouped.setdefault(hook.kind, []).append(hook_binding)

        envs: list[HookRuntimeEnv] = [self._static_env]
        if grouped.get("path_wrapper"):
            path_wrapper_installer = PathWrapperInstaller(socket_path=self._socket_path)
            envs.append(
                path_wrapper_installer.install(
                    grouped["path_wrapper"],
                    workspace=self._workspace,
                    repo_path=self._repo_path,
                )
            )
            self._installers.append(path_wrapper_installer)
        if grouped.get("ld_preload"):
            preload_installer = PreloadInstaller(socket_path=self._socket_path)
            envs.append(
                preload_installer.install(
                    grouped["ld_preload"],
                    workspace=self._workspace,
                    repo_path=self._repo_path,
                )
            )
            self._installers.append(preload_installer)
        self._static_env = _merge_envs(envs)

    def activation(self, requested_capabilities: Sequence[str] | None = None) -> HookActivation:
        """Validate and normalize optional capability requests for one shell launch."""
        requested = frozenset(str(item) for item in (requested_capabilities or ()))
        unknown = sorted(requested - self._known_capabilities)
        if unknown:
            supported = ", ".join(sorted(self._known_capabilities)) or "(none)"
            requested_text = ", ".join(unknown)
            raise ValueError(f"Unknown hook capabilities: {requested_text}. Supported: {supported}.")
        return HookActivation(capabilities=requested)

    def static_env(self, *, activation: HookActivation | None = None) -> HookRuntimeEnv:
        """Return scope-independent shell env fragments for the session.

        Baseline hooks contribute here regardless of optional capabilities.
        """
        del activation
        return self._static_env

    def scope_env(self, scope: ScopeInfo, *, activation: HookActivation | None = None) -> HookRuntimeEnv:
        """Return scope-derived shell env fragments for one shell launch.

        Optional hooks may add env only when their capabilities are requested.
        """
        activation = activation or HookActivation()
        envs = [
            HookRuntimeEnv(
                env={
                    "VCS_CORE_SCOPE": scope.name,
                    "VCS_CORE_SCOPE_INSTANCE_ID": scope.instance_id,
                    "VCS_CORE_WORKSPACE": str(self._mg.working_directory_for_scope(scope)),
                }
            )
        ]
        for installer in self._installers:
            envs.append(installer.scope_env(scope, mg=self._mg, activation=activation))
        return _merge_envs(envs)

    def ingest_line(self, raw: str | bytes, *, global_seq: int = 0) -> HookDispatchResult:
        event = parse_hook_event_line(raw)
        return self.dispatch(event, global_seq=global_seq)

    def dispatch(
        self,
        event: HookEvent,
        *,
        global_seq: int = 0,
        capture_authority: Any | None = None,
    ) -> HookDispatchResult:
        binding = self._bindings.get((event.binding_name, event.hook_id))
        if binding is None:
            return HookDispatchResult.ignored(
                "ignored_unsupported",
                reason=f"unknown hook binding: {(event.binding_name, event.hook_id)!r}",
            )

        try:
            scope = self._resolve_scope(event.scope)
        except ValueError as exc:
            return HookDispatchResult.ignored("ignored_stale_scope", reason=str(exc))
        if scope.instance_id != event.scope_instance_id:
            return HookDispatchResult.ignored("ignored_stale_scope", reason="scope instance mismatch")

        action = binding.hook.translator(event)
        if action is None:
            return HookDispatchResult.ignored("ignored_no_effect")
        if isinstance(action, HookIgnored):
            return HookDispatchResult.ignored(action.outcome, reason=action.reason)

        operation_metadata: dict[str, object] = {
            "hook_kind": event.kind,
            "hook_phase": event.phase,
            "hook_pid": event.pid,
            "hook_proc_seq": event.proc_seq,
        }
        if event.cwd is not None:
            operation_metadata["hook_cwd"] = event.cwd
        if event.command_operation_id is not None:
            operation_metadata["command_operation_id"] = event.command_operation_id
        if event.capture_epoch is not None:
            operation_metadata["capture_epoch"] = event.capture_epoch

        if isinstance(action, HookCaptureDiagnostic):
            if hasattr(self._mg, "_record_capture_diagnostic"):
                self._mg._record_capture_diagnostic(
                    binding.binding_name,
                    action.event,
                    command_operation_id=event.command_operation_id or "uncorrelated",
                    capture_epoch=event.capture_epoch,
                    global_seq=global_seq,
                    event_seq=action.seq,
                    capture_mechanism=action.capture_mechanism,
                    reason=action.reason,
                )
                return HookDispatchResult.persisted()
            return HookDispatchResult.ignored("ignored_unsupported", reason=action.reason)

        if isinstance(action, HookCaptureEvent):
            if event.command_operation_id is None:
                return HookDispatchResult.ignored(
                    "ignored_unsupported",
                    reason="shim_context_missing",
                )
            if hasattr(self._mg, "_record_capture_event"):
                accepted_by_capture = False
                authority = capture_authority
                if authority is not None:
                    accepted = authority.accept_event(
                        event.command_operation_id,
                        pid=event.pid,
                        proc_seq=event.proc_seq,
                        global_seq=global_seq,
                    )
                    if not accepted.accepted:
                        if hasattr(self._mg, "_record_capture_diagnostic"):
                            self._mg._record_capture_diagnostic(
                                binding.binding_name,
                                action.event,
                                command_operation_id=event.command_operation_id,
                                capture_epoch=event.capture_epoch,
                                global_seq=global_seq,
                                event_seq=action.seq,
                                capture_mechanism=action.capture_mechanism,
                                reason=_capture_diagnostic_reason(accepted.reason),
                            )
                            return HookDispatchResult.persisted()
                        return HookDispatchResult.ignored("ignored_unsupported", reason=accepted.reason)
                    accepted_by_capture = True
                else:
                    accepted_by_capture = False
                try:
                    self._mg._record_capture_event(
                        binding.binding_name,
                        action.event,
                        command_operation_id=event.command_operation_id,
                        capture_epoch=event.capture_epoch,
                        global_seq=global_seq,
                        event_seq=action.seq,
                        capture_mechanism=action.capture_mechanism,
                    )
                except Exception:
                    if accepted_by_capture and authority is not None and hasattr(authority, "mark_failed"):
                        authority.mark_failed(
                            event.command_operation_id,
                            global_seq=global_seq,
                            reason="capture_persist_failed",
                        )
                    raise
                if accepted_by_capture and authority is not None:
                    authority.mark_processed(event.command_operation_id, global_seq=global_seq)
                return HookDispatchResult.persisted()
            return HookDispatchResult.ignored("ignored_unsupported", reason="capture journal is unavailable")

        if isinstance(action, HookCaptureProcessStart | HookCaptureProcessFinish):
            if event.command_operation_id is None:
                return HookDispatchResult.ignored(
                    "ignored_unsupported",
                    reason="shim_context_missing",
                )
            authority = capture_authority
            if authority is None:
                return HookDispatchResult.ignored("ignored_unsupported", reason="capture authority is unavailable")
            if isinstance(action, HookCaptureProcessStart):
                accepted = authority.register_process(event.command_operation_id, pid=event.pid)
            else:
                accepted = authority.finish_process(
                    event.command_operation_id,
                    pid=event.pid,
                    last_proc_seq=action.last_proc_seq,
                )
            if not accepted.accepted:
                return HookDispatchResult.ignored("ignored_unsupported", reason=accepted.reason)
            return HookDispatchResult.persisted()

        if isinstance(action, HookCaptureShellCommandFinish):
            if event.command_operation_id is None:
                return HookDispatchResult.ignored(
                    "ignored_unsupported",
                    reason="shim_context_missing",
                )
            authority = capture_authority
            if authority is None:
                return HookDispatchResult.ignored("ignored_unsupported", reason="capture authority is unavailable")
            accepted = authority.finish_shell_command(event.command_operation_id, pid=event.pid, proc_seq=action.seq)
            if not accepted.accepted:
                return HookDispatchResult.ignored("ignored_unsupported", reason=accepted.reason)
            return HookDispatchResult.persisted()

        persisted = False
        for effect in action.effects:
            self._mg._record_in_child_operation(
                binding.binding_name,
                effect,
                scope=scope,
                operation_id=_hook_operation_id(event),
                operation_kind=_hook_operation_kind(event),
                operation_metadata=operation_metadata,
            )
            persisted = True
        if persisted:
            return HookDispatchResult.persisted()
        return HookDispatchResult.ignored("ignored_no_effect", reason="translator produced no effects")

    def shutdown(self) -> None:
        for installer in reversed(self._installers):
            installer.uninstall()
        self._installers.clear()

    def _resolve_scope(self, name: str) -> ScopeInfo:
        if name == "ground":
            return self._mg.ground
        scope = self._mg.lookup_scope(name)
        if scope is None:
            raise ValueError(f"No tracked scope '{name}'.")
        return scope


class PathWrapperInstaller:
    """Install PATH wrapper binaries for shell-level command capture."""

    kind: HookKind = "path_wrapper"

    def __init__(self, *, socket_path: str) -> None:
        self._socket_path = socket_path
        self._managed_root: Path | None = None
        self._session_root: Path | None = None
        self._wrapper_dir: Path | None = None
        self._capabilities: frozenset[str] = frozenset()

    def install(
        self,
        bindings: Sequence[HookBinding],
        *,
        workspace: Path,
        repo_path: Path,
    ) -> HookRuntimeEnv:
        del workspace
        binaries: dict[str, HookBinding] = {}
        for binding in bindings:
            binary = str(binding.hook.config.get("binary", "")).strip()
            if not binary:
                raise ValueError(f"path_wrapper hook {binding.hook.hook_id!r} is missing config.binary")
            if binary in binaries:
                raise ValueError(
                    f"Multiple path_wrapper hooks claim binary {binary!r}; spike runtime cannot disambiguate"
                )
            binaries[binary] = binding
        self._capabilities = frozenset(capability for binding in bindings for capability in binding.hook.capabilities)

        managed_root = (repo_path / "runtime" / "hooks").resolve()
        managed_root.mkdir(parents=True, exist_ok=True)
        session_root = Path(tempfile.mkdtemp(prefix="path-wrapper-", dir=managed_root))
        wrapper_dir = session_root / "bin"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        self._managed_root = managed_root
        self._session_root = session_root
        self._wrapper_dir = wrapper_dir

        search_path = _sanitize_exec_path(os.environ.get("PATH"), managed_roots=(managed_root,))
        for binary, binding in binaries.items():
            real_binary = shutil.which(binary, path=search_path)
            if real_binary is None:
                raise RuntimeError(f"Unable to resolve binary on PATH for hook wrapper: {binary}")
            resolved_binary = Path(real_binary).resolve()
            if _is_managed_hook_path(resolved_binary, managed_roots=(managed_root,)):
                raise RuntimeError(
                    f"Resolved hook wrapper target for {binary!r} points back into managed hook state: {resolved_binary}"
                )
            wrapper_path = wrapper_dir / binary
            wrapper_path.write_text(
                _path_wrapper_script(
                    real_binary=str(resolved_binary),
                    binding_name=binding.binding_name,
                    hook_id=binding.hook.hook_id,
                    binary_name=binary,
                )
            )
            wrapper_path.chmod(0o755)

        return HookRuntimeEnv(
            env={"VCS_CORE_HOOK_SOCKET": self._socket_path},
            prepend_path=(str(wrapper_dir),),
            sockets={"hook": self._socket_path},
        )

    def uninstall(self) -> None:
        if self._session_root is not None:
            shutil.rmtree(self._session_root, ignore_errors=True)
        self._wrapper_dir = None
        self._session_root = None

    def scope_env(self, scope: ScopeInfo, *, mg: VcsCore, activation: HookActivation | None = None) -> HookRuntimeEnv:
        del scope, mg, activation
        return EMPTY_HOOK_ENV


class PreloadInstaller:
    """Install env fragments for the filesystem preload shim."""

    kind: HookKind = "ld_preload"

    def __init__(self, *, socket_path: str) -> None:
        self._socket_path = socket_path
        self._repo_path: Path | None = None
        self._capabilities: frozenset[str] = frozenset()

    def install(
        self,
        bindings: Sequence[HookBinding],
        *,
        workspace: Path,
        repo_path: Path,
    ) -> HookRuntimeEnv:
        del workspace
        self._repo_path = repo_path.resolve()
        self._capabilities = frozenset(capability for binding in bindings for capability in binding.hook.capabilities)
        return EMPTY_HOOK_ENV

    def uninstall(self) -> None:
        return None

    def scope_env(self, scope: ScopeInfo, *, mg: VcsCore, activation: HookActivation | None = None) -> HookRuntimeEnv:
        del scope, mg
        activation = activation or HookActivation()
        if self._capabilities and not (activation.capabilities & self._capabilities):
            return EMPTY_HOOK_ENV
        if self._repo_path is None:
            raise RuntimeError("preload hook runtime is not initialized")
        shim_path = ensure_fs_capture_shim(self._repo_path)
        return HookRuntimeEnv(
            env={},
            prepend_env={"LD_PRELOAD": (shim_path,)},
        )


def _merge_envs(envs: Sequence[HookRuntimeEnv]) -> HookRuntimeEnv:
    env: dict[str, str] = {}
    prepend_path: list[str] = []
    prepend_env: dict[str, list[str]] = {}
    sockets: dict[str, str] = {}
    for item in envs:
        prepend_path.extend(item.prepend_path)
        for key, values in item.prepend_env.items():
            prepend_env.setdefault(key, []).extend(values)
        for key, value in item.sockets.items():
            if key in sockets and sockets[key] != value:
                raise ValueError(f"Conflicting hook socket mapping for {key!r}")
            sockets[key] = value
        for key, value in item.env.items():
            if key in env and env[key] != value:
                raise ValueError(f"Conflicting hook env assignment for {key!r}")
            env[key] = value
    return HookRuntimeEnv(
        env=env,
        prepend_path=tuple(prepend_path),
        prepend_env={key: tuple(values) for key, values in prepend_env.items()},
        sockets=sockets,
    )


def _capture_diagnostic_reason(reason: str | None) -> str:
    if reason in {"capture_complete", "capture_incomplete"}:
        return "late_event_after_finalization"
    if reason == "unknown_command_operation":
        return "uncorrelated_capture_event"
    return reason or "capture_rejected"


def _sanitize_exec_path(path_value: str | None, *, managed_roots: Sequence[Path]) -> str:
    entries = (path_value or os.defpath).split(os.pathsep)
    filtered: list[str] = []
    for raw_entry in entries:
        candidate = Path(raw_entry or ".").resolve()
        if _is_managed_hook_path(candidate, managed_roots=managed_roots):
            continue
        filtered.append(raw_entry)
    return os.pathsep.join(filtered)


def _is_managed_hook_path(path: Path, *, managed_roots: Sequence[Path]) -> bool:
    resolved = path.resolve()
    return any(_is_relative_to(resolved, root.resolve()) for root in managed_roots)


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _path_wrapper_script(*, real_binary: str, binding_name: str, hook_id: str, binary_name: str) -> str:
    return f"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time


def _send(payload: dict[str, object]) -> None:
    socket_path = os.environ.get("VCS_CORE_HOOK_SOCKET")
    if not socket_path or os.environ.get("VCS_CORE_HOOK_SUPPRESS") == "1":
        return
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(socket_path)
        conn.sendall((json.dumps(payload) + "\\n").encode())
        conn.close()
    except OSError:
        return


def main() -> int:
    env = dict(os.environ)
    env["VCS_CORE_HOOK_SUPPRESS"] = "1"
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_IGN)
    argv = [{real_binary!r}, *sys.argv[1:]]
    def _reset_child_signals() -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    proc = subprocess.Popen(argv, env=env, preexec_fn=_reset_child_signals)
    returncode = proc.wait()
    scope = os.environ.get("VCS_CORE_SCOPE")
    scope_instance_id = os.environ.get("VCS_CORE_SCOPE_INSTANCE_ID")
    command_operation_id = os.environ.get("VCS_CORE_COMMAND_OPERATION_ID")
    if scope and scope_instance_id:
        payload = {{
            "binding_name": {binding_name!r},
            "hook_id": {hook_id!r},
            "kind": "path_wrapper",
            "phase": "finish",
            "scope": scope,
            "scope_instance_id": scope_instance_id,
            "pid": os.getpid(),
            "proc_seq": 1,
            "timestamp_ns": time.time_ns(),
            "cwd": os.getcwd(),
            "exe": {real_binary!r},
            "argv": [{binary_name!r}, *sys.argv[1:]],
            "exit_code": returncode,
            "signal": -returncode if returncode < 0 else None,
            "command_operation_id": command_operation_id,
            "payload": {{}},
        }}
        _send(payload)
    if returncode >= 0:
        return returncode
    signum = -returncode
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    os._exit(128 + signum)


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"hook event is missing non-empty string field {key!r}.")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"hook event is missing integer field {key!r}.")  # noqa: TRY004
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"hook event field {key!r} must be a string when present.")  # noqa: TRY004
    return value


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"hook event field {key!r} must be an integer when present.")  # noqa: TRY004
    return value


def _require_literal(payload: Mapping[str, Any], key: str, allowed: set[str]) -> str:
    value = _require_str(payload, key)
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Unsupported hook {key} {value!r}. Supported: {choices}.")
    return value
