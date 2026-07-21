from __future__ import annotations

import json
import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from shepherd_dialect import provider_stream
from shepherd_dialect.provider_activity import ProviderActivityLedger, ProviderActivityManifest
from shepherd_dialect.provider_stream import ProviderProcessRequest, ProviderStreamError

if TYPE_CHECKING:
    from pathlib import Path


def _request(workspace: Path, *, deadline: float = 2.0) -> ProviderProcessRequest:
    return ProviderProcessRequest(
        adapter_id="codex-python",
        provider_id="fixture",
        invocation_id="fixture:stream",
        working_directory=workspace,
        payload={},
        deadline_seconds=deadline,
    )


def _set_command(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setitem(provider_stream._REGISTERED_ADAPTERS, "codex-python", (sys.executable, "-u", "-c", code))


def _record(record_type: str, payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": provider_stream.PROVIDER_STREAM_SCHEMA_VERSION,
            "record_type": record_type,
            record_type: payload,
        }
    )


def _empty_manifest() -> ProviderActivityManifest:
    return ProviderActivityManifest(
        provider_id="fixture",
        invocation_id="fixture:stream",
        activity_count=0,
        ingress_count=0,
        last_record_digest=None,
        terminal_seen=True,
        terminal_kind="completed",
        category_counts={},
        complete=True,
    )


def test_supervisor_preserves_prior_activity_when_stream_becomes_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ProviderActivityLedger(
        provider_id="fixture",
        invocation_id="fixture:stream",
        source="fixture.transport",
        projector=lambda _message, _state: {"category": "notification", "kind": "notification.fixture"},
    )
    activity = ledger.append_ingress('{"method":"fixture"}\n')
    record = {
        "schema_version": provider_stream.PROVIDER_STREAM_SCHEMA_VERSION,
        "record_type": "activity",
        "activity": activity.as_wire_record(),
    }
    output = json.dumps(record) + "\nnot-json\n"
    _set_command(monkeypatch, f"import sys;sys.stdout.write({output!r});sys.stdout.flush()")

    with pytest.raises(ProviderStreamError, match="malformed JSON") as caught:
        provider_stream.supervise_provider_process(_request(tmp_path))

    assert caught.value.activities == (activity,)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('{"schema_version":"shepherd.provider_stream.v1","record_type":"future","future":{}}', "unknown"),
        ("", "without an activity manifest"),
    ],
)
def test_supervisor_fails_closed_on_unknown_or_truncated_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, message: str
) -> None:
    output = line + "\n" if line else ""
    _set_command(monkeypatch, f"import sys;sys.stdout.write({output!r});sys.stdout.flush()")
    with pytest.raises(ProviderStreamError, match=message):
        provider_stream.supervise_provider_process(_request(tmp_path))


def test_hard_deadline_kills_the_entire_broker_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid));"
        "time.sleep(60)"
    )
    _set_command(monkeypatch, code)

    with pytest.raises(ProviderStreamError, match="hard deadline"):
        provider_stream.supervise_provider_process(_request(tmp_path, deadline=0.3))

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("broker child survived process-group termination")


def test_broker_environment_scrubs_secrets_and_credentialed_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-password@proxy.example:8443")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")

    environment = provider_stream._broker_environment({"NO_PROXY": "localhost"})

    assert "OPENAI_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["HTTP_PROXY"] == "http://proxy.example:8080"
    assert environment["NO_PROXY"] == "localhost"
    assert "sk-ambient-secret" not in repr(environment)
    assert "proxy-password" not in repr(environment)


def test_broker_environment_rejects_non_toolchain_additions() -> None:
    with pytest.raises(ProviderStreamError, match="unsupported keys"):
        provider_stream._broker_environment({"OPENAI_API_KEY": "must-not-cross"})


@pytest.mark.parametrize(
    ("records", "message"),
    [
        (("result",), "result before manifest"),
        (("manifest", "manifest"), "multiple manifests"),
        (("manifest", "result", "result"), "after its terminal result"),
    ],
)
def test_supervisor_rejects_invalid_terminal_record_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: tuple[str, ...],
    message: str,
) -> None:
    manifest = _empty_manifest().as_wire_record()
    bodies = {
        "manifest": manifest,
        "result": {"status": "ok"},
    }
    output = "".join(_record(record_type, bodies[record_type]) + "\n" for record_type in records)
    _set_command(monkeypatch, f"import sys;sys.stdout.write({output!r});sys.stdout.flush()")

    with pytest.raises(ProviderStreamError, match=message):
        provider_stream.supervise_provider_process(_request(tmp_path))


def test_bounded_stream_pump_applies_backpressure_without_dropping_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_count = 1500
    code = f"""
from shepherd_dialect.provider_activity import ProviderActivityLedger
from shepherd_dialect.provider_stream import emit_provider_stream_record
ledger = ProviderActivityLedger(
    provider_id="fixture",
    invocation_id="fixture:stream",
    source="fixture.transport",
    projector=lambda message, state: {{"category": "notification", "kind": "notification.tick", "method": "tick"}},
)
for _ in range({record_count}):
    activity = ledger.append_ingress('{{"method":"tick"}}\\n')
    emit_provider_stream_record("activity", activity.as_wire_record())
manifest = ledger.manifest(terminal_kind="completed", terminal_seen=True)
emit_provider_stream_record("manifest", manifest.as_wire_record())
emit_provider_stream_record("result", {{"status": "ok"}})
"""
    _set_command(monkeypatch, code)

    result = provider_stream.supervise_provider_process(_request(tmp_path, deadline=10.0))

    assert len(result.activities) == record_count
    assert result.manifest.activity_count == record_count
    assert result.activities[-1].sequence == record_count - 1


def test_stderr_is_digested_separately_from_a_valid_activity_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _record("manifest", _empty_manifest().as_wire_record()) + "\n" + _record("result", {"status": "ok"}) + "\n"
    code = f"import sys;sys.stderr.write('private diagnostic\\n');sys.stdout.write({output!r});sys.stdout.flush()"
    _set_command(monkeypatch, code)

    result = provider_stream.supervise_provider_process(_request(tmp_path))

    assert result.stderr_length == len("private diagnostic\n")
    assert result.stderr_digest.startswith("sha256:")
    assert result.activities == ()


def test_nonzero_broker_exit_cannot_succeed_with_a_valid_manifest_and_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _record("manifest", _empty_manifest().as_wire_record()) + "\n" + _record("result", {"status": "ok"}) + "\n"
    code = f"import sys;sys.stdout.write({output!r});sys.stdout.flush();sys.exit(3)"
    _set_command(monkeypatch, code)

    with pytest.raises(ProviderStreamError, match="provider broker failed") as caught:
        provider_stream.supervise_provider_process(_request(tmp_path))

    assert caught.value.returncode == 3
    assert caught.value.manifest == _empty_manifest()
