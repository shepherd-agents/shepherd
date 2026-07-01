"""Tests for config loading and layered merge."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vcs_core.config import SecretRef, VcsCoreConfig, _deep_merge, _merge_layers, load_config

if TYPE_CHECKING:
    from pathlib import Path


def test_load_config_empty_workspace(tmp_path: Path) -> None:
    config = load_config(str(tmp_path))
    assert config.min_version == 1
    assert config.bindings == {}


def test_load_config_project_only(tmp_path: Path) -> None:
    project_toml = tmp_path / "vcscore.toml"
    project_toml.write_text('[bindings.git]\ntype = "git"\ncommands = ["git"]\n')
    config = load_config(str(tmp_path))
    assert "git" in config.bindings
    assert config.bindings["git"].binding_options()["commands"] == ["git"]


def test_load_config_repo_only(tmp_path: Path) -> None:
    repo_dir = tmp_path / ".vcscore"
    repo_dir.mkdir()
    repo_toml = repo_dir / "config.toml"
    repo_toml.write_text('[defaults]\ndevice = "container"\n')
    config = load_config(str(tmp_path))
    assert config.defaults.device == "container"


def test_load_config_all_layers(tmp_path: Path) -> None:
    # Project config
    project_toml = tmp_path / "vcscore.toml"
    project_toml.write_text(
        '[bindings.http]\ntype = "http"\nintercept = ["api.github.com/*"]\ndefault-mode = "intercept"\n'
    )
    # Repo config (overrides default-mode)
    repo_dir = tmp_path / ".vcscore"
    repo_dir.mkdir()
    repo_toml = repo_dir / "config.toml"
    repo_toml.write_text('[bindings.http]\ndefault-mode = "observe"\n')

    config = load_config(str(tmp_path))
    assert config.bindings["http"].binding_options()["intercept"] == ["api.github.com/*"]
    assert config.bindings["http"].binding_options()["default-mode"] == "observe"


def test_key_level_merge_preserves_other_keys() -> None:
    base = {"bindings": {"http": {"type": "http", "intercept": ["a"], "mode": "intercept"}}}
    override = {"bindings": {"http": {"mode": "observe"}}}
    _deep_merge(base, override)
    assert base["bindings"]["http"]["intercept"] == ["a"]
    assert base["bindings"]["http"]["mode"] == "observe"


def test_key_level_merge_higher_layer_wins() -> None:
    layers = [
        {"bindings": {"pg": {"type": "sqlite", "dsn": "a", "level": "read"}}},
        {"bindings": {"pg": {"level": "serializable"}}},
    ]
    result = _merge_layers(layers)
    assert result["bindings"]["pg"]["dsn"] == "a"
    assert result["bindings"]["pg"]["level"] == "serializable"


def test_secret_ref_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_SECRET", "my-secret-value")
    ref = SecretRef(env="TEST_SECRET")
    assert ref.resolve() == "my-secret-value"


def test_secret_ref_resolution_failure() -> None:
    from vcs_core._errors import SubstrateNotBoundError

    ref = SecretRef(env="DEFINITELY_NOT_SET_12345")
    with pytest.raises(SubstrateNotBoundError, match="DEFINITELY_NOT_SET_12345"):
        ref.resolve()


def test_cli_overrides_highest_priority(tmp_path: Path) -> None:
    project_toml = tmp_path / "vcscore.toml"
    project_toml.write_text('[defaults]\ndevice = "local"\n')
    config = load_config(str(tmp_path), cli_overrides={"defaults": {"device": "container"}})
    assert config.defaults.device == "container"


def test_config_model_validation() -> None:
    config = VcsCoreConfig(min_version=1, bindings={"test": {"type": "sqlite", "key": "value"}})
    assert config.bindings["test"].type == "sqlite"
    assert config.bindings["test"].binding_options()["key"] == "value"
