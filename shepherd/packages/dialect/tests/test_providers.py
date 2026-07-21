"""Keyless shape tests for the dialect's providers (W1 of the real-SDK demo plan).

The demo provider is never a CI gate (``deterministic-fake-v1-provider``) — what
CI pins is its *shape*: the negotiation discipline shared with the fake, the
S1-proven argv (`spikes/260610-real-sdk-jail-probe`, 5/5), and the dialect's
dependency posture (CLI-direct: no SDK package, no legacy ``shepherd_providers``
reach). Nothing here touches the network, the key, or the CLI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect import ClaudeAgentProvider, DeterministicFakeProvider
from shepherd_dialect import providers as providers_module
from shepherd_dialect.provider_runtime import (
    MODEL_CALL,
    MODEL_TURN,
    PROVIDER_INVOCATION_COMPLETED,
    PROVIDER_INVOCATION_FAILED,
    PROVIDER_INVOCATION_STARTED,
    TOOL_CALL_COMPLETED,
    TOOL_CALL_STARTED,
)
from shepherd_dialect.providers import (
    ClaudeHeadlessProvider,
    HermesHeadlessProvider,
    claude_auth,
    claude_auth_mode,
    hermes_auth_status,
)
from shepherd_dialect.providers.claude_auth import _HostLoginLookup
from shepherd_dialect.providers.claude_cli import _diagnose_claude_cli_failure


def _found(blob: bytes) -> _HostLoginLookup:
    """A host-login lookup that found a credential in the default config file."""
    return _HostLoginLookup(blob, (("default_config", "default_config_found"),))


def _absent() -> _HostLoginLookup:
    """A host-login lookup that found no credential anywhere."""
    return _HostLoginLookup(None, (("default_config", "default_config_missing"),))


@pytest.mark.parametrize(
    "provider",
    [
        DeterministicFakeProvider(),
        ClaudeAgentProvider(prompt="x"),
        HermesHeadlessProvider(prompt="x", model="m", model_provider="anthropic"),
    ],
)
def test_providers_refuse_without_execution_authority(provider) -> None:
    """These bodies run only jailed — no capability/spec, no execution (fail-closed)."""
    with pytest.raises(ExecutionAuthorityRequired):
        provider.execute(None, None, None, {}, execution=None, confinement=None)


def _assert_hard_stop(argv, budget_seconds: int) -> None:
    """The outermost budget alarm: the tree-reaper on Linux, perl elsewhere (§4.6)."""
    if sys.platform.startswith("linux"):
        assert argv[0] == sys.executable, "the reaper runs under this interpreter"
        assert argv[1].endswith("_reaper.py"), "the tree-reaping supervisor must be outermost"
        assert argv[2] == str(budget_seconds)
    else:
        assert argv[0] == "/usr/bin/perl", "the alarm prefix must be outermost"
        assert "alarm" in argv[2]
        assert argv[3] == str(budget_seconds)


def test_command_argv_is_the_s1_shape(tmp_path) -> None:
    """Hard stop outermost, env redirect into the single writable root, body last."""
    provider = ClaudeAgentProvider(prompt="do the thing", max_turns=3, budget_seconds=90)
    argv = provider.command_argv(tmp_path, "/somewhere/claude")
    _assert_hard_stop(argv, 90)
    env_block = argv[argv.index("/usr/bin/env") : argv.index("/somewhere/claude")]
    scratch = str(tmp_path / ".claude-scratch")
    for var in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
        assert any(a.startswith(f"{var}={scratch}") for a in env_block), f"{var} must redirect into the scratch"
    assert "DISABLE_AUTOUPDATER=1" in env_block
    body = argv[argv.index("/somewhere/claude") :]
    assert body[1:3] == ["-p", "do the thing"]
    assert body[body.index("--allowed-tools") + 1] == "Write,Edit,Read"
    assert body[body.index("--max-turns") + 1] == "3"


def _clear_claude_auth_env(monkeypatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SHEPHERD_NO_CREDENTIAL_SEEDING",
        "SHEPHERD_ALLOW_KEYLESS_CLAUDE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_claude_auth_prefers_env_credentials(monkeypatch) -> None:
    """Env-carried credentials win; no host login is read."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(b"{}"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert claude_auth_mode() == "api_key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert claude_auth_mode() == "oauth_token"


def test_claude_auth_uses_host_login_with_opt_out(monkeypatch) -> None:
    """Keyless + signed-in CLI → subscription seeding; opt-out env disables it."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(b"{}"))
    assert claude_auth_mode() == "subscription_login"
    monkeypatch.setenv("SHEPHERD_NO_CREDENTIAL_SEEDING", "1")
    assert claude_auth_mode() is None


def test_claude_auth_none_without_any_credentials(monkeypatch) -> None:
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _absent())
    assert claude_auth_mode() is None


def test_headless_execute_seeds_login_into_scratch_and_scrubs(tmp_path, monkeypatch) -> None:
    """Keyless launch seeds .credentials.json (0600) into the scratch config; scrub removes it."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(b'{"probe": true}'))
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
    seen = {}

    class _Proc:
        returncode = 1
        stderr = "refused for the probe"
        stdout = ""

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            cred = tmp_path / ".claude-scratch" / "config" / ".credentials.json"
            seen["present"] = cred.is_file()
            seen["mode"] = cred.stat().st_mode & 0o777 if cred.is_file() else None
            return _Proc()

    provider = ClaudeHeadlessProvider(prompt="x")
    with pytest.raises(RuntimeError):
        provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())
    assert seen["present"] is True
    assert seen["mode"] == 0o600
    assert not (tmp_path / ".claude-scratch").exists()


