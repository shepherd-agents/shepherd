"""`shepherd doctor claude` honesty checks.

The offline `claude-auth` check must predict a working jailed run rather than
report "a credential blob is readable": an expired subscription login is a hard
fail, and `--probe` appends the authoritative, network-reaching verdict.
"""

from __future__ import annotations

import importlib

import shepherd_dialect
from shepherd_dialect.providers import ClaudeAuthStatus, CodexAuthStatus

# `from shepherd.cli import doctor` (and `import shepherd.cli.doctor as ...`) both
# resolve to the click Group the package re-exports, not the module — reach the
# real module through the import system.
doctor_module = importlib.import_module("shepherd.cli.doctor")


def _find(checks, name):
    return next(c for c in checks if c["name"] == name)


def test_claude_check_hard_fails_on_expired_login(monkeypatch) -> None:
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(
        shepherd_dialect,
        "claude_auth_status",
        lambda: ClaudeAuthStatus("subscription_login", False, "access token is expired"),
    )
    monkeypatch.setattr(doctor_module.shutil, "which", lambda cmd: "/fake/claude")

    checks = doctor_module._claude_checks()

    auth = _find(checks, "claude-auth")
    assert auth["ok"] is False
    assert "expired" in auth["detail"]
    # no probe unless explicitly requested
    assert all(c["name"] != "claude-auth-probe" for c in checks)


def test_claude_check_valid_login_is_green_but_unverified(monkeypatch) -> None:
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(
        shepherd_dialect,
        "claude_auth_status",
        lambda: ClaudeAuthStatus("subscription_login", True, "found, not verified — run ... --probe"),
    )
    monkeypatch.setattr(doctor_module.shutil, "which", lambda cmd: "/fake/claude")

    auth = _find(doctor_module._claude_checks(), "claude-auth")
    assert auth["ok"] is True
    assert "not verified" in auth["detail"]


def test_probe_flag_appends_authoritative_verdict(monkeypatch) -> None:
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(
        shepherd_dialect,
        "claude_auth_status",
        lambda: ClaudeAuthStatus("subscription_login", True, "found, not verified"),
    )
    monkeypatch.setattr(shepherd_dialect, "probe_claude_auth", lambda: (False, "Not logged in · Please run /login"))
    monkeypatch.setattr(doctor_module.shutil, "which", lambda cmd: "/fake/claude")

    checks = doctor_module._claude_checks(probe=True)

    probe = _find(checks, "claude-auth-probe")
    assert probe["ok"] is False
    assert "Not logged in" in probe["detail"]


def test_probe_flag_skips_cleanly_without_cli(monkeypatch) -> None:
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(
        shepherd_dialect,
        "claude_auth_status",
        lambda: ClaudeAuthStatus(None, False, "no credentials"),
    )
    monkeypatch.setattr(doctor_module.shutil, "which", lambda cmd: None)

    checks = doctor_module._claude_checks(probe=True)

    probe = _find(checks, "claude-auth-probe")
    assert probe["ok"] is False
    assert "skipped" in probe["detail"]


def test_codex_doctor_checks_pinned_sdk_profile_and_no_model_probe(monkeypatch) -> None:
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(
        shepherd_dialect,
        "codex_auth_status",
        lambda profile: CodexAuthStatus(True, profile, "chatgpt", "subscription profile is present", "0.144.4", True),
    )
    seen: list[str] = []

    def probe(profile: str) -> tuple[bool, str]:
        seen.append(profile)
        return True, "ChatGPT subscription account is ready (plus plan)"

    monkeypatch.setattr(shepherd_dialect, "probe_codex_auth", probe)

    checks = doctor_module._codex_checks(profile="release", probe=True)

    assert _find(checks, "codex-sdk")["ok"] is True
    assert _find(checks, "codex-auth")["ok"] is True
    assert _find(checks, "codex-auth-probe")["ok"] is True
    assert seen == ["release"]


def test_codex_doctor_fails_closed_on_version_drift(monkeypatch) -> None:
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(
        shepherd_dialect,
        "codex_auth_status",
        lambda profile: CodexAuthStatus(
            False,
            profile,
            "api_key",
            "openai-codex 0.145.0 differs from tested 0.144.4",
            "0.145.0",
            False,
        ),
    )

    checks = doctor_module._codex_checks(profile="api")

    assert _find(checks, "codex-sdk")["ok"] is False
    assert _find(checks, "codex-auth")["ok"] is False


