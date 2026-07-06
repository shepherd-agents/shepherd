"""Keyless shape tests for the dialect's providers (W1 of the real-SDK demo plan).

The demo provider is never a CI gate (``deterministic-fake-v1-provider``) — what
CI pins is its *shape*: the negotiation discipline shared with the fake, the
S1-proven argv (`spikes/260610-real-sdk-jail-probe`, 5/5), and the dialect's
dependency posture (CLI-direct: no SDK package, no legacy ``shepherd_providers``
reach). Nothing here touches the network, the key, or the CLI.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from vcs_core.spi import ExecutionAuthorityRequired

from shepherd_dialect import ClaudeAgentProvider, DeterministicFakeProvider
from shepherd_dialect import providers as providers_module
from shepherd_dialect.providers import ClaudeHeadlessProvider, claude_auth_mode


@pytest.mark.parametrize("provider", [DeterministicFakeProvider(), ClaudeAgentProvider(prompt="x")])
def test_providers_refuse_without_execution_authority(provider) -> None:
    """Both bodies run only jailed — no capability/spec, no execution (fail-closed)."""
    with pytest.raises(ExecutionAuthorityRequired):
        provider.execute(None, None, None, {}, execution=None, confinement=None)


def test_command_argv_is_the_s1_shape(tmp_path) -> None:
    """Hard stop outermost, env redirect into the single writable root, body last."""
    provider = ClaudeAgentProvider(prompt="do the thing", max_turns=3, budget_seconds=90)
    argv = provider.command_argv(tmp_path, "/somewhere/claude")
    assert argv[0] == "/usr/bin/perl", "the alarm prefix must be outermost"
    assert "alarm" in argv[2]
    assert argv[3] == "90"
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
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SHEPHERD_NO_CREDENTIAL_SEEDING"):
        monkeypatch.delenv(var, raising=False)


def test_claude_auth_prefers_env_credentials(monkeypatch) -> None:
    """Env-carried credentials win; no host login is read."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(providers_module, "_read_host_claude_login", lambda: b"{}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert claude_auth_mode() == "api_key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert claude_auth_mode() == "oauth_token"


def test_claude_auth_uses_host_login_with_opt_out(monkeypatch) -> None:
    """Keyless + signed-in CLI → subscription seeding; opt-out env disables it."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(providers_module, "_read_host_claude_login", lambda: b"{}")
    assert claude_auth_mode() == "subscription_login"
    monkeypatch.setenv("SHEPHERD_NO_CREDENTIAL_SEEDING", "1")
    assert claude_auth_mode() is None


def test_claude_auth_none_without_any_credentials(monkeypatch) -> None:
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(providers_module, "_read_host_claude_login", lambda: None)
    assert claude_auth_mode() is None


def test_headless_execute_seeds_login_into_scratch_and_scrubs(tmp_path, monkeypatch) -> None:
    """Keyless launch seeds .credentials.json (0600) into the scratch config; scrub removes it."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(providers_module, "_read_host_claude_login", lambda: b'{"probe": true}')
    monkeypatch.setattr(providers_module.shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
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
        providers_module,
        "_read_host_claude_login",
        lambda: (_ for _ in ()).throw(AssertionError("host login must not be read")),
    )
    monkeypatch.setattr(providers_module.shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)
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
    assert argv[0] == "/usr/bin/perl"
    assert argv[3] == "240"


def test_headless_argv_passes_explicit_turn_cap(tmp_path) -> None:
    """An explicit ``max_turns`` opts into a hard turn cap via ``--max-turns``."""
    argv = ClaudeHeadlessProvider(prompt="do the thing", max_turns=8, budget_seconds=90).command_argv(
        tmp_path, "/somewhere/claude"
    )
    assert argv[argv.index("--max-turns") + 1] == "8"
    assert argv[3] == "90"


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


def _run_headless_with_proc(tmp_path, monkeypatch, proc):
    """Drive ClaudeHeadlessProvider.execute against a fake confined process."""
    _clear_claude_auth_env(monkeypatch)
    monkeypatch.setattr(providers_module, "_read_host_claude_login", lambda: None)
    monkeypatch.setattr(providers_module.shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)

    class _Cap:
        working_path = str(tmp_path)

        def launch_confined(self, command, confinement):
            return proc

    provider = ClaudeHeadlessProvider(prompt="x")
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
    monkeypatch.setattr(providers_module.shutil, "which", lambda cmd: "/fake/claude" if cmd == "claude" else None)

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


def test_headless_alarm_kill_maps_to_budget_exhausted(tmp_path, monkeypatch) -> None:
    """SIGALRM (rc -14) from the budget alarm is a trace-preserving Exhausted, not a refusal."""
    from shepherd_dialect.nucleus import BudgetExhausted

    class _Proc:
        returncode = -14
        stderr = ""
        stdout = ""

    with pytest.raises(BudgetExhausted, match="budget exceeded"):
        _run_headless_with_proc(tmp_path, monkeypatch, _Proc())


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