def test_headless_execute_does_not_seed_when_env_key_present(tmp_path, monkeypatch) -> None:
    """With an env credential the scratch config stays empty — no host login is touched."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        claude_auth,
        "_read_host_claude_login",
        lambda: (_ for _ in ()).throw(AssertionError("host login must not be read")),
    )
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
    seen = {}

    class _Proc:
        returncode = 1
        stderr = "refused for the probe"
        stdout = ""

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            seen["cred_absent"] = not (tmp_path / ".claude-scratch" / "config" / ".credentials.json").exists()
            return _Proc()

    provider = ClaudeHeadlessProvider(prompt="x")
    with pytest.raises(RuntimeError):
        provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())
    assert seen["cred_absent"] is True


def test_headless_argv_is_uncapped_by_default(tmp_path) -> None:
    """No ``max_turns`` set → no ``--max-turns`` flag; the budget alarm is the bound."""
    argv = ClaudeHeadlessProvider(prompt="do the thing").command_argv(tmp_path, "/somewhere/claude")
    assert "--max-turns" not in argv
    # The wall-clock alarm still rides the argv as the always-on guardrail.
    _assert_hard_stop(argv, 240)


def test_headless_argv_passes_explicit_turn_cap(tmp_path) -> None:
    """An explicit ``max_turns`` opts into a hard turn cap via ``--max-turns``."""
    argv = ClaudeHeadlessProvider(prompt="do the thing", max_turns=8, budget_seconds=90).command_argv(
        tmp_path, "/somewhere/claude"
    )
    assert argv[argv.index("--max-turns") + 1] == "8"
    _assert_hard_stop(argv, 90)


def test_headless_argv_omits_json_schema_by_default(tmp_path) -> None:
    """No ``json_schema`` set → no ``--json-schema`` flag, so CLIs that predate
    the flag never see it and the argv stays the S1-proven shape."""
    argv = ClaudeHeadlessProvider(prompt="do the thing").command_argv(tmp_path, "/somewhere/claude")
    assert "--json-schema" not in argv


def test_headless_argv_passes_json_schema_when_set(tmp_path) -> None:
    """A ``json_schema`` rides the argv serialized, adjacent to its flag."""
    schema = {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]}
    argv = ClaudeHeadlessProvider(prompt="do the thing", json_schema=schema).command_argv(tmp_path, "/somewhere/claude")
    serialized = argv[argv.index("--json-schema") + 1]
    assert json.loads(serialized) == schema


def test_headless_capabilities_structured_output_tracks_schema() -> None:
    """The executable capability claim is per-instance: a schema-demanding
    invocation claims ``structured_output``; a plain one does not."""
    assert ClaudeHeadlessProvider(prompt="x").capabilities.structured_output is False
    assert ClaudeHeadlessProvider(prompt="x", json_schema={"type": "object"}).capabilities.structured_output is True
    # The resume claim is untouched by this axis (S2, not S1).
    assert ClaudeHeadlessProvider(prompt="x", json_schema={"type": "object"}).capabilities.session_resume is False


def test_claude_api_does_not_claim_session_resume() -> None:
    """``resume`` is plumbed to the SDK, but the executable claim is False: the
    scrubbed per-run scratch guarantees the transcript a resume needs is absent
    across jailed runs. Pin the corrected claim until session state survives
    runs as a recorded, addressable input."""
    from shepherd_dialect import ClaudeApiProvider

    caps = ClaudeApiProvider(prompt="x", resume="11111111-1111-1111-1111-111111111111").capabilities
    assert caps.session_resume is False
    # The rest of the transport's claims are untouched by the correction.
    assert caps.structured_output is True
    assert caps.transport == "agent_sdk_worker"


# A trimmed success envelope carrying the CLI's schema-validated object — the
# ``structured_output`` field appears when ``--json-schema`` rode the argv
# (empirically pinned against claude CLI 2.1.202).
_STRUCTURED_OK_STDOUT = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"{\\"n\\":42}","structured_output":{"n":42},'
    '"session_id":"11111111-1111-1111-1111-111111111111",'
    '"usage":{"input_tokens":1},"num_turns":1,'
    '"uuid":"00000000-0000-0000-0000-000000000000"}\n'
)

# The same success envelope with no ``structured_output`` field — what a run
# looks like when the CLI could not produce a schema-conforming object.
_STRUCTURED_MISSING_STDOUT = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"result":"done","session_id":"11111111-1111-1111-1111-111111111111",'
    '"usage":{"input_tokens":1},"num_turns":1,'
    '"uuid":"00000000-0000-0000-0000-000000000000"}\n'
)


def test_headless_structured_output_lifted_into_outcome(tmp_path, monkeypatch) -> None:
    """rc=0 + envelope ``structured_output`` → the typed object lands in the
    invocation result and the recorded outcome, not just the text channel."""

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = _STRUCTURED_OK_STDOUT

    provider = ClaudeHeadlessProvider(prompt="x", json_schema={"type": "object"})
    result = _run_headless_with_proc(tmp_path, monkeypatch, _Proc(), provider=provider)
    assert result.outcome["structured_output"] == {"n": 42}
    assert result.outcome["session_id"] == "11111111-1111-1111-1111-111111111111"
    kinds = [event.kind for event in result.provider_events]
    assert kinds[0] == PROVIDER_INVOCATION_STARTED
    assert kinds[-1] == PROVIDER_INVOCATION_COMPLETED


def test_headless_structured_output_absent_without_schema_still_succeeds(tmp_path, monkeypatch) -> None:
    """No schema demanded → a plain-text success stays a success (regression
    guard: the lift must not turn ordinary runs into refusals)."""

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = _STRUCTURED_MISSING_STDOUT

    result = _run_headless_with_proc(tmp_path, monkeypatch, _Proc())
    assert result.outcome["structured_output"] is None
    assert result.outcome["terminal"] == "success"


def test_headless_missing_structured_output_fails_loudly(tmp_path, monkeypatch) -> None:
    """Schema demanded but the success envelope carries no validated object →
    a named refusal, not an empty typed result the caller would misread."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = _STRUCTURED_MISSING_STDOUT

    provider = ClaudeHeadlessProvider(prompt="x", json_schema={"type": "object"})
    with pytest.raises(ProviderInvocationError, match="no structured_output") as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc(), provider=provider)
    failed = excinfo.value.provider_events[-1]
    assert failed.payload["error_type"] == "StructuredOutputMissing"


def test_provider_requires_a_prompt() -> None:
    """The prompt is the body — an empty one is a caller bug, not an API call."""
    provider = ClaudeAgentProvider()

    class _Cap:
        working_path = "/nowhere"

    with pytest.raises(ValueError, match="needs a prompt"):
        provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())


# A faithful (trimmed) headless not-logged-in envelope: the CLI reports the auth
# failure *inside* a well-formed stream-json result and still exits 1, with the
# reason ~900 chars before the trailing bookkeeping fields a tail-slice surfaces.
_NOT_LOGGED_IN_STDOUT = (
    '{"type":"assistant","message":{"content":[{"type":"text",'
    '"text":"Not logged in \\u00b7 Please run /login"}],"error":"authentication_failed"}}\n'
    '{"type":"result","subtype":"success","is_error":true,'
    '"result":"Not logged in \\u00b7 Please run /login","usage":{"service_tier":"standard",'
    '"inference_geo":"","iterations":[],"speed":"standard"},"modelUsage":{},'
    '"permission_denials":[],"terminal_reason":"completed","fast_mode_state":"off",'
    '"uuid":"00000000-0000-0000-0000-000000000000"}\n'
)


# An org-policy denial: valid credential, but the account/organization is not
# permitted (HTTP 403). The human message is in `result`, `api_error_status:403`
# is the machine signal, and the model was never called (empty modelUsage).
_ORG_403_STDOUT = (
    '{"type":"assistant","message":{"content":[{"type":"text",'
    '"text":"Your organization has disabled Claude subscription access for this account."}],'
    '"error":"permission_error"}}\n'
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":403,'
    '"result":"Your organization has disabled Claude subscription access for this account.",'
    '"usage":{"iterations":[]},"modelUsage":{},"terminal_reason":"completed",'
    '"uuid":"00000000-0000-0000-0000-000000000000"}\n'
)


def _run_headless_with_proc(tmp_path, monkeypatch, proc, provider=None):
    """Drive ClaudeHeadlessProvider.execute against a fake confined process.

    Opts into ``SHEPHERD_ALLOW_KEYLESS_CLAUDE`` so the body actually launches:
    these tests exercise the *CLI-envelope diagnosis* of a returned proc, not the
    keyless preflight (which is covered separately below)."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setenv("SHEPHERD_ALLOW_KEYLESS_CLAUDE", "1")
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _absent())
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            return proc

    provider = ClaudeHeadlessProvider(prompt="x") if provider is None else provider
    return provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())


def test_headless_auth_failure_surfaces_cli_reason_and_remedy(tmp_path, monkeypatch) -> None:
    """rc=1 not-logged-in envelope → the CLI's own reason + an actionable remedy,
    not a blind tail-slice that drops the cause (the reporter's opaque case)."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    class _Proc:
        returncode = 1
        stderr = ""
        stdout = _NOT_LOGGED_IN_STDOUT

    with pytest.raises(ProviderInvocationError) as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc())

    message = str(excinfo.value)
    assert message.startswith("confined body refused (rc=1)")
    assert "Not logged in" in message, "the CLI's own reason must be surfaced, not dropped"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in message
    assert "ANTHROPIC_API_KEY" in message
    failed = excinfo.value.provider_events[-1]
    assert failed.payload["failure_classification"] == "auth_failure"
    # the durable trace keeps the full envelope, not just a 300-char tail
    assert failed.payload["stdout_length"] == len(_NOT_LOGGED_IN_STDOUT)