# --- hermes lane (execplan 260709 r5 §S2) ------------------------------------
#
# The hermes lane has no account default: model selection and auth routing are
# explicit, so missing --provider/--model are red checks naming the flag, not
# crashes. The version pin is warn-only (optional) so upstream's fortnightly
# cadence never gates readiness.

from click.testing import CliRunner
from shepherd_dialect.providers import HermesAuthStatus


def _arm_hermes_lane(monkeypatch, *, which="/fake/hermes", version="0.18.2") -> None:
    """The three patches every hermes-lane test needs; override per test."""
    monkeypatch.setattr(shepherd_dialect, "native_jail_available", lambda: True)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda cmd: which)
    monkeypatch.setattr(doctor_module, "_hermes_version", lambda cli_path: version)


def test_hermes_auth_check_names_the_missing_provider_flag(monkeypatch) -> None:
    _arm_hermes_lane(monkeypatch)

    checks = doctor_module._hermes_checks(model=None, model_provider=None)

    auth = _find(checks, "hermes-auth")
    assert auth["ok"] is False
    assert "--provider" in auth["detail"]
    assert "anthropic" in auth["detail"], "the red check must name the supported set"
    assert all(c["name"] != "hermes-auth-probe" for c in checks), "no probe unless requested"


def test_hermes_auth_check_resolves_env_key_for_the_provider(monkeypatch) -> None:
    _arm_hermes_lane(monkeypatch)
    monkeypatch.setattr(
        shepherd_dialect,
        "hermes_auth_status",
        lambda provider: HermesAuthStatus("env_key", True, "ANTHROPIC_API_KEY set"),
    )

    auth = _find(doctor_module._hermes_checks(model=None, model_provider="anthropic"), "hermes-auth")
    assert auth["ok"] is True
    assert "ANTHROPIC_API_KEY" in auth["detail"]


def test_hermes_arguments_are_normalized_at_the_boundary(monkeypatch) -> None:
    """`--provider Anthropic ` means anthropic — the workspace router strips and
    lowercases, and 'unsupported model_provider' for a supported one is a lie."""
    _arm_hermes_lane(monkeypatch)
    seen = {}

    def _fake_status(provider):
        seen["provider"] = provider
        return HermesAuthStatus("env_key", True, "ANTHROPIC_API_KEY set")

    def _fake_probe(*, model, model_provider):
        seen["probe_args"] = (model, model_provider)
        return True, "authenticated"

    monkeypatch.setattr(shepherd_dialect, "hermes_auth_status", _fake_status)
    monkeypatch.setattr(shepherd_dialect, "probe_hermes_auth", _fake_probe)

    doctor_module._hermes_checks(model="  claude-haiku-4-5 ", model_provider=" Anthropic ", probe=True)

    assert seen["provider"] == "anthropic"
    assert seen["probe_args"] == ("claude-haiku-4-5", "anthropic")


def test_hermes_version_pin_is_warn_only(monkeypatch) -> None:
    """A drifted version is a visible warning naming the re-audit list, but it
    is optional — it must never gate readiness."""
    _arm_hermes_lane(monkeypatch, version="0.19.0")

    version = _find(doctor_module._hermes_checks(model=None, model_provider=None), "hermes-version")
    assert version["ok"] is False
    assert version["required"] is False, "warn-only: a drifted lane must not gate"
    assert "0.18.2" in version["detail"], "the tested version must be named"
    assert "oneshot.py" in version["detail"], "the re-audit list must be named"


def test_hermes_version_ignores_this_venv_for_a_foreign_path_cli(monkeypatch, tmp_path) -> None:
    """In-process metadata is trusted only when the PATH CLI lives under this
    interpreter's prefix — otherwise a hermes-agent installed in shepherd's own
    venv would green-stamp a different install (the drift case the pin exists
    to catch). A foreign CLI resolves through its shebang interpreter."""
    import importlib.metadata as importlib_metadata

    monkeypatch.setattr(importlib_metadata, "version", lambda name: "0.0.0-wrong-venv")
    cli = tmp_path / "hermes"  # tmp_path is never under sys.prefix
    cli.write_text("#!/usr/bin/env python3\n# console script\n")
    seen = {}

    class _Proc:
        stdout = "9.9.9\n"

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Proc()

    import subprocess as _subprocess

    # the function imports subprocess lazily, so the global module is the seam
    monkeypatch.setattr(_subprocess, "run", _fake_run)

    assert doctor_module._hermes_version(str(cli)) == "9.9.9"
    assert seen["argv"][0] == "python3", "env-style shebangs must be token-split and `env` dropped"


