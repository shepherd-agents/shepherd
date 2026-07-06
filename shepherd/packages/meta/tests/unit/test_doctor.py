"""`shepherd doctor claude` honesty checks.

The offline `claude-auth` check must predict a working jailed run rather than
report "a credential blob is readable": an expired subscription login is a hard
fail, and `--probe` appends the authoritative, network-reaching verdict.
"""

from __future__ import annotations

import importlib

import shepherd_dialect
from shepherd_dialect.providers import ClaudeAuthStatus

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
