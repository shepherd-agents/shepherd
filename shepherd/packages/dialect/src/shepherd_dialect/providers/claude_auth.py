"""Claude lane authentication: env keys, host subscription login, offline verdicts.

The network-reaching counterpart, ``probe_claude_auth``, lives with the headless
provider it drives (``claude_headless``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def claude_auth_mode() -> str | None:
    """Return how the jailed Claude lane can authenticate on this host, or ``None``.

    - ``"api_key"``: ``ANTHROPIC_API_KEY`` is set (passes through the jail's env block).
    - ``"oauth_token"``: ``CLAUDE_CODE_OAUTH_TOKEN`` is set (same passthrough).
    - ``"subscription_login"``: the host's ``claude`` CLI is signed in and credential
      seeding is enabled; the provider copies the login into the scrubbed scratch
      config at launch. Set ``SHEPHERD_NO_CREDENTIAL_SEEDING=1`` to disable.
    """
    mode, _ = _resolve_claude_auth()
    return mode


@dataclass(frozen=True)
class _HostLoginLookup:
    """The result of looking for a host ``claude`` login, with a non-secret trail.

    ``blob`` is the credential bytes (or ``None``); ``attempts`` records each source
    tried as ``(source_class, status)`` — never a path, never credential bytes — so
    a failed resolution can say *which* source class failed and roughly how
    (missing vs unreadable vs keychain-denied/timeout) without leaking secrets.
    """

    blob: bytes | None
    attempts: tuple[tuple[str, str], ...] = ()

    @property
    def source(self) -> str | None:
        """The source class the credential was found in, if any."""
        for source, status in self.attempts:
            if status.endswith("_found"):
                return source
        return None


@dataclass(frozen=True)
class _ClaudeAuthResolution:
    """How the jailed Claude lane would authenticate, with a diagnostic trail.

    Extends the ``(mode, blob)`` pair the run path consumes with a non-secret
    ``status`` and source ``attempts`` so ``doctor``/``probe`` can explain a
    ``mode is None`` verdict (seeding disabled? keychain denied? nothing found?)
    instead of a flat "no credentials".
    """

    mode: str | None
    blob: bytes | None = None
    source: str | None = None
    status: str = "unknown"
    seeding_disabled: bool = False
    attempts: tuple[tuple[str, str], ...] = ()


def _resolve_claude_auth() -> tuple[str | None, bytes | None]:
    """Return ``(auth_mode, login_blob)``; the blob is set only when seeding applies."""
    resolution = _resolve_claude_auth_diagnostic()
    return resolution.mode, resolution.blob


def _resolve_claude_auth_diagnostic() -> _ClaudeAuthResolution:
    """Resolve Claude auth with a non-secret diagnostic trail (see ``_ClaudeAuthResolution``)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _ClaudeAuthResolution("api_key", None, "env_api_key", "env_api_key")
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return _ClaudeAuthResolution("oauth_token", None, "env_oauth_token", "env_oauth_token")
    if os.environ.get("SHEPHERD_NO_CREDENTIAL_SEEDING"):
        return _ClaudeAuthResolution(None, None, None, "seeding_disabled", seeding_disabled=True)
    lookup = _read_host_claude_login()
    if lookup.blob is not None:
        won = next((s for s in lookup.attempts if s[1].endswith("_found")), ("host_login", "found"))
        return _ClaudeAuthResolution("subscription_login", lookup.blob, won[0], won[1], attempts=lookup.attempts)
    status = lookup.attempts[-1][1] if lookup.attempts else "no_credentials"
    return _ClaudeAuthResolution(None, None, None, status, attempts=lookup.attempts)


def _read_host_claude_login() -> _HostLoginLookup:
    """Return the host ``claude`` CLI's login credentials + a source trail. Never raises.

    The jail redirects ``CLAUDE_CONFIG_DIR`` into an empty scratch, which strips the
    CLI's sign-in state; these credentials are re-seeded into that scratch so a
    subscription login works exactly like an env-carried key. Locations are Claude
    Code internals and may shift across CLI versions — every source here fails soft
    (a keyless resolution then makes the public headless provider refuse before
    launch, unless ``SHEPHERD_ALLOW_KEYLESS_CLAUDE`` is set), but each attempt is
    recorded (source class + coarse status, never a path or bytes) so a keyless
    verdict can name *why*. Ambiguous platform signals collapse to ``keychain_failed``
    rather than guessing ``security`` exit-code trivia.
    """
    attempts: list[tuple[str, str]] = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    file_sources: list[tuple[str, Path]] = []
    if config_dir:
        file_sources.append(("configured_config", Path(config_dir) / ".credentials.json"))
    file_sources.append(("default_config", Path.home() / ".claude" / ".credentials.json"))
    for source, candidate in file_sources:
        try:
            if candidate.is_file():
                blob = candidate.read_bytes()
                attempts.append((source, f"{source}_found"))
                return _HostLoginLookup(blob, tuple(attempts))
            attempts.append((source, f"{source}_missing"))
        except Exception:  # noqa: BLE001 — an unreadable source is a recorded miss, not a crash
            attempts.append((source, f"{source}_unreadable"))

    if sys.platform != "darwin":
        attempts.append(("macos_keychain", "unsupported_platform"))
        return _HostLoginLookup(None, tuple(attempts))
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        attempts.append(("macos_keychain", "keychain_timeout"))
        return _HostLoginLookup(None, tuple(attempts))
    except Exception:  # noqa: BLE001 — collapse ambiguous `security` failures, don't guess
        attempts.append(("macos_keychain", "keychain_failed"))
        return _HostLoginLookup(None, tuple(attempts))
    if proc.returncode == 0 and proc.stdout.strip():
        attempts.append(("macos_keychain", "keychain_found"))
        return _HostLoginLookup(bytes(proc.stdout.strip()), tuple(attempts))
    # `security` exits 44 for "not found"; any other nonzero is a denial/other
    # failure. We do not overfit the exact code — not-found vs failed is the
    # useful cut; anything ambiguous collapses to keychain_failed.
    status = "keychain_not_found" if proc.returncode == 44 else "keychain_failed"
    attempts.append(("macos_keychain", status))
    return _HostLoginLookup(None, tuple(attempts))


def _claude_blob_expiry(blob: bytes | None) -> bool | None:
    """Whether a subscription login blob's access token is expired.

    ``True`` = expired, ``False`` = still valid, ``None`` = not determinable
    (missing field / unrecognized shape). Never raises. The blob is Claude Code's
    ``.credentials.json`` / keychain payload: ``{"claudeAiOauth": {"expiresAt": <ms>}}``.
    """
    if not blob:
        return None
    try:
        data = json.loads(blob)
        oauth = data.get("claudeAiOauth") if isinstance(data, Mapping) else None
        expires_at = oauth.get("expiresAt") if isinstance(oauth, Mapping) else None
        if not isinstance(expires_at, (int, float)):
            return None
        return (expires_at / 1000.0) < time.time()
    except Exception:  # noqa: BLE001 — best-effort; an unreadable blob is "not determinable"
        return None


@dataclass(frozen=True)
class ClaudeAuthStatus:
    """The offline readiness verdict for the jailed Claude lane."""

    mode: str | None
    ok: bool
    detail: str


def claude_auth_status() -> ClaudeAuthStatus:
    """Offline verdict on whether the jailed Claude lane can authenticate.

    Cheap and network-free. Env credentials pass; an absent login fails; a
    subscription login is inspected for token expiry — an expired access token
    cannot be refreshed under the jail (keychain write-back is blocked), so it is
    a hard fail. A valid-looking login is reported ``ok`` but *unverified*:
    ``probe_claude_auth`` is the authoritative check. This is what makes a green
    ``doctor`` honest rather than merely "a blob is readable".
    """
    resolution = _resolve_claude_auth_diagnostic()
    mode, blob = resolution.mode, resolution.blob
    if mode == "api_key":
        return ClaudeAuthStatus(mode, True, "ANTHROPIC_API_KEY set")
    if mode == "oauth_token":
        return ClaudeAuthStatus(mode, True, "CLAUDE_CODE_OAUTH_TOKEN set")
    if mode is None:
        return ClaudeAuthStatus(None, False, _keyless_detail(resolution))
    expired = _claude_blob_expiry(blob)
    if expired is True:
        return ClaudeAuthStatus(
            mode,
            False,
            "signed-in `claude` CLI, but the access token is expired — a jailed run cannot "
            "refresh it; run `claude login` or set CLAUDE_CODE_OAUTH_TOKEN",
        )
    unverified = "run `shepherd doctor claude --probe` to authenticate"
    if expired is False:
        return ClaudeAuthStatus(mode, True, f"signed-in `claude` CLI (found, not verified — {unverified})")
    return ClaudeAuthStatus(
        mode, True, f"signed-in `claude` CLI (found, format unrecognized, not verified — {unverified})"
    )


_AUTH_REMEDY = (
    "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or ANTHROPIC_API_KEY, or sign in with `claude login`"
)


def _keyless_detail(resolution: _ClaudeAuthResolution) -> str:
    """A sharp offline message for a ``mode is None`` verdict, named by source status.

    Scans the *whole* attempt trail for the most actionable signal rather than only
    the last status: a source that was found-but-unreadable or a keychain that was
    denied/timed out is more useful to surface than a later "not found", which is
    the trail's normal terminal state.
    """
    if resolution.seeding_disabled:
        return f"credential seeding is disabled (SHEPHERD_NO_CREDENTIAL_SEEDING) and no env credential is set — {_AUTH_REMEDY}"
    statuses = {status for _source, status in resolution.attempts} or {resolution.status}
    if "keychain_timeout" in statuses:
        return f"the macOS keychain lookup timed out — {_AUTH_REMEDY}"
    if "keychain_failed" in statuses:
        return f"the macOS keychain lookup was denied or failed — {_AUTH_REMEDY}"
    if any(status.endswith("_unreadable") for status in statuses):
        return f"a `claude` credential file was found but unreadable — {_AUTH_REMEDY}"
    return f"no signed-in `claude` login found — {_AUTH_REMEDY}"


_KEYLESS_ESCAPE = (
    "If a `claude` wrapper authenticates outside Shepherd's known credential routes, "
    "set SHEPHERD_ALLOW_KEYLESS_CLAUDE=1 to launch anyway."
)


def _claude_preflight_refusal(resolution: _ClaudeAuthResolution) -> tuple[str, str, str] | None:
    """``(classification, error_type, message)`` if a jailed launch is known-doomed, else ``None``.

    The public headless provider redirects ``HOME``/``CLAUDE_CONFIG_DIR`` into an
    empty scratch, so a body with no env credential and no seedable host login
    authenticates against nothing — a guaranteed not-logged-in failure — and an
    expired subscription blob cannot be refreshed under the jail. Both are refused
    before launch (unless ``SHEPHERD_ALLOW_KEYLESS_CLAUDE`` is set) so a trace reader
    sees a preflight refusal, not a wasted confined run that reads like a jail denial.
    """
    if resolution.mode is None:
        message = (
            f"Claude CLI auth is not available for a jailed run ({_keyless_detail(resolution)}). {_KEYLESS_ESCAPE}"
        )
        return "auth_missing", "ClaudeAuthMissing", message
    if resolution.mode == "subscription_login" and _claude_blob_expiry(resolution.blob) is True:
        message = (
            "the seeded `claude` subscription login is expired and a jailed run cannot refresh it — "
            "run `claude login` or set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`). If your "
            "`claude` wrapper keeps its real auth outside the standard store, set "
            "SHEPHERD_NO_CREDENTIAL_SEEDING=1 and SHEPHERD_ALLOW_KEYLESS_CLAUDE=1 to skip seeding the "
            "stale blob and launch anyway."
        )
        return "auth_expired", "ClaudeAuthExpired", message
    return None