def test_hermes_version_refuses_non_python_launchers(monkeypatch, tmp_path) -> None:
    """A pip sh-wrapper launcher cannot run the metadata one-liner — the check
    degrades to unknown instead of handing a python payload to /bin/sh."""
    import subprocess as _subprocess

    def _must_not_run(argv, **kwargs):
        raise AssertionError(f"no subprocess for a non-python shebang: {argv}")

    monkeypatch.setattr(_subprocess, "run", _must_not_run)
    cli = tmp_path / "hermes"
    cli.write_text('#!/bin/sh\nexec /usr/bin/real-hermes "$@"\n')

    assert doctor_module._hermes_version(str(cli)) is None


def test_hermes_probe_skips_and_names_missing_arguments(monkeypatch) -> None:
    _arm_hermes_lane(monkeypatch)

    def _probe_must_not_run(**kwargs):
        raise AssertionError("probe must not run without both arguments")

    monkeypatch.setattr(shepherd_dialect, "probe_hermes_auth", _probe_must_not_run)

    probe = _find(doctor_module._hermes_checks(model=None, model_provider="anthropic", probe=True), "hermes-auth-probe")
    assert probe["ok"] is False
    assert "--model" in probe["detail"]

    probe = _find(doctor_module._hermes_checks(model=None, model_provider=None, probe=True), "hermes-auth-probe")
    assert "--model and --provider" in probe["detail"]


def test_hermes_probe_flag_appends_authoritative_verdict(monkeypatch) -> None:
    _arm_hermes_lane(monkeypatch)
    monkeypatch.setattr(
        shepherd_dialect,
        "hermes_auth_status",
        lambda provider: HermesAuthStatus("env_key", True, "ANTHROPIC_API_KEY set"),
    )
    seen = {}

    def _fake_probe(*, model, model_provider):
        seen["args"] = (model, model_provider)
        return True, "authenticated (anthropic via env key)"

    monkeypatch.setattr(shepherd_dialect, "probe_hermes_auth", _fake_probe)

    checks = doctor_module._hermes_checks(model="claude-haiku-4-5", model_provider="anthropic", probe=True)

    probe = _find(checks, "hermes-auth-probe")
    assert probe["ok"] is True
    assert "authenticated" in probe["detail"]
    assert seen["args"] == ("claude-haiku-4-5", "anthropic")


def test_hermes_probe_skips_cleanly_without_cli(monkeypatch) -> None:
    _arm_hermes_lane(monkeypatch, which=None)
    monkeypatch.setattr(
        shepherd_dialect,
        "hermes_auth_status",
        lambda provider: HermesAuthStatus(None, False, "ANTHROPIC_API_KEY is not set"),
    )

    checks = doctor_module._hermes_checks(model="m", model_provider="anthropic", probe=True)

    probe = _find(checks, "hermes-auth-probe")
    assert probe["ok"] is False
    assert "skipped" in probe["detail"]
    assert all(c["name"] != "hermes-version" for c in checks), "no version check without a CLI"


def test_hermes_help_advertises_the_supported_provider_set() -> None:
    """The --provider help text hand-copies the set (a module-level dialect
    import would break doctor's degrade-gracefully posture) — this pin makes
    the copy drift-proof."""
    from shepherd_dialect import HERMES_SUPPORTED_MODEL_PROVIDERS

    result = CliRunner().invoke(doctor_module.doctor, ["hermes", "--help"])
    assert result.exit_code == 0
    for provider in HERMES_SUPPORTED_MODEL_PROVIDERS:
        assert provider in result.output, f"--help must advertise {provider!r}"


def test_doctor_group_rejects_options_placed_before_the_subcommand() -> None:
    """`doctor --json hermes` used to parse then silently drop --json — a CI
    script would pipe human output to jq. Refuse loudly instead."""
    result = CliRunner().invoke(doctor_module.doctor, ["--json", "hermes", "--provider", "anthropic"])
    assert result.exit_code != 0
    assert "after the subcommand" in result.output