def test_agent_provider_auth_failure_is_actionable(tmp_path, monkeypatch) -> None:
    """The legacy ClaudeAgentProvider gets the same actionable reason + remedy
    (it raises RuntimeError, not ProviderInvocationError, and records no events)."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)

    class _Proc:
        returncode = 1
        stderr = ""
        stdout = _NOT_LOGGED_IN_STDOUT

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            return _Proc()

    provider = ClaudeAgentProvider(prompt="x")
    with pytest.raises(RuntimeError, match="Not logged in") as excinfo:
        provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())
    message = str(excinfo.value)
    assert message.startswith("confined body refused (rc=1)")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in message


def test_headless_root_permission_failure_is_actionable(tmp_path, monkeypatch) -> None:
    """The rootful `--dangerously-skip-permissions` refusal gets its own remedy."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    class _Proc:
        returncode = 1
        stderr = "--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons"
        stdout = ""

    with pytest.raises(ProviderInvocationError) as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc())
    message = str(excinfo.value)
    assert "non-root user" in message
    assert excinfo.value.provider_events[-1].payload["failure_classification"] == "root_permission"


def test_headless_access_denied_403_is_classified(tmp_path, monkeypatch) -> None:
    """An org-policy 403 is `access_denied` (not `auth_failure`): the human message
    is surfaced, the 403 status is preserved, and the remedy is not "re-login"."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    class _Proc:
        returncode = 1
        stderr = ""
        stdout = _ORG_403_STDOUT

    with pytest.raises(ProviderInvocationError) as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc())

    message = str(excinfo.value)
    assert "disabled Claude subscription access" in message, "the CLI's own reason must be surfaced"
    assert "organization policy" in message or "organization" in message
    assert "setup-token" not in message, "a 403 is not a login problem; do not steer to re-login first"
    payload = excinfo.value.provider_events[-1].payload
    assert payload["failure_classification"] == "access_denied"
    assert payload["cli_api_error_status"] == 403
    assert payload["cli_is_error"] is True
    assert payload["cli_assistant_error"] == "permission_error"


def test_access_denied_classified_from_assistant_error_without_status() -> None:
    """A `permission_error` assistant message classifies `access_denied` even when the
    envelope omits `api_error_status` and the `result` text matches no policy phrase —
    the extracted assistant error must drive classification, not just be recorded."""
    stdout = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"..."}],'
        '"error":"permission_error"}}\n'
        '{"type":"result","subtype":"success","is_error":true,'
        '"result":"Request failed with status 403","modelUsage":{},'
        '"terminal_reason":"completed","uuid":"0"}\n'
    )
    diagnosis = _diagnose_claude_cli_failure(1, stdout, "")
    assert diagnosis.classification == "access_denied"
    assert diagnosis.cli_api_error_status is None
    assert diagnosis.cli_assistant_error == "permission_error"
    assert "setup-token" not in (diagnosis.remedy or ""), "a 403 is not a login problem"


def test_access_denied_classified_from_bare_403_result_text() -> None:
    """A bare `403`/`forbidden` in the result text classifies `access_denied` even with
    no assistant error and no structured status (a differently-phrased policy denial)."""
    stdout = (
        '{"type":"result","subtype":"success","is_error":true,'
        '"result":"HTTP 403 Forbidden returned by the API","modelUsage":{},"uuid":"0"}\n'
    )
    diagnosis = _diagnose_claude_cli_failure(1, stdout, "")
    assert diagnosis.classification == "access_denied"


def test_headless_alarm_kill_maps_to_budget_exhausted(tmp_path, monkeypatch) -> None:
    """SIGALRM (rc -14) from the budget alarm is a trace-preserving Exhausted, not a refusal;
    the started bookend rides the exception's events channel (§4.7)."""
    from shepherd_dialect.nucleus import BudgetExhausted

    class _Proc:
        returncode = -14
        stderr = ""
        stdout = ""

    with pytest.raises(BudgetExhausted, match="budget exceeded") as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc())
    kinds = [event.kind for event in excinfo.value.provider_events]
    assert kinds == [PROVIDER_INVOCATION_STARTED], "the exhausted run must keep its started evidence"


def test_headless_scrub_residue_fails_closed(tmp_path, monkeypatch) -> None:
    """§4.7: residue after the scrub is refused loudly — the claude scratch holds
    the seeded .credentials.json, which must never ride the captured delta."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = '{"type":"result","subtype":"success","result":"ok","session_id":"s"}'

    with pytest.raises(ProviderInvocationError, match="scrub left residue") as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc())
    assert excinfo.value.provider_events[-1].payload["error_type"] == "ScratchScrubResidue"


def test_legacy_scrub_residue_fails_closed(tmp_path, monkeypatch) -> None:
    """§4.7: the legacy lane refuses residue in its RuntimeError idiom (no events)."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = "ok"

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            return _Proc()

    with pytest.raises(RuntimeError, match="scrub left residue"):
        ClaudeAgentProvider(prompt="x").execute(None, None, None, {}, execution=_Cap(), confinement=object())


def test_claude_stream_id_less_tool_calls_pair(tmp_path) -> None:
    """§4.7: id-less tool_use/tool_result blocks pair via the fallback queue —
    the completed event inherits the started event's synthetic id, not a fresh one."""
    from shepherd_dialect.providers.claude_cli import _claude_stream_events_to_provider_events

    stream = (
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "done"}]}},
    )
    events = _claude_stream_events_to_provider_events(
        stream, provider_id="claude-headless", invocation_id="inv", model="m", sequence_start=0
    )
    assert [e.kind for e in events] == [TOOL_CALL_STARTED, TOOL_CALL_COMPLETED]
    assert events[0].tool_call_id == events[1].tool_call_id
    assert events[1].payload["canonical_tool_name"] == "write_file", "the pairing recovers the tool name"


def test_headless_alarm_kill_hints_at_hung_body_when_silent(tmp_path, monkeypatch) -> None:
    """rc=-14 with zero output reads as a hung body (stale CLI / blocked network),
    not "the model ran long"; when the model produced output, no hint is added."""
    from shepherd_dialect.nucleus import BudgetExhausted

    class _Silent:
        returncode = -14
        stderr = ""
        stdout = ""

    with pytest.raises(BudgetExhausted, match="hung before starting"):
        _run_headless_with_proc(tmp_path, monkeypatch, _Silent())

    class _Ran:
        returncode = -14
        stderr = ""
        stdout = '{"type":"assistant","message":{"content":[{"type":"text","text":"working..."}]}}'

    with pytest.raises(BudgetExhausted) as excinfo:
        _run_headless_with_proc(tmp_path, monkeypatch, _Ran())
    assert "hung before starting" not in str(excinfo.value)


class _RecordingCap:
    """A fake ExecutionCapability that records whether launch_confined ran."""

    def __init__(self, tmp_path, proc=None):
        self.working_path = str(tmp_path)
        self.launched = False
        self._proc = proc

    def launch_confined(self, command, confinement):
        self.launched = True
        return self._proc


def _headless_with_cap(monkeypatch, cap, *, host_login, allow_keyless=False):
    """Run ClaudeHeadlessProvider.execute against a recording cap, with env cleared."""
    _clear_claude_auth_env(monkeypatch)
    if allow_keyless:
        monkeypatch.setenv("SHEPHERD_ALLOW_KEYLESS_CLAUDE", "1")
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: host_login)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
    provider = ClaudeHeadlessProvider(prompt="x")
    return provider.execute(None, None, None, {}, execution=cap, confinement=object())


