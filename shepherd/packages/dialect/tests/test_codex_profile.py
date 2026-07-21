from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import pytest

pytest.importorskip("openai_codex", reason="install shepherd-dialect[codex] to run Codex profile tests")

from shepherd_dialect.providers.codex_profile import (
    adopt_existing_codex_login,
    codex_auth_status,
    login_codex_api_key,
    logout_codex_profile,
    resolve_codex_profile,
)


def test_existing_host_login_is_never_imported_implicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_root = tmp_path / "profiles"
    host_home = tmp_path / "host-codex"
    host_home.mkdir()
    host_auth = host_home / "auth.json"
    host_auth.write_text('{"access_token":"sk-host-fixture"}', encoding="utf-8")
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    monkeypatch.setenv("CODEX_HOME", str(host_home))

    assert codex_auth_status("default").ok is False
    assert not profile_root.exists()

    adopt_existing_codex_login("default")
    resolved = resolve_codex_profile("default")
    assert resolved.auth_path.is_symlink()
    assert resolved.auth_path.resolve() == host_auth.resolve()
    assert resolved.mode == "chatgpt"
    metadata = json.loads((resolved.profile_root / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "explicit_existing_login_link"
    assert "sk-host-fixture" not in json.dumps(metadata)

    logout_codex_profile("default")
    assert host_auth.read_text(encoding="utf-8") == '{"access_token":"sk-host-fixture"}'
    assert not (profile_root / "default").exists()


def test_api_key_login_uses_private_sdk_call_and_persists_only_mode_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openai_codex

    profile_root = tmp_path / "profiles"
    monkeypatch.setenv("SHEPHERD_CODEX_PROFILE_ROOT", str(profile_root))
    observed: dict[str, str] = {}

    class FakeConfig:
        def __init__(self, **kwargs: object) -> None:
            self.env = kwargs["env"]

    class FakeCodex:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def login_api_key(self, api_key: str) -> None:
            observed["key"] = api_key
            credential_home = Path(self.config.env["CODEX_HOME"])
            (credential_home / "auth.json").write_text('{"fixture":"opaque-sdk-state"}', encoding="utf-8")

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    monkeypatch.setattr(openai_codex, "CodexConfig", FakeConfig)
    synthetic = "sk-synthetic-never-durable"
    login_codex_api_key("ci", synthetic)

    assert observed == {"key": synthetic}
    resolved = resolve_codex_profile("ci")
    assert resolved.mode == "api_key"
    durable = "\n".join(
        path.read_text(encoding="utf-8")
        for path in resolved.profile_root.rglob("*")
        if path.is_file() and path.name != "lock"
    )
    assert synthetic not in durable
    assert json.loads((resolved.profile_root / "metadata.json").read_text(encoding="utf-8"))["mode"] == "api_key"
