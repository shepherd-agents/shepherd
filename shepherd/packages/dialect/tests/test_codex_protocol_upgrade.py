"""Upgrade alarms for behavior the raw-ingress substrate must not trust."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("openai_codex", reason="install shepherd-dialect[codex] to run Codex protocol tests")

from openai_codex.client import CodexClient, CodexConfig  # type: ignore[import-not-found]

from shepherd_dialect.providers.codex_protocol import CodexProtocolError, write_codex_permission_config

FAKE_APP_SERVER = Path(__file__).with_name("support") / "fake_codex_app_server.py"


@pytest.mark.xfail(
    reason=(
        "openai-codex 0.144.4 can drop notifications emitted between the turn/start response and "
        "turn-queue registration; Shepherd captures stdout before this router"
    ),
    strict=False,
)
def test_sdk_queue_race_remains_a_pinned_upgrade_alarm(tmp_path: Path) -> None:
    config = CodexConfig(
        launch_args_override=(sys.executable, "-B", str(FAKE_APP_SERVER)),
        cwd=str(tmp_path),
        env={"SHEPHERD_CODEX_SPIKE_SCENARIO": "all-events"},
    )
    client = CodexClient(config=config)
    client.start()
    try:
        client.initialize()
        register_turn_notifications = client.register_turn_notifications

        def delayed_register(turn_id: str) -> None:
            time.sleep(0.1)
            register_turn_notifications(turn_id)

        client.register_turn_notifications = delayed_register  # type: ignore[method-assign]
        turn = client.turn_start("thread-spike", "race fixture")
        assert client._router._turn_notifications[turn.turn.id].qsize() == 13
    finally:
        client.close()


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "localhost",
        "service.localhost",
        "metadata.google.internal",
        "example..com",
        "-example.com",
        "example.com-",
    ],
)
def test_permission_profile_refuses_unsafe_or_non_domain_network_hosts(tmp_path: Path, host: str) -> None:
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    codex_home.mkdir()
    workspace.mkdir()

    with pytest.raises(CodexProtocolError, match="network host"):
        write_codex_permission_config(
            codex_home=codex_home,
            workspace=workspace,
            credential_paths=(),
            writable_roots=(workspace,),
            network_mode="broker",
            allowed_hosts=(host,),
        )


def test_permission_profile_normalizes_one_exact_domain_allowlist(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    codex_home.mkdir()
    workspace.mkdir()

    write_codex_permission_config(
        codex_home=codex_home,
        workspace=workspace,
        credential_paths=(),
        writable_roots=(workspace,),
        network_mode="broker",
        allowed_hosts=("API.Example.COM.",),
    )

    config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert '"api.example.com" = "allow"' in config
    assert "API.Example.COM" not in config