def test_headless_refuses_keyless_launch_before_confinement(tmp_path, monkeypatch) -> None:
    """No env credential and no host login → refuse *before* launch_confined, naming all
    three exits; the failed event says launch_attempted:false (a preflight, not a jail denial)."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    cap = _RecordingCap(tmp_path)
    with pytest.raises(ProviderInvocationError) as excinfo:
        _headless_with_cap(monkeypatch, cap, host_login=_absent())

    assert cap.launched is False, "a known-keyless run must not spend a confined launch"
    message = str(excinfo.value)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "SHEPHERD_ALLOW_KEYLESS_CLAUDE" in message
    failed = excinfo.value.provider_events[-1]
    assert failed.payload["failure_classification"] == "auth_missing"
    assert failed.payload["error_type"] == "ClaudeAuthMissing"
    assert failed.payload["launch_attempted"] is False
    assert failed.payload["auth_mode"] == "none"


def test_headless_keyless_allow_flag_permits_launch(tmp_path, monkeypatch) -> None:
    """SHEPHERD_ALLOW_KEYLESS_CLAUDE=1 opts a wrapper back into the launch path."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    class _Proc:
        returncode = 1
        stderr = ""
        stdout = _NOT_LOGGED_IN_STDOUT

    cap = _RecordingCap(tmp_path, _Proc())
    with pytest.raises(ProviderInvocationError):  # the CLI still fails, but the body ran
        _headless_with_cap(monkeypatch, cap, host_login=_absent(), allow_keyless=True)
    assert cap.launched is True


def test_headless_refuses_expired_login_before_confinement(tmp_path, monkeypatch) -> None:
    """An expired seeded subscription blob is refused before launch as auth_expired —
    a jailed run cannot refresh it, so seeding-and-launching is a guaranteed failure."""
    import time

    from shepherd_dialect.provider_runtime import ProviderInvocationError

    expired = int((time.time() - 3600) * 1000)
    cap = _RecordingCap(tmp_path)
    with pytest.raises(ProviderInvocationError) as excinfo:
        _headless_with_cap(monkeypatch, cap, host_login=_found(b'{"claudeAiOauth":{"expiresAt":%d}}' % expired))

    assert cap.launched is False
    failed = excinfo.value.provider_events[-1]
    assert failed.payload["failure_classification"] == "auth_expired"
    assert failed.payload["error_type"] == "ClaudeAuthExpired"
    assert failed.payload["launch_attempted"] is False
    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(excinfo.value)


def test_claude_auth_status_env_and_absent(monkeypatch) -> None:
    """Env credentials pass; no credentials is a hard offline fail."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _absent())
    status = providers_module.claude_auth_status()
    assert status.mode is None
    assert status.ok is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    status = providers_module.claude_auth_status()
    assert status.mode == "api_key"
    assert status.ok is True


def test_claude_auth_status_hard_fails_on_expired_subscription_blob(monkeypatch) -> None:
    """A readable-but-expired login is a hard fail (a jailed run cannot refresh it);
    a valid blob is ok-but-unverified; an unrecognized shape is ok-but-unverified."""
    import time

    _clear_claude_auth_env(monkeypatch)
    expired = int((time.time() - 3600) * 1000)
    valid = int((time.time() + 3600) * 1000)

    monkeypatch.setattr(
        claude_auth, "_read_host_claude_login", lambda: _found(b'{"claudeAiOauth":{"expiresAt":%d}}' % expired)
    )
    status = providers_module.claude_auth_status()
    assert status.mode == "subscription_login"
    assert status.ok is False
    assert "expired" in status.detail

    monkeypatch.setattr(
        claude_auth, "_read_host_claude_login", lambda: _found(b'{"claudeAiOauth":{"expiresAt":%d}}' % valid)
    )
    status = providers_module.claude_auth_status()
    assert status.ok is True
    assert "not verified" in status.detail

    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(b'{"unexpected":"shape"}'))
    status = providers_module.claude_auth_status()
    assert status.ok is True
    assert "not verified" in status.detail


def test_claude_auth_status_names_the_keyless_source(monkeypatch) -> None:
    """A `mode is None` verdict names the source that failed, not a flat "no credentials"."""
    _clear_claude_auth_env(monkeypatch)

    # Seeding disabled by env is distinct from "nothing found".
    monkeypatch.setenv("SHEPHERD_NO_CREDENTIAL_SEEDING", "1")
    status = providers_module.claude_auth_status()
    assert status.ok is False
    assert "seeding is disabled" in status.detail
    monkeypatch.delenv("SHEPHERD_NO_CREDENTIAL_SEEDING")

    # A keychain denial/timeout is distinguishable from a clean "not found".
    monkeypatch.setattr(
        claude_auth,
        "_read_host_claude_login",
        lambda: _HostLoginLookup(None, (("macos_keychain", "keychain_timeout"),)),
    )
    assert "keychain lookup timed out" in providers_module.claude_auth_status().detail

    monkeypatch.setattr(
        claude_auth,
        "_read_host_claude_login",
        lambda: _HostLoginLookup(None, (("macos_keychain", "keychain_failed"),)),
    )
    assert "denied or failed" in providers_module.claude_auth_status().detail

    # An actionable earlier status (unreadable file) is surfaced even when a later
    # attempt terminates the trail with a plain "not found" — the trail is scanned
    # whole, not just its last status.
    monkeypatch.setattr(
        claude_auth,
        "_read_host_claude_login",
        lambda: _HostLoginLookup(
            None,
            (
                ("default_config", "default_config_unreadable"),
                ("macos_keychain", "keychain_not_found"),
            ),
        ),
    )
    assert "unreadable" in providers_module.claude_auth_status().detail


def test_read_host_login_records_source_trail_without_secrets(tmp_path, monkeypatch) -> None:
    """The real reader returns a `(source_class, status)` trail — no paths, no bytes —
    and continues past a missing configured source to the default file."""
    _clear_claude_auth_env(monkeypatch)
    cfg = tmp_path / "cfgdir"
    cfg.mkdir()
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_bytes(b'{"claudeAiOauth":{"expiresAt":1}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))  # dir exists, credential file does not
    monkeypatch.setattr(claude_auth.Path, "home", classmethod(lambda cls: home))

    lookup = claude_auth._read_host_claude_login()
    assert lookup.blob == b'{"claudeAiOauth":{"expiresAt":1}}'
    assert lookup.source == "default_config"
    assert lookup.attempts[0] == ("configured_config", "configured_config_missing")
    assert lookup.attempts[-1] == ("default_config", "default_config_found")
    # The trail is source classes + coarse statuses only — never a path or bytes.
    flat = repr(lookup.attempts)
    assert str(cfg) not in flat
    assert str(home) not in flat
    assert "expiresAt" not in flat


def test_probe_claude_auth_reports_not_logged_in(monkeypatch) -> None:
    """The live probe classifies a not-logged-in envelope as a failed auth."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")  # env auth ⇒ no seeding needed
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)

    class _Proc:
        returncode = 1
        stdout = _NOT_LOGGED_IN_STDOUT
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    ok, detail = providers_module.probe_claude_auth()
    assert ok is False
    assert "Not logged in" in detail


def test_probe_claude_auth_success(monkeypatch) -> None:
    """A clean success envelope authenticates."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)

    class _Proc:
        returncode = 0
        stdout = '{"type":"result","subtype":"success","is_error":false,"result":"ok","usage":{}}'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    ok, detail = providers_module.probe_claude_auth()
    assert ok is True
    assert "authenticated" in detail


def test_probe_claude_auth_no_credentials(monkeypatch) -> None:
    """No resolvable credential is a failed probe, not a crash."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _absent())
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
    ok, detail = providers_module.probe_claude_auth()
    assert ok is False
    assert "no signed-in `claude` login found" in detail


