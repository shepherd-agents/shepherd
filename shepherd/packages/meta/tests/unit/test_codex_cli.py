from __future__ import annotations

import importlib

import shepherd_dialect
from click.testing import CliRunner
from shepherd_dialect.providers import CodexAuthStatus

codex_module = importlib.import_module("shepherd.cli.codex")


def test_api_key_login_uses_hidden_prompt_and_in_process_profile_api(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def login(profile: str, api_key: str) -> None:
        seen.update(profile=profile, api_key=api_key)

    monkeypatch.setattr(shepherd_dialect, "login_codex_api_key", login)

    result = CliRunner().invoke(
        codex_module.codex,
        ["login", "--profile", "api", "--mode", "api-key"],
        input="sk-private-fixture\n",
    )

    assert result.exit_code == 0
    assert seen == {"profile": "api", "api_key": "sk-private-fixture"}
    assert "sk-private-fixture" not in result.output


def test_status_probe_reports_named_subscription_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        shepherd_dialect,
        "codex_auth_status",
        lambda profile: CodexAuthStatus(True, profile, "chatgpt", "profile present", "0.144.4", True),
    )
    monkeypatch.setattr(
        shepherd_dialect,
        "probe_codex_auth",
        lambda profile: (True, f"profile {profile} account ready"),
    )

    result = CliRunner().invoke(codex_module.codex, ["status", "--profile", "release", "--probe"])

    assert result.exit_code == 0
    assert "profile present" in result.output
    assert "profile release account ready" in result.output
