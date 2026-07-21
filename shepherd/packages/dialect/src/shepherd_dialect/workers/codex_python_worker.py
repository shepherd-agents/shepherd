"""Short-lived trusted Codex broker used by the restricted provider runtime."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from shepherd_dialect.provider_stream import emit_provider_stream_record
from shepherd_dialect.providers.codex_protocol import (
    make_capturing_client,
    run_codex_turn,
    write_codex_permission_config,
)


def run(payload: Mapping[str, object]) -> int:
    """Execute one trusted broker request and emit its verified stream."""
    from openai_codex.client import CodexConfig, _installed_codex_path  # type: ignore[import-not-found]

    provider_id = _required_string(payload, "provider_id")
    invocation_id = _required_string(payload, "invocation_id")
    workspace = Path(_required_string(payload, "working_directory")).resolve()
    runtime = Path(_required_string(payload, "runtime_directory")).resolve()
    credential_home = Path(_required_string(payload, "credential_home")).resolve()
    credential_auth = Path(_required_string(payload, "credential_auth")).resolve()
    profile_root = Path(_required_string(payload, "profile_root")).resolve()
    model = _required_string(payload, "model")
    auth_mode = _required_string(payload, "auth_mode")
    prompt = _required_string(payload, "prompt")
    output_schema = payload.get("output_schema")
    if output_schema is not None and not isinstance(output_schema, Mapping):
        raise TypeError("output_schema must be an object or null")
    deadline_seconds = payload.get("deadline_seconds")
    if not isinstance(deadline_seconds, int | float) or isinstance(deadline_seconds, bool) or deadline_seconds <= 0:
        raise TypeError("deadline_seconds must be positive")
    writable_roots = _path_sequence(payload.get("writable_roots", [str(workspace)]), "writable_roots")
    network_mode = _required_string(payload, "network_mode")
    allowed_hosts = _string_sequence(payload.get("allowed_hosts", []), "allowed_hosts")
    app_server_argv = payload.get("app_server_argv")
    if app_server_argv is not None:
        app_server_argv = _string_sequence(app_server_argv, "app_server_argv")
    app_server_env = payload.get("app_server_env", {})
    if not isinstance(app_server_env, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in app_server_env.items()
    ):
        raise TypeError("app_server_env must be a string mapping")

    codex_home = runtime / "codex-home"
    home = runtime / "home"
    codex_home.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    auth_link = codex_home / "auth.json"
    auth_link.symlink_to(credential_auth)
    permission_digest = write_codex_permission_config(
        codex_home=codex_home,
        workspace=workspace,
        credential_paths=(
            profile_root,
            credential_home,
            credential_auth,
            codex_home,
            home,
            runtime / "request.json",
        ),
        writable_roots=writable_roots,
        network_mode=network_mode,
        allowed_hosts=allowed_hosts,
        readable_runtime_paths=(_installed_codex_path().parent.parent,),
    )
    denied_paths = (
        profile_root,
        credential_home,
        credential_auth,
        codex_home,
        home,
        runtime / "request.json",
    )
    sdk_env = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        **{str(key): str(value) for key, value in app_server_env.items()},
    }
    config = CodexConfig(
        launch_args_override=tuple(app_server_argv) if app_server_argv is not None else None,
        cwd=str(workspace),
        env=sdk_env,
    )
    emit_lock = threading.Lock()

    def emit_activity(activity: Any) -> None:
        with emit_lock:
            emit_provider_stream_record("activity", activity.as_wire_record())

    client, ledger, projector = make_capturing_client(
        config=config,
        provider_id=provider_id,
        invocation_id=invocation_id,
        workspace_root=workspace,
        on_activity=emit_activity,
    )
    terminal = "broker_error"
    result_payload: dict[str, object]
    returncode = 1
    try:
        turn = run_codex_turn(
            client=client,
            projector=projector,
            workspace=workspace,
            model=model,
            prompt=prompt,
            output_schema=dict(output_schema) if isinstance(output_schema, Mapping) else None,
            deadline_seconds=float(deadline_seconds),
            auth_mode=auth_mode,
            writable_roots=writable_roots,
            denied_paths=denied_paths,
            allow_fake_runtime=payload.get("allow_fake_runtime") is True,
        )
        terminal = turn.terminal
        if terminal != "completed":
            raise RuntimeError(f"Codex turn ended with terminal state {terminal!r}")
        result_payload = {
            "status": "ok",
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
            "terminal": turn.terminal,
            "output_text": turn.output_text,
            "structured_output": dict(turn.structured_output),
            "usage": dict(turn.usage),
            "cost": dict(turn.cost),
            "rate_limits": dict(turn.rate_limits),
            "sdk_version": turn.sdk_version,
            "runtime_version": turn.runtime_version,
            "permission_profile_digest": permission_digest,
            "sandbox_evidence": dict(turn.sandbox_evidence),
            "approval_policy": "never",
            "auth_mode": auth_mode,
        }
        returncode = 0
    except Exception as exc:  # noqa: BLE001 - broker failures must be framed, never dumped with secrets
        exception_terminal = getattr(exc, "terminal", None)
        if isinstance(exception_terminal, str) and exception_terminal:
            terminal = exception_terminal
        result_payload = {
            "status": "error",
            "terminal": terminal,
            "error_type": type(exc).__name__,
            "error_digest": _digest_text(str(exc)),
            "error_length": len(str(exc)),
            "permission_profile_digest": permission_digest,
            "approval_policy": "never",
            "auth_mode": auth_mode,
        }
    finally:
        client.close()
    terminal_seen = terminal not in {"broker_error", "deadline_without_terminal"}
    manifest = ledger.manifest(
        terminal_kind=terminal,
        terminal_seen=terminal_seen,
        complete=terminal_seen,
    )
    with emit_lock:
        emit_provider_stream_record("manifest", manifest.as_wire_record())
        emit_provider_stream_record("result", result_payload)
    return returncode


def main() -> None:
    """Read one private request file and run the broker."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m shepherd_dialect.workers.codex_python_worker REQUEST.json")
    path = Path(sys.argv[1])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError("request payload must be an object")
        raise SystemExit(run(payload))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - startup must fail without leaking payload data
        sys.stderr.write(f"codex broker startup failed: {type(exc).__name__}\n")
        raise SystemExit(1) from None


def _required_string(value: Mapping[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise TypeError(f"{key} must be a non-empty string")
    return raw


def _string_sequence(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) and item for item in value):
        raise TypeError(f"{key} must be a sequence of non-empty strings")
    return tuple(value)


def _path_sequence(value: object, key: str) -> tuple[Path, ...]:
    return tuple(Path(item).resolve() for item in _string_sequence(value, key))


def _digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()}"


if __name__ == "__main__":
    main()