def test_providers_import_no_sdk_and_no_legacy_reach() -> None:
    """CLI-direct posture: the dialect's dependency set is unchanged by W1."""
    probe = (
        "import sys\n"
        "import shepherd_dialect.providers\n"
        "bad = [m for m in sys.modules if m.startswith(('claude_agent_sdk', 'shepherd_providers', 'shepherd_core'))]\n"
        "print(','.join(bad) or 'none')\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "none"


# --- Hermes headless lane (execplan 260709 r4) -------------------------------
#
# Keyless shape tests: argv purity, construction invariants, the seeded config,
# capability claims, and the envelope/state.db outcome logic against a fake
# confined process. Nothing here touches the network, a key, or the hermes CLI.

_HERMES_OK_ENVELOPE = {
    "estimated_cost_usd": 0.0016723,
    "cost_status": "estimated",
    "cost_source": "official_docs_snapshot",
    "input_tokens": 3,
    "output_tokens": 5,
    "cache_read_tokens": 12368,
    "cache_write_tokens": 326,
    "reasoning_tokens": 0,
    "total_tokens": 12702,
    "api_calls": 2,
    "model": "claude-haiku-4-5",
    "provider": "anthropic",
    "session_id": "20260709_000000_ok",
    "completed": True,
    "failed": False,
}

# A faithful failed envelope (spiked): hermes exits 0, every field null except
# api_calls, completed:false / failed:true — and session_id is null, which is
# what makes the sole-session harvest fallback load-bearing.
_HERMES_FAILED_ENVELOPE = {
    "estimated_cost_usd": None,
    "cost_status": None,
    "cost_source": None,
    "input_tokens": None,
    "output_tokens": None,
    "cache_read_tokens": None,
    "cache_write_tokens": None,
    "reasoning_tokens": None,
    "total_tokens": None,
    "api_calls": 1,
    "model": None,
    "provider": None,
    "session_id": None,
    "completed": False,
    "failed": True,
}


def _write_hermes_state_db(db_path, session_ids, rows_by_session=None) -> None:
    """A minimal scratch state.db with the columns the harvest reads."""
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
        " role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    for session_id in session_ids:
        con.execute("INSERT INTO sessions (id) VALUES (?)", (session_id,))
        for row in (rows_by_session or {}).get(session_id, ()):
            con.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    row.get("role"),
                    row.get("content"),
                    row.get("tool_call_id"),
                    row.get("tool_calls"),
                    row.get("tool_name"),
                ),
            )
    con.commit()
    con.close()


_HERMES_TOOL_ROWS = (
    {"role": "user", "content": "write it"},
    {
        "role": "assistant",
        "content": "Creating the file:",
        "tool_calls": json.dumps(
            [{"id": "toolu_01", "function": {"name": "write_file", "arguments": '{"path": "out.txt"}'}}]
        ),
    },
    {"role": "tool", "content": '{"bytes_written": 7}', "tool_call_id": "toolu_01", "tool_name": "write_file"},
    {"role": "assistant", "content": "Done."},
)


def _run_hermes_with_proc(
    tmp_path,
    monkeypatch,
    proc,
    *,
    provider=None,
    envelope=_HERMES_OK_ENVELOPE,
    db_sessions=("20260709_000000_ok",),
    db_rows=None,
    corrupt_db=False,
):
    """Drive HermesHeadlessProvider.execute against a fake confined process.

    The fake launch writes the usage envelope and scratch state.db the way a
    real run would (both must exist before the post-launch reads and the D3
    scrub), then returns ``proc``.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/hermes" if cmd == "hermes" else None)
    scratch = tmp_path / ".hermes-scratch"

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            if envelope is not None:
                (scratch / "usage.json").write_text(json.dumps(envelope))
            if corrupt_db:
                (scratch / "hermes" / "state.db").write_text("not a database")
            elif db_sessions is not None:
                _write_hermes_state_db(
                    scratch / "hermes" / "state.db",
                    db_sessions,
                    db_rows or dict.fromkeys(db_sessions, _HERMES_TOOL_ROWS),
                )
            return proc

    if provider is None:
        provider = HermesHeadlessProvider(prompt="x", model="claude-haiku-4-5", model_provider="anthropic")
    return provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())


class _HermesProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_hermes_requires_model_and_model_provider() -> None:
    """Both required: a scrubbed HERMES_HOME has no account default — the seeded
    config is the model selection, so optionality is an illusion (r3 spikes:
    model-without-provider fails auth resolution; neither is an opaque 400)."""
    with pytest.raises(ValueError, match="requires both model and model_provider"):
        HermesHeadlessProvider(prompt="x", model="claude-haiku-4-5")
    with pytest.raises(ValueError, match="requires both model and model_provider"):
        HermesHeadlessProvider(prompt="x", model_provider="anthropic")
    with pytest.raises(ValueError, match="requires both model and model_provider"):
        HermesHeadlessProvider(prompt="x")


def test_hermes_rejects_model_provider_outside_the_v1_auth_set() -> None:
    """An unknown provider passing construction and failing at runtime with an
    uninformative envelope is the failure class the invariant eliminates."""
    with pytest.raises(ValueError, match="outside the v1 auth set") as excinfo:
        HermesHeadlessProvider(prompt="x", model="grok-4", model_provider="xai")
    assert "anthropic, openai, openrouter" in str(excinfo.value)


def test_hermes_argv_is_the_r4_shape(tmp_path) -> None:
    """Alarm outermost, home redirect + hardening trio, --yolo, no
    --ignore-user-config, -m/--provider always riding, -z last."""
    provider = HermesHeadlessProvider(
        prompt="do the thing", model="claude-haiku-4-5", model_provider="anthropic", budget_seconds=90
    )
    argv = provider.command_argv(tmp_path, "/somewhere/hermes")
    _assert_hard_stop(argv, 90)
    env_block = argv[argv.index("/usr/bin/env") : argv.index("/somewhere/hermes")]
    scratch = str(tmp_path / ".hermes-scratch")
    for var in ("HOME", "HERMES_HOME", "TMPDIR"):
        assert any(a.startswith(f"{var}={scratch}") for a in env_block), f"{var} must redirect into the scratch"
    for hardening in ("HERMES_SAFE_MODE=1", "HERMES_DISABLE_LAZY_INSTALLS=1", "HERMES_SKIP_NODE_BOOTSTRAP=1"):
        assert hardening in env_block
    body = argv[argv.index("/somewhere/hermes") :]
    assert "--ignore-rules" in body
    assert "--yolo" in body
    assert "--ignore-user-config" not in body, "dropped in r4: a no-op that risks the load-bearing seeding"
    assert body[body.index("-t") + 1] == "file,terminal"
    assert body[body.index("--usage-file") + 1].startswith(scratch)
    assert body[body.index("-m") + 1] == "claude-haiku-4-5"
    assert body[body.index("--provider") + 1] == "anthropic"
    assert body[-2:] == ["-z", "do the thing"], "the prompt rides -z, last"


def test_hermes_seeded_config_shape() -> None:
    """Model routing + the compression disarm (the fourth disarm — an aux LLM
    call the toolset gate cannot reach, which also rewrites harvest rows)."""
    from shepherd_dialect.providers.hermes import _hermes_seeded_config

    config = _hermes_seeded_config("claude-haiku-4-5", "anthropic")
    assert 'default: "claude-haiku-4-5"' in config
    assert 'provider: "anthropic"' in config
    assert "compression:\n  enabled: false" in config


def test_hermes_capabilities_claims() -> None:
    """Every claim executable today; search_content included (hermes's
    search_files is a ripgrep content-regex tool — r4)."""
    provider = HermesHeadlessProvider(prompt="x", model="m", model_provider="openrouter")
    caps = provider.capabilities
    assert caps.transport == "headless_cli"
    assert caps.confined is True
    assert caps.network_required is True
    assert caps.structured_output is False
    assert caps.session_resume is False
    assert caps.workspace_tools == frozenset(
        {"read_file", "write_file", "edit_file", "search_files", "search_content", "bash"}
    )
    assert caps.custom_tools is False
    assert caps.mcp is False


def test_hermes_auth_status_resolves_env_key_per_provider(monkeypatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Isolate the host Claude login: the subscription path (Phase 1) would
    # otherwise make anthropic ok=True from a real developer login.
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _absent())
    assert hermes_auth_status("anthropic").ok is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    status = hermes_auth_status("anthropic")
    assert (status.mode, status.ok) == ("env_key", True)
    assert hermes_auth_status("openai").ok is False, "the key is resolved against model_provider, not any-key"
    unsupported = hermes_auth_status("nous")
    assert unsupported.ok is False
    assert "unsupported model_provider" in unsupported.detail


def test_hermes_keyless_preflight_refuses_before_launch(tmp_path, monkeypatch) -> None:
    """No env key → a preflight refusal (launch_attempted False), not a wasted
    confined run; SHEPHERD_ALLOW_KEYLESS_HERMES opts a wrapper in."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SHEPHERD_ALLOW_KEYLESS_HERMES", raising=False)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _absent())  # no subscription fallback
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/hermes" if cmd == "hermes" else None)
    launched = []

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            launched.append(command)
            return _HermesProc(returncode=1)

    provider = HermesHeadlessProvider(prompt="x", model="m", model_provider="anthropic")
    with pytest.raises(ProviderInvocationError, match="ANTHROPIC_API_KEY") as excinfo:
        provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())
    assert launched == [], "the refusal must precede launch_confined"
    failed = excinfo.value.provider_events[-1]
    assert failed.payload["error_type"] == "HermesAuthMissing"
    assert failed.payload["launch_attempted"] is False
    monkeypatch.setenv("SHEPHERD_ALLOW_KEYLESS_HERMES", "1")
    with pytest.raises(ProviderInvocationError, match="confined body refused"):
        provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())
    assert len(launched) == 1, "the escape hatch must reach launch"


