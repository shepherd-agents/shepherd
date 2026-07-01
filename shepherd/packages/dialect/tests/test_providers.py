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
from shepherd_dialect.providers import ClaudeHeadlessProvider


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
