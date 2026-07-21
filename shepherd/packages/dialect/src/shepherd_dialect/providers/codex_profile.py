"""Shepherd-owned ChatGPT subscription and API-key profiles for Codex."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

CODEX_TESTED_VERSION = "0.144.4"
CODEX_REAUDIT_ON_BUMP = "protocol, auth storage, permission profiles, event registry, and bundled runtime"

_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CodexProfileError(RuntimeError):
    """Raised when a Codex authentication profile is missing or unusable."""


@dataclass(frozen=True)
class CodexAuthStatus:
    """Non-secret readiness result for one named authentication profile."""

    ok: bool
    profile_id: str
    mode: str
    detail: str
    sdk_version: str | None
    runtime_compatible: bool


@dataclass(frozen=True)
class ResolvedCodexProfile:
    """Trusted profile resolution used only by the provider broker."""

    profile_id: str
    profile_root: Path
    credential_home: Path
    auth_path: Path
    mode: str


def codex_profile_root() -> Path:
    """Return the non-workspace state root for named Codex profiles."""
    configured = os.environ.get("SHEPHERD_CODEX_PROFILE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "shepherd" / "provider-profiles" / "codex").resolve()


def resolve_codex_profile(profile_id: str = "default") -> ResolvedCodexProfile:
    """Resolve one logged-in profile without reading credential bytes."""
    profile = _profile_path(profile_id)
    credential_home = profile / "credential"
    auth_path = credential_home / "auth.json"
    metadata_path = profile / "metadata.json"
    if not metadata_path.is_file() or not auth_path.is_file():
        raise CodexProfileError(
            f"Codex profile {profile_id!r} is not logged in; run `shepherd codex login --profile {profile_id}`"
        )
    try:
        stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexProfileError(f"Codex profile {profile_id!r} metadata is corrupt") from exc
    mode = stored.get("mode") if isinstance(stored, dict) else None
    if mode not in {"chatgpt", "api_key"}:
        raise CodexProfileError(f"Codex profile {profile_id!r} has an unsupported authentication mode")
    return ResolvedCodexProfile(
        profile_id=profile_id,
        profile_root=profile,
        credential_home=credential_home,
        auth_path=auth_path,
        mode=mode,
    )


def codex_auth_status(profile_id: str = "default") -> CodexAuthStatus:
    """Inspect dependency compatibility and offline profile presence."""
    version = _sdk_version()
    compatible = version == CODEX_TESTED_VERSION
    try:
        profile = resolve_codex_profile(profile_id)
    except CodexProfileError as exc:
        return CodexAuthStatus(False, profile_id, "chatgpt", str(exc), version, compatible)
    if version is None:
        return CodexAuthStatus(
            False,
            profile_id,
            profile.mode,
            "openai-codex is not installed; install shepherd-dialect[codex]",
            None,
            False,
        )
    if not compatible:
        return CodexAuthStatus(
            False,
            profile_id,
            profile.mode,
            f"openai-codex {version} differs from tested {CODEX_TESTED_VERSION}; re-audit required",
            version,
            False,
        )
    detail = "subscription profile is present" if profile.mode == "chatgpt" else "API-key profile is present"
    return CodexAuthStatus(True, profile_id, profile.mode, detail, version, True)


def probe_codex_auth(profile_id: str = "default") -> tuple[bool, str]:
    """Read app-server account state without starting a model turn."""
    profile = resolve_codex_profile(profile_id)
    _require_compatible_sdk()
    from openai_codex import Codex, CodexConfig  # type: ignore[import-not-found]

    with (
        codex_profile_lock(profile_id),
        Codex(
            CodexConfig(
                cwd=str(profile.profile_root),
                env={
                    "CODEX_HOME": str(profile.credential_home),
                    "HOME": str(profile.profile_root / "home"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                config_overrides=('cli_auth_credentials_store="file"',),
            )
        ) as codex,
    ):
        account = codex.account(refresh_token=True)
    root = getattr(getattr(account, "account", None), "root", None)
    account_type = getattr(root, "type", None)
    account_type = getattr(account_type, "value", account_type)
    if profile.mode == "chatgpt" and account_type == "apiKey":
        return False, "profile metadata says ChatGPT but app-server reports API-key auth"
    if profile.mode == "api_key" and account_type != "apiKey":
        return False, "profile metadata says API key but app-server reports another auth mode"
    if root is None:
        return False, "app-server reports no authenticated account"
    if profile.mode == "api_key":
        return True, "API-key account is ready"
    plan = getattr(root, "plan_type", None)
    plan_value = getattr(plan, "value", plan)
    suffix = f" ({plan_value} plan)" if isinstance(plan_value, str) and plan_value else ""
    return True, f"ChatGPT subscription account is ready{suffix}"


def login_codex_chatgpt(
    profile_id: str = "default",
    *,
    on_device_code: Callable[[str, str], None] | None = None,
) -> None:
    """Create or replace one profile using Codex's device-code login flow."""
    _require_compatible_sdk()
    from openai_codex import Codex, CodexConfig  # type: ignore[import-not-found]

    profile = _prepare_profile(profile_id)
    credential_home = profile / "credential"
    home = profile / "home"
    with codex_profile_lock(profile_id):
        _reset_profile_auth(credential_home)
        with Codex(
            CodexConfig(
                cwd=str(profile),
                env={
                    "CODEX_HOME": str(credential_home),
                    "HOME": str(home),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                config_overrides=('cli_auth_credentials_store="file"',),
            )
        ) as codex:
            handle = codex.login_chatgpt_device_code()
            if on_device_code is not None:
                on_device_code(handle.verification_url, handle.user_code)
            completed = handle.wait()
            success = getattr(completed, "success", None)
            if success is False:
                raise CodexProfileError("ChatGPT device-code login was not completed")
        if not (credential_home / "auth.json").is_file():
            raise CodexProfileError("Codex completed login without creating file-backed account state")
        _write_metadata(profile, mode="chatgpt", source="device_code")


def login_codex_api_key(profile_id: str, api_key: str) -> None:
    """Create or replace one profile using private in-process API-key login."""
    if not isinstance(api_key, str) or not api_key:
        raise CodexProfileError("API key must be a non-empty string")
    _require_compatible_sdk()
    from openai_codex import Codex, CodexConfig  # type: ignore[import-not-found]

    profile = _prepare_profile(profile_id)
    credential_home = profile / "credential"
    home = profile / "home"
    with codex_profile_lock(profile_id):
        _reset_profile_auth(credential_home)
        with Codex(
            CodexConfig(
                cwd=str(profile),
                env={
                    "CODEX_HOME": str(credential_home),
                    "HOME": str(home),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                config_overrides=('cli_auth_credentials_store="file"',),
            )
        ) as codex:
            codex.login_api_key(api_key)
    if not (credential_home / "auth.json").is_file():
        raise CodexProfileError("Codex completed API-key login without creating file-backed account state")
    _write_metadata(profile, mode="api_key", source="private_prompt")


def adopt_existing_codex_login(profile_id: str = "default", *, source_home: Path | None = None) -> None:
    """Explicitly link an existing Codex ChatGPT login into a Shepherd profile.

    This operation is intentionally explicit.  Provider execution never scans
    or imports ``~/.codex`` on its own, and no token bytes are copied.
    """
    source = (source_home or _host_codex_home()).expanduser().resolve()
    source_auth = source / "auth.json"
    if not source_auth.is_file():
        raise CodexProfileError(f"existing Codex login not found under {source}")
    profile = _prepare_profile(profile_id)
    target = profile / "credential" / "auth.json"
    with codex_profile_lock(profile_id):
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(source_auth)
        _write_metadata(profile, mode="chatgpt", source="explicit_existing_login_link")


def logout_codex_profile(profile_id: str = "default") -> None:
    """Remove only the selected Shepherd profile; linked source auth survives."""
    profile = _profile_path(profile_id)
    with codex_profile_lock(profile_id):
        if profile.exists():
            shutil.rmtree(profile)


@contextmanager
def codex_profile_lock(profile_id: str) -> Iterator[None]:
    """Serialize login, refresh, run, and logout for one profile."""
    profile = _profile_path(profile_id)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    lock_path = profile / "lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    lock_path.chmod(0o600)
    if os.name == "posix":
        import fcntl

        with lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    lock = _thread_lock_for(profile_id)
    with lock:
        yield


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(profile_id: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(profile_id, threading.Lock())


def _prepare_profile(profile_id: str) -> Path:
    profile = _profile_path(profile_id)
    credential = profile / "credential"
    home = profile / "home"
    for path in (profile, credential, home):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    return profile


def _reset_profile_auth(credential_home: Path) -> None:
    auth_path = credential_home / "auth.json"
    if auth_path.exists() or auth_path.is_symlink():
        auth_path.unlink()


def _profile_path(profile_id: str) -> Path:
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise CodexProfileError("Codex profile id must use 1-64 letters, digits, dots, underscores, or hyphens")
    return codex_profile_root() / profile_id


def _write_metadata(profile: Path, *, mode: str, source: str) -> None:
    path = profile / "metadata.json"
    path.write_text(
        json.dumps(
            {
                "schema": "shepherd.codex_profile.v1",
                "mode": mode,
                "source": source,
                "sdk_version": CODEX_TESTED_VERSION,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _host_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _sdk_version() -> str | None:
    try:
        return metadata.version("openai-codex")
    except metadata.PackageNotFoundError:
        return None


def _require_compatible_sdk() -> None:
    version = _sdk_version()
    if version is None:
        raise CodexProfileError("openai-codex is not installed; install shepherd-dialect[codex]")
    if version != CODEX_TESTED_VERSION:
        raise CodexProfileError(
            f"openai-codex {version} differs from tested {CODEX_TESTED_VERSION}; re-audit {CODEX_REAUDIT_ON_BUMP}"
        )


__all__ = [
    "CODEX_REAUDIT_ON_BUMP",
    "CODEX_TESTED_VERSION",
    "CodexAuthStatus",
    "CodexProfileError",
    "ResolvedCodexProfile",
    "adopt_existing_codex_login",
    "codex_auth_status",
    "codex_profile_lock",
    "codex_profile_root",
    "login_codex_api_key",
    "login_codex_chatgpt",
    "logout_codex_profile",
    "probe_codex_auth",
    "resolve_codex_profile",
]