def test_hermes_success_lifts_envelope_and_state_events(tmp_path, monkeypatch) -> None:
    """The envelope becomes usage/cost evidence; the scratch state.db becomes
    tool-call events between the bookends; stdout is the output text."""
    result = _run_hermes_with_proc(tmp_path, monkeypatch, _HermesProc(stdout="Done. Created out.txt\n"))
    assert result.outcome["terminal"] == "success"
    assert result.outcome["session_id"] == "20260709_000000_ok"
    assert result.outcome["usage"]["total_tokens"] == 12702
    assert result.outcome["usage"]["estimated_cost_usd"] == pytest.approx(0.0016723)
    kinds = [event.kind for event in result.provider_events]
    assert kinds[0] == PROVIDER_INVOCATION_STARTED
    assert kinds[-1] == PROVIDER_INVOCATION_COMPLETED
    assert TOOL_CALL_STARTED in kinds
    assert TOOL_CALL_COMPLETED in kinds
    assert MODEL_CALL in kinds
    assert MODEL_TURN in kinds
    tool_start = next(e for e in result.provider_events if e.kind == TOOL_CALL_STARTED)
    assert tool_start.payload["canonical_tool_name"] == "write_file"
    assert tool_start.tool_call_id == "toolu_01"
    # the scratch is scrubbed before return — housekeeping never enters the delta
    assert not (tmp_path / ".hermes-scratch").exists()


def test_hermes_envelope_failure_is_the_outcome_authority(tmp_path, monkeypatch) -> None:
    """hermes exits 0 on failure (spiked: HTTP 401 on stdout, rc 0). The failed
    envelope carries session_id null → the sole-session fallback still harvests
    the partial transcript, which rides the failure events."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    with pytest.raises(ProviderInvocationError, match="envelope reported failure") as excinfo:
        _run_hermes_with_proc(
            tmp_path,
            monkeypatch,
            _HermesProc(returncode=0, stdout="HTTP 401: invalid x-api-key"),
            envelope=_HERMES_FAILED_ENVELOPE,
            db_sessions=("20260709_000000_fail",),
        )
    message = str(excinfo.value)
    assert "HTTP 401" in message, "the CLI's own reason must be surfaced"
    assert "ANTHROPIC_API_KEY" in message
    events = excinfo.value.provider_events
    assert events[-1].payload["failure_classification"] == "auth_failure"
    assert events[-1].payload["envelope_failed"] is True
    kinds = [event.kind for event in events]
    assert TOOL_CALL_STARTED in kinds, "sole-session fallback must keep the partial transcript"


def test_hermes_alarm_kill_maps_to_budget_exhausted(tmp_path, monkeypatch) -> None:
    """rc -14 (SIGALRM) → Exhausted; no usage file is written on a kill (spiked),
    and the partial transcript harvested before the stop rides the exception's
    events channel instead of being discarded with the budget."""
    from shepherd_dialect.nucleus import BudgetExhausted

    with pytest.raises(BudgetExhausted, match="budget exceeded") as excinfo:
        _run_hermes_with_proc(tmp_path, monkeypatch, _HermesProc(returncode=-14), envelope=None)
    kinds = [event.kind for event in excinfo.value.provider_events]
    assert kinds[0] == PROVIDER_INVOCATION_STARTED
    assert TOOL_CALL_STARTED in kinds, "the alarm-kill partial transcript must ride the exception"


def test_hermes_missing_envelope_fails_loudly(tmp_path, monkeypatch) -> None:
    """rc 0 with no usage file violates the --usage-file contract — an empty
    success would be misread as 'the agent did nothing'."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    with pytest.raises(ProviderInvocationError, match="no usage envelope") as excinfo:
        _run_hermes_with_proc(tmp_path, monkeypatch, _HermesProc(returncode=0), envelope=None)
    assert excinfo.value.provider_events[-1].payload["error_type"] == "UsageEnvelopeMissing"


def test_hermes_zero_session_db_degrades_to_bookends(tmp_path, monkeypatch) -> None:
    """A refusal can fail before the session row exists (failure dumps are
    files, not rows): null session_id + zero sessions → bookends-only, not an error."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    with pytest.raises(ProviderInvocationError) as excinfo:
        _run_hermes_with_proc(
            tmp_path,
            monkeypatch,
            _HermesProc(returncode=0, stdout="HTTP 401: invalid x-api-key"),
            envelope=_HERMES_FAILED_ENVELOPE,
            db_sessions=(),
        )
    kinds = [event.kind for event in excinfo.value.provider_events]
    assert kinds == [PROVIDER_INVOCATION_STARTED, PROVIDER_INVOCATION_FAILED]


def test_hermes_multi_session_db_degrades_to_bookends(tmp_path, monkeypatch) -> None:
    """More than one session in a fresh scratch is unexpected — degrade rather
    than guess which transcript belongs to this run."""
    result = _run_hermes_with_proc(
        tmp_path,
        monkeypatch,
        _HermesProc(stdout="Done.\n"),
        envelope={**_HERMES_OK_ENVELOPE, "session_id": None},
        db_sessions=("a", "b"),
    )
    kinds = [event.kind for event in result.provider_events]
    assert TOOL_CALL_STARTED not in kinds
    assert result.outcome["terminal"] == "success"


def test_hermes_corrupt_state_db_degrades_to_bookends(tmp_path, monkeypatch) -> None:
    """Any sqlite error (alarm-killed WAL sidecars, torn writes) degrades the
    harvest quietly; the run outcome is unaffected."""
    result = _run_hermes_with_proc(tmp_path, monkeypatch, _HermesProc(stdout="Done.\n"), corrupt_db=True)
    kinds = [event.kind for event in result.provider_events]
    assert TOOL_CALL_STARTED not in kinds
    assert result.outcome["terminal"] == "success"


def test_hermes_incomplete_envelope_fails_loudly(tmp_path, monkeypatch) -> None:
    """completed != true with failed: false (partial/interrupted — constructible
    from oneshot's thinking-budget-exhausted path, rc 0 with an error banner as
    the reply) must not be recorded as terminal success."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    with pytest.raises(ProviderInvocationError, match="incomplete run") as excinfo:
        _run_hermes_with_proc(
            tmp_path,
            monkeypatch,
            _HermesProc(returncode=0, stdout="⚠️ Thinking Budget Exhausted — partial answer above"),
            envelope={**_HERMES_OK_ENVELOPE, "completed": False, "failed": False},
        )
    failed = excinfo.value.provider_events[-1]
    assert failed.payload["error_type"] == "EnvelopeNotCompleted"
    assert failed.payload["envelope_failed"] is False


