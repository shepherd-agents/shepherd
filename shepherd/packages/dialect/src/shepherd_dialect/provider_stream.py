"""Live framed provider-worker stream and restricted process supervision."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from shepherd_dialect.provider_activity import (
    ProviderActivity,
    ProviderActivityError,
    ProviderActivityManifest,
    validate_activity_stream,
)

PROVIDER_STREAM_SCHEMA_VERSION = "shepherd.provider_stream.v1"

_ACTIVITY = "activity"
_MANIFEST = "manifest"
_RESULT = "result"
_REGISTERED_ADAPTERS: dict[str, tuple[str, ...]] = {
    "codex-python": (sys.executable, "-u", "-B", "-m", "shepherd_dialect.workers.codex_python_worker"),
}


class ProviderStreamError(RuntimeError):
    """Raised when a provider broker exits without a complete verified stream."""

    def __init__(
        self,
        message: str,
        *,
        activities: tuple[ProviderActivity, ...] = (),
        manifest: ProviderActivityManifest | None = None,
        returncode: int | None = None,
        stderr_digest: str | None = None,
        stderr_length: int = 0,
    ) -> None:
        super().__init__(message)
        self.activities = activities
        self.manifest = manifest
        self.returncode = returncode
        self.stderr_digest = stderr_digest
        self.stderr_length = stderr_length


@dataclass(frozen=True)
class ProviderProcessRequest:
    """A provider-neutral request; callers select an adapter, never broker argv."""

    adapter_id: Literal["codex-python"]
    provider_id: str
    invocation_id: str
    working_directory: Path
    payload: Mapping[str, object]
    deadline_seconds: float
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.adapter_id not in _REGISTERED_ADAPTERS:
            raise ProviderStreamError(f"unregistered provider adapter: {self.adapter_id!r}")
        if not self.provider_id or not self.invocation_id:
            raise ProviderStreamError("provider process identity must be non-empty")
        if self.deadline_seconds <= 0:
            raise ProviderStreamError("provider process deadline must be positive")
        if not self.working_directory.is_absolute() or not self.working_directory.is_dir():
            raise ProviderStreamError("provider working_directory must be an existing absolute directory")


@dataclass(frozen=True)
class ProviderProcessResult:
    """Verified terminal broker result."""

    result: Mapping[str, object]
    activities: tuple[ProviderActivity, ...]
    manifest: ProviderActivityManifest
    returncode: int
    stderr_digest: str
    stderr_length: int


def emit_provider_stream_record(record_type: str, payload: Mapping[str, object]) -> None:
    """Write one atomic line from a broker to its supervising parent."""
    if record_type not in {_ACTIVITY, _MANIFEST, _RESULT}:
        raise ProviderStreamError(f"unsupported provider stream record type: {record_type!r}")
    sys.stdout.write(
        json.dumps(
            {
                "schema_version": PROVIDER_STREAM_SCHEMA_VERSION,
                "record_type": record_type,
                record_type: dict(payload),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def supervise_provider_process(request: ProviderProcessRequest) -> ProviderProcessResult:
    """Start one registered broker, consume it live, and verify terminal integrity."""
    runtime_dir = Path(tempfile.mkdtemp(prefix="shepherd-provider-"))
    runtime_dir.chmod(0o700)
    payload_path = runtime_dir / "request.json"
    payload = {
        "provider_id": request.provider_id,
        "invocation_id": request.invocation_id,
        "working_directory": str(request.working_directory),
        "runtime_directory": str(runtime_dir),
        **dict(request.payload),
    }
    payload_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    payload_path.chmod(0o600)
    environment = _broker_environment(request.environment)
    command = [*_REGISTERED_ADAPTERS[request.adapter_id], str(payload_path)]
    process: subprocess.Popen[str] | None = None
    activities: list[ProviderActivity] = []
    manifest: ProviderActivityManifest | None = None
    result: Mapping[str, object] | None = None
    stderr_chunks: list[str] = []
    line_queue: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=1024)
    terminal_record_seen = False
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=request.working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=_pump_lines,
            args=(process.stdout, "stdout", line_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_pump_lines,
            args=(process.stderr, "stderr", line_queue),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        closed: set[str] = set()
        while len(closed) < 2:
            remaining = request.deadline_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise ProviderStreamError("provider broker exceeded its hard deadline")
            try:
                channel, line = line_queue.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if process.poll() is not None and not stdout_thread.is_alive() and not stderr_thread.is_alive():
                    break
                continue
            if line is None:
                closed.add(channel)
                continue
            if channel == "stderr":
                stderr_chunks.append(line)
                continue
            if terminal_record_seen:
                raise ProviderStreamError("provider broker emitted a record after its terminal result")
            record = _parse_stream_line(line)
            record_type = record["record_type"]
            body = record[record_type]
            if not isinstance(body, Mapping):
                raise ProviderStreamError(f"provider stream {record_type} body must be an object")
            if record_type == _ACTIVITY:
                if manifest is not None:
                    raise ProviderStreamError("provider broker emitted activity after manifest")
                activity = ProviderActivity.from_wire_record(body)
                expected_sequence = len(activities)
                if activity.sequence != expected_sequence:
                    raise ProviderStreamError(f"provider activity sequence gap at {expected_sequence}")
                if activity.provider_id != request.provider_id or activity.invocation_id != request.invocation_id:
                    raise ProviderStreamError("provider activity identity mismatch")
                expected_previous = activities[-1].record_digest if activities else None
                if activity.previous_record_digest != expected_previous:
                    raise ProviderStreamError(f"provider activity chain break at {expected_sequence}")
                activities.append(activity)
            elif record_type == _MANIFEST:
                if manifest is not None:
                    raise ProviderStreamError("provider broker emitted multiple manifests")
                manifest = ProviderActivityManifest.from_wire_record(body)
                validate_activity_stream(activities, manifest, require_complete=False)
            elif record_type == _RESULT:
                if manifest is None:
                    raise ProviderStreamError("provider broker emitted result before manifest")
                if result is not None:
                    raise ProviderStreamError("provider broker emitted multiple results")
                result = dict(body)
                terminal_record_seen = True

        remaining = max(0.01, request.deadline_seconds - (time.monotonic() - started))
        returncode = process.wait(timeout=remaining)
        stderr = "".join(stderr_chunks)
        stderr_raw = stderr.encode("utf-8", errors="replace")
        retained_activities = tuple(activities)
        stderr_digest = f"sha256:{hashlib.sha256(stderr_raw).hexdigest()}"
        stderr_length = len(stderr_raw)
        if manifest is None:
            raise ProviderStreamError(
                "provider broker closed without an activity manifest",
                activities=retained_activities,
                manifest=None,
                returncode=returncode,
                stderr_digest=stderr_digest,
                stderr_length=stderr_length,
            )
        try:
            validate_activity_stream(
                activities,
                manifest,
                require_complete=result is not None and result.get("status") == "ok",
            )
        except ProviderActivityError as exc:
            raise ProviderStreamError(
                str(exc),
                activities=retained_activities,
                manifest=manifest,
                returncode=returncode,
                stderr_digest=stderr_digest,
                stderr_length=stderr_length,
            ) from exc
        if result is None:
            raise ProviderStreamError(
                "provider broker closed without one terminal result",
                activities=retained_activities,
                manifest=manifest,
                returncode=returncode,
                stderr_digest=stderr_digest,
                stderr_length=stderr_length,
            )
        result_ok = result.get("status") == "ok"
        if returncode != 0 or not result_ok:
            error_type = result.get("error_type")
            detail = f" ({error_type})" if isinstance(error_type, str) and error_type else ""
            raise ProviderStreamError(
                f"provider broker failed{detail}",
                activities=retained_activities,
                manifest=manifest,
                returncode=returncode,
                stderr_digest=stderr_digest,
                stderr_length=stderr_length,
            )
        return ProviderProcessResult(
            result=result,
            activities=retained_activities,
            manifest=manifest,
            returncode=returncode,
            stderr_digest=stderr_digest,
            stderr_length=stderr_length,
        )
    except ProviderStreamError as exc:
        _attach_stream_diagnostics(
            exc,
            activities=activities,
            manifest=manifest,
            process=process,
            stderr_chunks=stderr_chunks,
        )
        raise
    except ProviderActivityError as exc:
        failure = ProviderStreamError(str(exc))
        _attach_stream_diagnostics(
            failure,
            activities=activities,
            manifest=manifest,
            process=process,
            stderr_chunks=stderr_chunks,
        )
        raise failure from exc
    except subprocess.TimeoutExpired as exc:
        failure = ProviderStreamError("provider broker did not exit before its hard deadline")
        _attach_stream_diagnostics(
            failure,
            activities=activities,
            manifest=manifest,
            process=process,
            stderr_chunks=stderr_chunks,
        )
        raise failure from exc
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _attach_stream_diagnostics(
    exc: ProviderStreamError,
    *,
    activities: list[ProviderActivity],
    manifest: ProviderActivityManifest | None,
    process: subprocess.Popen[str] | None,
    stderr_chunks: list[str],
) -> None:
    stderr_raw = "".join(stderr_chunks).encode("utf-8", errors="replace")
    if not exc.activities:
        exc.activities = tuple(activities)
    if exc.manifest is None:
        exc.manifest = manifest
    if exc.returncode is None and process is not None:
        exc.returncode = process.poll()
    if exc.stderr_digest is None:
        exc.stderr_digest = f"sha256:{hashlib.sha256(stderr_raw).hexdigest()}"
        exc.stderr_length = len(stderr_raw)


def _parse_stream_line(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProviderStreamError("provider broker emitted malformed JSON") from exc
    if not isinstance(value, dict):
        raise ProviderStreamError("provider broker record must be an object")
    if value.get("schema_version") != PROVIDER_STREAM_SCHEMA_VERSION:
        raise ProviderStreamError(f"unsupported provider stream schema: {value.get('schema_version')!r}")
    record_type = value.get("record_type")
    if record_type not in {_ACTIVITY, _MANIFEST, _RESULT}:
        raise ProviderStreamError(f"unknown provider stream record type: {record_type!r}")
    expected = {"schema_version", "record_type", record_type}
    if set(value) != expected:
        raise ProviderStreamError("provider stream record has unknown or missing top-level fields")
    return value


def _pump_lines(stream: Any, channel: str, target: queue.Queue[tuple[str, str | None]]) -> None:
    try:
        for line in stream:
            target.put((channel, line))
    finally:
        target.put((channel, None))


def _broker_environment(additional: Mapping[str, str]) -> dict[str, str]:
    """Keep runtime/toolchain settings while dropping ambient provider secrets."""
    keep = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "TMPDIR",
        "TMP",
        "TEMP",
    }
    environment = {key: value for key, value in os.environ.items() if key in keep}
    unexpected = set(additional).difference(keep)
    if unexpected:
        raise ProviderStreamError(f"provider broker environment contains unsupported keys: {sorted(unexpected)!r}")
    environment.update(additional)
    for key in ("HTTP_PROXY", "HTTPS_PROXY"):
        value = environment.get(key)
        if value:
            parsed = urllib.parse.urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


__all__ = [
    "PROVIDER_STREAM_SCHEMA_VERSION",
    "ProviderProcessRequest",
    "ProviderProcessResult",
    "ProviderStreamError",
    "emit_provider_stream_record",
    "supervise_provider_process",
]