def test_hermes_harvest_survives_special_char_working_paths(tmp_path) -> None:
    """SQLite URI filenames percent-decode %xx and stop at ?/# — the harvest
    must percent-encode the path (spiked: raw f-string URIs fail or misdirect
    for all three characters)."""
    from shepherd_dialect.providers.hermes import _harvest_hermes_session

    for odd in ("work%20space", "work#space", "work?space"):
        db_path = tmp_path / odd / "state.db"
        _write_hermes_state_db(db_path, ("s1",), {"s1": _HERMES_TOOL_ROWS})
        rows = _harvest_hermes_session(db_path, "s1")
        assert rows is not None, f"harvest failed under {odd!r}"
        assert len(rows) == len(_HERMES_TOOL_ROWS), f"harvest lost rows under {odd!r}"


def test_hermes_diagnosis_prefers_envelope_failure_key() -> None:
    """The envelope's structured `failure` (str(exc), class name included) is
    the classification source when present; reply text cannot spoof it."""
    from shepherd_dialect.providers.hermes import _diagnose_hermes_cli_failure

    structured = _diagnose_hermes_cli_failure(
        0,
        "I reviewed the code as asked.",
        "",
        model_provider="anthropic",
        envelope_failure="AuthenticationError: 401 invalid x-api-key",
    )
    assert structured.classification == "auth_failure"
    assert "AuthenticationError" in structured.summary


def test_hermes_diagnosis_does_not_misclassify_reply_prose() -> None:
    """Without a failure key, only the anchored first line classifies — prose
    that merely mentions authentication or 4xx codes stays unknown."""
    from shepherd_dialect.providers.hermes import _diagnose_hermes_cli_failure

    prose = _diagnose_hermes_cli_failure(
        0,
        "Reviewed the authentication module; 401 lines changed across http 4 handlers.",
        "",
        model_provider="anthropic",
    )
    assert prose.classification == "unknown"
    anchored = _diagnose_hermes_cli_failure(0, "HTTP 401: invalid x-api-key", "", model_provider="anthropic")
    assert anchored.classification == "auth_failure"
    assert "ANTHROPIC_API_KEY" in (anchored.remedy or "")


def test_hermes_probe_reports_instead_of_raising_on_empty_model(monkeypatch) -> None:
    """probe_hermes_auth documents 'never raises': an empty model is a failed
    probe verdict, not a ValueError escaping into the doctor command."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/hermes" if cmd == "hermes" else None)
    ok, detail = providers_module.probe_hermes_auth(model="", model_provider="anthropic")
    assert ok is False
    assert "model" in detail


def test_hermes_null_tool_ids_pair_started_with_completed() -> None:
    """Id-less calls (hermes's object-message persistence drops call ids) must
    still pair: the result row inherits the oldest open call's synthetic id and
    name instead of drifting apart on a shared counter."""
    from shepherd_dialect.providers.hermes import _provider_events_from_hermes_rows

    rows = (
        {"role": "assistant", "tool_calls": json.dumps([{"function": {"name": "write_file", "arguments": "{}"}}])},
        {"role": "tool", "content": "ok", "tool_call_id": None, "tool_name": None},
    )
    events = _provider_events_from_hermes_rows(
        rows, provider_id="hermes-headless", invocation_id="inv", model="m", sequence_start=1
    )
    assert [event.kind for event in events] == [TOOL_CALL_STARTED, TOOL_CALL_COMPLETED]
    started, completed = events
    assert started.tool_call_id == completed.tool_call_id
    assert completed.payload["canonical_tool_name"] == "write_file", "the pairing must recover the tool name"


def test_hermes_scrub_residue_fails_closed(tmp_path, monkeypatch) -> None:
    """A scrub that leaves .hermes-scratch behind must refuse the outcome —
    residue would ride the captured delta with the unredacted transcript."""
    from shepherd_dialect.provider_runtime import ProviderInvocationError

    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(ProviderInvocationError, match="scrub left residue") as excinfo:
        _run_hermes_with_proc(tmp_path, monkeypatch, _HermesProc(stdout="Done.\n"))
    assert excinfo.value.provider_events[-1].payload["error_type"] == "ScratchScrubResidue"


# --- Phase 1: Anthropic subscription seeding (execplan 260709 §subscription) --
#
# A signed-in Claude login is seeded access-token-only into the jail so a keyless
# hermes anthropic run authenticates — modeled on claude-headless, with the
# safety invariant that a jailed run can never rotate the host login.

_HOST_CLAUDE_BLOB = json.dumps(
    {"claudeAiOauth": {"accessToken": "sk-ant-oat01-HOSTLOGIN", "refreshToken": "rt-HOST", "expiresAt": 9999999999000}}
).encode()


def test_strip_refresh_capable_is_fail_closed_by_pattern() -> None:
    """The one shared safety primitive fails closed by *pattern*, not an exact key
    name: any refresh-named field (renamed/versioned included) and id_token are
    dropped, so a future schema tweak cannot smuggle a refresh-capable field past
    a `k != "refreshToken"` denylist. The access token and the fields a run needs
    survive."""
    from shepherd_dialect.providers.hermes import _strip_refresh_capable

    stripped = _strip_refresh_capable(
        {
            "access_token": "keep",
            "auth_type": "oauth",
            "base_url": "keep",
            "refresh_token": "SECRET",
            "refreshToken": "SECRET",
            "refreshTokenV2": "SECRET",  # renamed/versioned — still stripped
            "refresh_token_expires_at": 1,  # refresh-adjacent — still stripped
            "last_refresh": 1,  # benign telemetry — harmless to over-strip
            "id_token": "IDENTITY-JWT",  # host identity secret — not needed in the jail
        }
    )
    assert stripped == {"access_token": "keep", "auth_type": "oauth", "base_url": "keep"}
    assert not any("refresh" in k.lower() or k.lower() == "id_token" for k in stripped)


def test_hermes_access_token_only_strips_refresh() -> None:
    """The safety invariant: the seed carries the access token and NOT the
    refresh token — a jailed hermes with no refresh token cannot rotate the host."""
    from shepherd_dialect.providers.hermes import _access_token_only

    stripped = _access_token_only(_HOST_CLAUDE_BLOB)
    assert stripped is not None
    oauth = json.loads(stripped)["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-ant-oat01-HOSTLOGIN"
    assert "refreshToken" not in oauth, "the refresh token must never enter the seed"
    assert _access_token_only(b"not json") is None
    assert _access_token_only(json.dumps({"claudeAiOauth": {}}).encode()) is None  # no access token


def test_hermes_auth_status_reports_subscription_login(monkeypatch) -> None:
    """anthropic + no env key + a signed-in Claude login → subscription_login;
    env key still wins, and non-anthropic providers get no subscription path."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("SHEPHERD_NO_CREDENTIAL_SEEDING", raising=False)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(_HOST_CLAUDE_BLOB))

    status = hermes_auth_status("anthropic")
    assert (status.mode, status.ok) == ("subscription_login", True)
    assert "Claude login" in status.detail

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert hermes_auth_status("anthropic").mode == "env_key", "an env key still wins"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert hermes_auth_status("openrouter").ok is False, "no subscription path for non-anthropic in Phase 1"


def test_hermes_subscription_is_linux_gated_and_opt_out_aware(monkeypatch) -> None:
    """The access-token-only mitigation is only sound where the jail leaves no
    other credential source — Linux, and not when seeding is opted out."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Clear the opt-out too, so the darwin assertion below proves the *platform*
    # gate — not the opt-out branch (which would also return mode=None and let a
    # deleted platform gate pass green).
    monkeypatch.delenv("SHEPHERD_NO_CREDENTIAL_SEEDING", raising=False)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(_HOST_CLAUDE_BLOB))

    monkeypatch.setattr(sys, "platform", "darwin")
    assert hermes_auth_status("anthropic").mode is None, "macOS keychain is reachable — gated off"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("SHEPHERD_NO_CREDENTIAL_SEEDING", "1")
    assert hermes_auth_status("anthropic").mode is None, "opt-out disables seeding"


def test_hermes_seeds_subscription_access_token_only_and_never_mutates_host(tmp_path, monkeypatch) -> None:
    """The hard gate (execplan §subscription): a subscription run seeds the
    scratch access-token-only AND leaves the host credential byte-identical."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SHEPHERD_NO_CREDENTIAL_SEEDING", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/hermes" if cmd == "hermes" else None)

    # A real fake-host credential file, watched for mutation.
    host_cred = tmp_path / "host" / ".claude" / ".credentials.json"
    host_cred.parent.mkdir(parents=True)
    host_cred.write_bytes(_HOST_CLAUDE_BLOB)
    monkeypatch.setattr(claude_auth, "_read_host_claude_login", lambda: _found(host_cred.read_bytes()))

    seen = {}

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            cred = tmp_path / ".hermes-scratch" / "home" / ".claude" / ".credentials.json"
            seen["seeded"] = json.loads(cred.read_text()) if cred.is_file() else None
            (tmp_path / ".hermes-scratch" / "usage.json").write_text(json.dumps(_HERMES_OK_ENVELOPE))
            _write_hermes_state_db(tmp_path / ".hermes-scratch" / "hermes" / "state.db", ("20260709_000000_ok",))

            class _Proc:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return _Proc()

    provider = HermesHeadlessProvider(prompt="x", model="claude-haiku-4-5", model_provider="anthropic")
    provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())

    oauth = seen["seeded"]["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-ant-oat01-HOSTLOGIN", "the access token is seeded"
    assert "refreshToken" not in oauth, "the refresh token must NOT ride the seed"
    assert host_cred.read_bytes() == _HOST_CLAUDE_BLOB, "the host credential must be byte-identical — never mutated"
    assert not (tmp_path / ".hermes-scratch").exists(), "scratch scrubbed after the run"


# --- Phase 2: ChatGPT / hermes-native OAuth seeding (openai-codex) ------------
#
# A signed-in Hermes login (~/.hermes/auth.json credential_pool) is seeded
# access-token-only into the jail's HERMES_HOME. Real schema, spiked live
# against a ChatGPT subscription (auth proven, host store untouched).

_HOST_CODEX_AUTHSTORE = json.dumps(
    {
        "version": 1,
        "credential_pool": {
            "openai-codex": [
                {
                    "id": "c1",
                    "label": "openai-codex-oauth-1",
                    "auth_type": "oauth",
                    "access_token": "eyJ-FAKE-CODEX-JWT",
                    "refresh_token": "rt-CODEX-SECRET",
                    "id_token": "eyJ-FAKE-IDENTITY-JWT",
                    "base_url": "https://chatgpt.com/backend-api/codex",
                }
            ]
        },
        "active_provider": "openai-codex",
    }
).encode()


def _seed_host_hermes_authstore(tmp_path, monkeypatch, content=_HOST_CODEX_AUTHSTORE):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("SHEPHERD_NO_CREDENTIAL_SEEDING", raising=False)
    hh = tmp_path / "hermes-home"
    hh.mkdir(exist_ok=True)
    (hh / "auth.json").write_bytes(content)
    monkeypatch.setenv("HERMES_HOME", str(hh))
    return hh / "auth.json"


def test_hermes_openai_codex_is_a_supported_provider() -> None:
    """openai-codex (ChatGPT subscription) constructs — it authenticates via the
    seeded Hermes OAuth store, not an env key."""
    HermesHeadlessProvider(prompt="x", model="gpt-5.3-codex-spark", model_provider="openai-codex")


def test_hermes_authstore_seed_strips_refresh(tmp_path, monkeypatch) -> None:
    """The Phase 2 safety invariant: the seeded auth.json carries the access
    token (JWT) and NOT the refresh token."""
    from shepherd_dialect.providers.hermes import _host_hermes_oauth_authstore

    _seed_host_hermes_authstore(tmp_path, monkeypatch)
    seed = _host_hermes_oauth_authstore("openai-codex")
    assert seed is not None
    entry = json.loads(seed)["credential_pool"]["openai-codex"][0]
    assert entry["access_token"] == "eyJ-FAKE-CODEX-JWT"
    assert "refresh_token" not in entry, "the refresh token must never enter the seed"
    assert "id_token" not in entry, "the host identity JWT must not ride into the jail either"


def test_hermes_auth_status_reports_codex_subscription(tmp_path, monkeypatch) -> None:
    host_auth = _seed_host_hermes_authstore(tmp_path, monkeypatch)
    status = hermes_auth_status("openai-codex")
    assert (status.mode, status.ok) == ("subscription_login", True)
    assert "Hermes openai-codex login" in status.detail

    host_auth.unlink()  # no login → refuse with the actionable hint
    gone = hermes_auth_status("openai-codex")
    assert gone.mode is None
    assert "hermes auth add openai-codex" in gone.detail


def test_hermes_codex_is_linux_gated_and_opt_out_aware(tmp_path, monkeypatch) -> None:
    _seed_host_hermes_authstore(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert hermes_auth_status("openai-codex").mode is None, "gated off non-Linux"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("SHEPHERD_NO_CREDENTIAL_SEEDING", "1")
    assert hermes_auth_status("openai-codex").mode is None, "opt-out disables seeding"


def test_hermes_codex_seeds_authstore_and_never_mutates_host(tmp_path, monkeypatch) -> None:
    """The Phase 2 hard gate: a codex run seeds HERMES_HOME/auth.json
    access-token-only AND leaves the host auth.json byte-identical."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/fake/hermes" if cmd == "hermes" else None)
    host_auth = _seed_host_hermes_authstore(tmp_path, monkeypatch)
    seen = {}

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            seeded = tmp_path / ".hermes-scratch" / "hermes" / "auth.json"
            seen["seeded"] = json.loads(seeded.read_text()) if seeded.is_file() else None
            (tmp_path / ".hermes-scratch" / "usage.json").write_text(json.dumps(_HERMES_OK_ENVELOPE))
            _write_hermes_state_db(tmp_path / ".hermes-scratch" / "hermes" / "state.db", ("20260709_000000_ok",))

            class _Proc:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return _Proc()

    provider = HermesHeadlessProvider(prompt="x", model="gpt-5.3-codex-spark", model_provider="openai-codex")
    provider.execute(None, None, None, {}, execution=_Cap(), confinement=object())

    entry = seen["seeded"]["credential_pool"]["openai-codex"][0]
    assert entry["access_token"] == "eyJ-FAKE-CODEX-JWT", "the access token is seeded"
    assert "refresh_token" not in entry, "the refresh token must NOT ride the seed"
    assert host_auth.read_bytes() == _HOST_CODEX_AUTHSTORE, "the host auth.json must be byte-identical — never mutated"
